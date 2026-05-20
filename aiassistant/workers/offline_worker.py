# ============================================================================
# OFFLINE WORKER AGENT
# ============================================================================
# This module implements the OfflineWorker class for processing tasks
# using entirely local/offline LLMs (Ollama-based models).
# It uses a three-stage pipeline: Parser -> Reasoner -> Formatter
# ============================================================================

import re
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests


@dataclass
class TaskResult:
    """Result object containing response text, actions, metadata, and sources."""
    text: str
    actions: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    sources: list = field(default_factory=list)


@dataclass
class ToolPlan:
    """Plan for tool execution with optional confirmation requirement."""
    tool: Optional[str] = None
    args: Dict[str, Any] = None
    requires_confirmation: bool = False
    summary: str = ""


class OfflineLLMClient:
    """
    Client for calling local LLMs via Ollama API.
    Sends requests to Ollama server and handles generation.
    """
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize with Ollama server configuration.
        
        Args:
            config: Dict with base_url, model, and timeout settings
        """
        self.base_url = config.get("base_url", "http://localhost:11434")
        self.default_model = config.get("model", "llama3.1:8b")
        self.timeout = config.get("timeout", 120)

    def generate(self, prompt: str, system: str = "", model: str = None) -> str:
        """
        Generate text using offline LLM via Ollama.
        
        Args:
            prompt: User prompt/question
            system: System instruction
            model: Specific model to use (default: self.default_model)
            
        Returns:
            Generated text or empty string on error
        """
        target_model = model or self.default_model
        payload = {
            "model": target_model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {
                "temperature": 0.2,
                "num_ctx": 4096
            }
        }
        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
        except Exception as e:
            print(f"[OfflineLLM Error - {target_model}]: {e}")
            return ""


class BaseWorker:
    """
    Base class for both Offline and Online workers.
    Implements the core task execution pipeline.
    """
    def __init__(self, spec, pipeline: dict, db, config):
        """
        Initialize worker with specification and configuration.
        
        Args:
            spec: Worker specification
            pipeline: Pipeline configuration
            db: Database connection
            config: Application configuration
        """
        self.spec = spec
        self.pipeline = pipeline
        self.db = db
        self.config = config
        self.tools = getattr(spec, "tools", {})

    def _call_model(self, step_config, prompt: str, system: str) -> str:
        """To be implemented by subclasses (Offline/Online)"""
        raise NotImplementedError

    def execute(self, query, context):
        """
        Execute the task processing pipeline.
        
        Args:
            query: User query text
            context: Task context
            
        Returns:
            TaskResult with response and metadata
        """
        # 1. Handle Pending Confirmations
        pending = context.metadata.get("pending_action") if context else None
        if pending:
            decision = _parse_confirmation(query)
            if decision == "confirm":
                executed = self._run_tool(pending["tool"], pending.get("args", {}))
                context.metadata.pop("pending_action", None)
                return TaskResult(
                    text=f"Confirmed. {executed}",
                    actions={"tool": pending["tool"], "args": pending.get("args", {}), "output": executed},
                    meta={"confirmed": True},
                )
            if decision == "cancel":
                context.metadata.pop("pending_action", None)
                return TaskResult(text="Cancelled.", meta={"confirmed": False})

        print(f"\n[{self.spec.name}] Running Pipeline...")

        # 2. PIPELINE: PARSER - Extract core intent from user query
        parser_cfg = self.pipeline.get("parser")
        sys_parser = "Extract the core intent and parameters from the user query. Output ONLY the refined instruction."
        parsed_query = self._call_model(parser_cfg, query, sys_parser)
        print(f" -> Parsed: {parsed_query}")
        if not parsed_query.strip():
            parsed_query = query

        # 3. PIPELINE: REASONER - Analyze request and determine steps
        reasoner_cfg = self.pipeline.get("reasoner")
        sys_reasoner = f"You are {self.spec.name}. {self.spec.description} Analyze the request and determine the exact steps. Available tools: {list(self.tools.keys())}."
        reasoning = self._call_model(reasoner_cfg, parsed_query, sys_reasoner)
        if not reasoning.strip():
            reasoning = parsed_query

        # 4. PIPELINE: FORMATTER - Format reasoning into structured JSON
        formatter_cfg = self.pipeline.get("formatter")
        sys_formatter = "Format the reasoning into strict JSON. Use keys: 'tool' (string name of tool, or null), 'args' (dict of arguments), 'response' (what to say to the user)."
        formatted_json = self._call_model(formatter_cfg, reasoning, sys_formatter)

        # 5. Extract JSON and Execute
        try:
            clean_json = formatted_json.replace("```json", "").replace("```", "").strip()
            plan_data = json.loads(clean_json)
            
            # Ensure plan_data is a dictionary, not a string
            if not isinstance(plan_data, dict):
                raise json.JSONDecodeError("JSON did not deserialize to a dict", clean_json, 0)
            
            tool_name = plan_data.get("tool")
            tool_args = plan_data.get("args", {})
            response_text = plan_data.get("response", "Task completed.")
            requires_confirmation = bool(plan_data.get("requires_confirmation"))
            summary = plan_data.get("summary") or response_text or "Confirm action"
            
        except json.JSONDecodeError:
            print("[Warning] LLM JSON parsing failed. Falling back to regex _select_tool_plan.")
            fallback_plan = self._select_tool_plan(query)
            tool_name = fallback_plan.tool
            tool_args = fallback_plan.args or {}
            response_text = fallback_plan.summary or "I am processing your request."
            requires_confirmation = fallback_plan.requires_confirmation
            summary = fallback_plan.summary or "Confirm action"

        if requires_confirmation and context is not None:
            context.metadata["pending_action"] = {
                "intent": self.spec.intent,
                "tool": tool_name,
                "args": tool_args,
                "summary": summary,
            }
            return TaskResult(
                text=f"Confirm: {summary}",
                actions={"tool": tool_name, "args": tool_args, "output": None},
                meta={"requires_confirmation": True},
            )

        # Database File RAG execution
        tool_output = None
        if self.spec.intent == "files" and self.db:
            tool_output = self.db.search_files(query, limit=5)

        # Tool execution
        if tool_name and tool_name in self.tools:
            tool_output = self._run_tool(tool_name, tool_args)

        return TaskResult(
            text=response_text,
            actions={"tool": tool_name, "args": tool_args, "output": tool_output},
        )

    def _run_tool(self, name: str, args: Dict[str, Any]):
        """
        Execute a tool with given arguments.
        
        Args:
            name: Tool name
            args: Tool arguments
            
        Returns:
            Tool output or error message
        """
        tool = self.tools.get(name)
        if not tool:
            return None
        try:
            return tool(**args)
        except Exception as e:
            return f"Error executing {name}: {str(e)}"

    def _select_tool_plan(self, query: str) -> ToolPlan:
        """
        Fallback: Extract tool plan using regex patterns.
        
        Args:
            query: User query
            
        Returns:
            ToolPlan from regex extraction
        """
        lower = query.lower()
        if self.spec.intent == "office":
            if _looks_like_meeting(lower):
                emails = _extract_emails(query)
                attendees = _extract_labeled_emails(query, ["with", "attendees", "invite", "invites"]) or emails
                subject = _extract_phrase(query, "subject") or _extract_phrase(query, "title")
                body = _extract_phrase(query, "agenda") or _extract_phrase(query, "notes")
                location = _extract_location(query)
                start = _extract_datetime_phrase(query)
                duration = _extract_duration_minutes(query) or 30
                if attendees and start:
                    summary = f"schedule meeting with {', '.join(attendees)} at {start}"
                    return ToolPlan(
                        tool="create_outlook_meeting",
                        args={"attendees": attendees, "subject": subject, "start": start, "duration_minutes": duration, "location": location, "body": body},
                        requires_confirmation=True,
                        summary=summary,
                    )
        return ToolPlan()


class OfflineWorker(BaseWorker):
    """
    Worker using entirely offline/local LLMs via Ollama.
    All three pipeline stages (parser, reasoner, formatter) use local models.
    """
    def __init__(self, spec, pipeline: dict, db, config):
        """
        Initialize offline worker with offline LLM client.
        
        Args:
            spec: Worker specification
            pipeline: Pipeline configuration
            db: Database connection
            config: Application configuration
        """
        super().__init__(spec, pipeline, db, config)
        self.offline_client = OfflineLLMClient(config.get("llm", {}).get("offline", {}))

    def _call_model(self, step_model: str, prompt: str, system: str) -> str:
        """
        Call offline LLM model.
        
        Args:
            step_model: Model name to use
            prompt: User prompt
            system: System instruction
            
        Returns:
            Generated response
        """
        return self.offline_client.generate(prompt, system=system, model=step_model)


# ==========================================
# REGEX HELPERS FOR TOOL EXTRACTION
# ==========================================

def _extract_number(text: str) -> int:
    """Extract a number from text, clamped to 0-100."""
    match = re.search(r"(\d{1,3})", text)
    return max(0, min(100, int(match.group(1)))) if match else 50


def _parse_confirmation(text: str) -> Optional[str]:
    """Parse user confirmation response (yes/no/confirm/cancel)."""
    confirm = {"yes", "confirm", "proceed", "do it", "ok", "okay", "sure", "y"}
    cancel = {"no", "cancel", "stop", "never mind", "n"}
    lower = text.lower()
    if any(token in lower for token in confirm):
        return "confirm"
    if any(token in lower for token in cancel):
        return "cancel"
    return None


def _looks_like_meeting(text: str) -> bool:
    """Check if text appears to be about scheduling a meeting."""
    return any(token in text for token in ["meeting", "invite", "schedule", "calendar", "appointment"])


def _extract_emails(text: str):
    """Extract email addresses from text."""
    return re.findall(r"[\w\.-]+@[\w\.-]+", text)


def _extract_labeled_emails(text: str, labels):
    """Extract emails that follow specific labels (e.g., 'with john@...')."""
    for label in labels:
        match = re.search(rf"{label}\s+([^\n]+)", text, flags=re.IGNORECASE)
        if match:
            return re.findall(r"[\w\.-]+@[\w\.-]+", match.group(1))
    return []


def _extract_phrase(text: str, anchor: str) -> str:
    """Extract phrase following an anchor word."""
    match = re.search(rf"{anchor}\s+(.+)", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_location(text: str) -> str:
    """Extract meeting location from text."""
    match = re.search(r"location\s+([^\n]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"in\s+room\s+([^\n]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


def _extract_datetime_phrase(text: str) -> str:
    """Extract date/time phrase from text."""
    match = re.search(r"on\s+([\w\-/]+)", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_duration_minutes(text: str) -> Optional[int]:
    """Extract duration in minutes from text."""
    match = re.search(r"(\d+)\s*(minutes|minute|mins|min|hours|hour)", text, flags=re.IGNORECASE)
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2).lower()
    return value * 60 if unit.startswith("hour") else value
