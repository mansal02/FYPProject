import re
import json
import time
import uuid
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Iterator

import requests
try:
    import google.generativeai as genai
except ImportError:
    genai = None

try:
    from PyQt5.QtCore import QThread, pyqtSignal
except ImportError:
    class QThread: pass
    def pyqtSignal(*args, **kwargs): return None


@dataclass
class TaskResult:
    """Result object containing response text, actions, metadata, and sources."""
    text: str
    actions: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    sources: list = field(default_factory=list)


@dataclass
class FastTaskResult:
    """Result with timing metrics."""
    text: str
    actions: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    timing_ms: float = 0.0


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


class OnlineLLMClient:
    """
    Client for calling Google Gemini API (online LLM).
    Handles authentication and generation requests.
    """
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Gemini client with API credentials.
        
        Args:
            config: Dict with 'api_key', 'model', and 'temperature' settings
        """
        self.api_key = config.get("api_key")
        self.default_model = config.get("model", "gemini-1.5-flash")
        self.temperature = config.get("temperature", 0.2)

        if genai and self.api_key:
            genai.configure(api_key=self.api_key)

    def generate(self, prompt: str, system: str = "", model: str = None) -> str:
        """
        Generate text using Gemini API.
        
        Args:
            prompt: User prompt/question
            system: System instruction for model behavior
            model: Specific model to use (default: self.default_model)
            
        Returns:
            Generated text response or error message
        """
        if not genai or not self.api_key:
            return "Online model not configured. Provide GEMINI_API_KEY."
        
        target_model = model or self.default_model
        try:
            model_instance = genai.GenerativeModel(
                model_name=target_model,
                system_instruction=system
            )
            result = model_instance.generate_content(
                prompt, 
                generation_config={"temperature": self.temperature}
            )
            return (result.text or "").strip()
        except Exception as e:
            print(f"[OnlineLLM Error]: {e}")
            return ""


class FastOfflineLLMClient:
    """
    Ultra-fast LLM client with optimized model sizes.
    Uses smaller, faster models for quick responses.
    """
    def __init__(self, config: Dict[str, Any]):
        """Initialize with fast model configuration."""
        self.base_url = config.get("base_url", "http://localhost:11434")
        self.fast_model = config.get("fast_model", "qwen2.5:3b")  
        self.balanced_model = config.get("balanced_model", "qwen2.5:7b")
        self.timeout = config.get("timeout", 30)  

    def generate(self, prompt: str, system: str = "", model: str = None, stream: bool = False) -> Any:
        """
        Generate text with optional streaming.
        
        Args:
            prompt: User prompt
            system: System instruction
            model: Model to use (default: fast_model)
            stream: Whether to stream response
            
        Returns:
            Generated text or iterator if streaming
        """
        target_model = model or self.fast_model
        payload = {
            "model": target_model,
            "prompt": prompt,
            "system": system,
            "stream": stream,
            "options": {
                "temperature": 0.1, 
                "num_ctx": 2048,  
                "num_predict": 256,  
            }
        }
        
        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout,
                stream=stream,
            )
            resp.raise_for_status()
            
            if stream:
                return self._stream_response(resp)
            else:
                return resp.json().get("response", "").strip()
        except Exception as e:
            print(f"[FastLLM Error]: {e}")
            return "" if not stream else iter([])

    def _stream_response(self, response) -> Iterator[str]:
        """Stream response chunks as they arrive."""
        for line in response.iter_lines():
            if line:
                try:
                    chunk = json.loads(line)
                    text = chunk.get("response", "")
                    if text:
                        yield text
                except json.JSONDecodeError:
                    continue


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
            
            user_id = getattr(context, "user_id", None) if context else None
            session_id = getattr(context, "session_id", None) if context else None
            rag_context = ""
            if self.db and user_id and hasattr(self.db, "build_memory_context"):
                rag_context = self.db.build_memory_context(user_id, session_id)

            if self.config.get("llm", {}).get("use_single_pass_optimization", True):
                combined_system = (
                    f"You are {self.spec.name}. {self.spec.description}\n"
                    f"Available tools: {list(self.tools.keys())}.\n"
                    "Analyze the core intent, determine the parameters, and output your answer directly as strict JSON.\n"
                    "Use keys: 'tool' (string name of tool, or null), 'args' (dict of arguments), 'response' (what to say to the user)."
                )

                if rag_context:
                    combined_system += f"\n\n[Context Memory]\n{rag_context}"
                    
                reasoner_cfg = self.pipeline.get("reasoner")
                formatted_json = self._call_model(reasoner_cfg, query, combined_system)
            else:
                parser_cfg = self.pipeline.get("parser")
                sys_parser = "Extract the core intent and parameters from the user query. Output ONLY the refined instruction."
                parsed_query = self._call_model(parser_cfg, query, sys_parser)
                
                if not parsed_query or not parsed_query.strip():
                    parsed_query = query
                reasoner_cfg = self.pipeline.get("reasoner")
                sys_reasoner = f"You are {self.spec.name}. {self.spec.description} Analyze the request and determine the exact steps. Available tools: {list(self.tools.keys())}."
                if rag_context:
                    sys_reasoner += f"\n\n[Context Memory]\n{rag_context}"                   
                reasoning = self._call_model(reasoner_cfg, parsed_query, sys_reasoner)
                if not reasoning or not reasoning.strip():
                    reasoning = parsed_query
                formatter_cfg = self.pipeline.get("formatter")
                sys_formatter = "Format the reasoning into strict JSON. Use keys: 'tool' (string name of tool, or null), 'args' (dict of arguments), 'response' (what to say to the user)."
                formatted_json = self._call_model(formatter_cfg, reasoning, sys_formatter)        

            try:
                if not formatted_json:
                    raise ValueError("Model returned an empty response")
                    
                clean_json = formatted_json.replace("```json", "").replace("```", "").strip()
                plan_data = json.loads(clean_json)
                
                if not isinstance(plan_data, dict):
                    raise json.JSONDecodeError("JSON did not deserialize to a dict", clean_json, 0)
                
                tool_name = plan_data.get("tool")
                tool_args = plan_data.get("args", {})
                response_text = plan_data.get("response", "Task completed.")
                requires_confirmation = bool(plan_data.get("requires_confirmation"))
                summary = plan_data.get("summary") or response_text or "Confirm action"
                
            except (json.JSONDecodeError, AttributeError, ValueError):
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

            tool_output = None
            if self.spec.intent == "files" and self.db and hasattr(self.db, "search_files"):
                tool_output = self.db.search_files(query, limit=5)

            if tool_name and tool_name in self.tools:
                tool_output = self._run_tool(tool_name, tool_args)

            return TaskResult(
                text=response_text,
                actions={"tool": tool_name, "args": tool_args, "output": tool_output},
            )

    def _run_tool(self, name: str, args: Dict[str, Any]):
        """Execute a tool with given arguments."""
        tool = self.tools.get(name)
        if not tool:
            return None
        try:
            return tool(**args)
        except Exception as e:
            return f"Error executing {name}: {str(e)}"

    def _select_tool_plan(self, query: str) -> ToolPlan:
        """Fallback: Extract tool plan using regex patterns."""
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
        super().__init__(spec, pipeline, db, config)
        self.offline_client = OfflineLLMClient(config.get("llm", {}).get("offline", {}))

    def _call_model(self, step_model: str, prompt: str, system: str) -> str:
        """Call offline LLM model."""
        return self.offline_client.generate(prompt, system=system, model=step_model)


class OnlineWorker(BaseWorker):
    """
    Worker using hybrid online/offline LLM pipeline.
    Can use either Gemini (online) or Ollama (offline) for each stage.
    """
    def __init__(self, spec, pipeline: dict, db, config):
        super().__init__(spec, pipeline, db, config)
        self.online_client = OnlineLLMClient(config.get("llm", {}).get("online", {}))
        self.offline_client = OfflineLLMClient(config.get("llm", {}).get("offline", {}))

    def _call_model(self, step_config: dict, prompt: str, system: str) -> str:
        """Call appropriate LLM based on pipeline stage configuration."""
        provider = step_config.get("provider")
        model_name = step_config.get("model")
        
        if provider == "gemini":
            return self.online_client.generate(prompt, system=system, model=model_name)
        else:
            return self.offline_client.generate(prompt, system=system, model=model_name)


class FastOfflineWorker:
    """
    Ultra-fast worker optimized for 3x speed improvement.
    Skips parsing, uses ultra-lightweight models, and collapses processing steps.
    """
    def __init__(self, spec, pipeline: dict, db, config):
        self.spec = spec
        self.pipeline = pipeline
        self.db = db
        self.config = config
        self.tools = getattr(spec, "tools", {})
        self.llm = FastOfflineLLMClient(config.get("llm", {}).get("offline", {}))

    def execute_fast(self, query: str, context) -> FastTaskResult:
        """Fast execution: skip parser, combine stages, use fast models, inject RAG context."""
        t_start = time.time()

        user_id = getattr(context, "user_id", None) if context else None
        session_id = getattr(context, "session_id", None) if context else None
        rag_context = ""
        
        if self.db and user_id and hasattr(self.db, "build_memory_context"):
            rag_context = self.db.build_memory_context(user_id, session_id)

        memory_block = f"Relevant System Context Memory:\n{rag_context}\n\n" if rag_context else ""
        
        combined_prompt = f"""{memory_block}User Query: {query}

You are {self.spec.name}. {self.spec.description}

Available tools: {list(self.tools.keys())}

RESPOND ONLY with valid JSON (no markdown wrapper blocks):
{{"tool": "tool_name_or_null", "args": {{}}, "response": "user response"}}"""

        sys_prompt = "You are a fast AI assistant. Output ONLY valid JSON."
        
        reasoning_json = self.llm.generate(
            combined_prompt,
            system=sys_prompt,
            model=self.llm.fast_model
        )

        try:
            if not reasoning_json:
                raise ValueError("Model returned an empty fast response.")
                
            clean_json = str(reasoning_json).replace("```json", "").replace("```", "").strip()
            plan = json.loads(clean_json)
            
            if not isinstance(plan, dict):
                raise ValueError("Deserialized JSON payload is not a dictionary object.")
                
        except (json.JSONDecodeError, ValueError, AttributeError) as parse_err:
            print(f"[FastPass Warning] LLM JSON format parsing failed ({parse_err}). Falling back gracefully.")
            plan = {
                "tool": None, 
                "args": {}, 
                "response": "I encountered an error parsing the fast instructions, processing request manually."
            }

        tool_name = plan.get("tool")
        tool_args = plan.get("args", {})
        tool_output = None
        
        if tool_name and tool_name in self.tools:
            try:
                tool_output = self.tools[tool_name](**tool_args)
            except Exception as tool_err:
                print(f"[FastPass Tool Crash Guard] Tool '{tool_name}' failed execution: {tool_err}")
                tool_output = f"Execution error: {str(tool_err)}"
        
        elapsed_ms = (time.time() - t_start) * 1000
        
        return FastTaskResult(
            text=plan.get("response", "Task completed via ultra-fast tracking."),
            actions={"tool": tool_name, "args": tool_args, "output": tool_output},
            meta={"optimized": True, "stages_skipped": 1, "rag_applied": bool(rag_context)},
            timing_ms=elapsed_ms,
        )

    def execute_streaming(self, query: str, context) -> Iterator[str]:
        """Stream response for real-time UI updates."""
        combined_prompt = f"""User Query: {query}

You are {self.spec.name}. {self.spec.description}

Respond concisely and directly."""

        sys_prompt = "Respond quickly and concisely."
        
        for chunk in self.llm.generate(
            combined_prompt,
            system=sys_prompt,
            model=self.llm.fast_model,
            stream=True
        ):
            if chunk:
                yield chunk


class CachedFastWorker(FastOfflineWorker):
    """Fast worker incorporating in-memory hit-caching mechanics."""
    def __init__(self, spec, pipeline: dict, db, config):
        super().__init__(spec, pipeline, db, config)
        self._cache = {}
        self._cache_hits = 0
        self._cache_misses = 0

    def execute_fast(self, query: str, context) -> FastTaskResult:
        """Execute with layer 1 caching evaluation."""
        t_start = time.time()
        cache_key = query.lower().strip()
        
        if cache_key in self._cache:
            self._cache_hits += 1
            cached_result = self._cache[cache_key]
            elapsed_ms = (time.time() - t_start) * 1000
            
            result = FastTaskResult(
                text=cached_result["text"],
                actions=cached_result["actions"],
                meta={"cached": True, "cache_hits": self._cache_hits},
                timing_ms=elapsed_ms,
            )
            print(f"[Cache HIT] {cache_key[:40]:40s} ({elapsed_ms:.0f}ms)")
            return result
        
        self._cache_misses += 1
        result = super().execute_fast(query, context)
        
        self._cache[cache_key] = {
            "text": result.text,
            "actions": result.actions,
        }
        
        print(f"[Cache MISS] {cache_key[:40]:40s} ({result.timing_ms:.0f}ms)")
        return result


def create_fast_worker(intent: str, tools: Dict[str, callable], config: Dict[str, Any]):
    """Factory builder for rapid deployment of CachedFastWorker models."""
    spec = type('FastSpec', (), {
        'name': f'{intent.title()}Worker',
        'intent': intent,
        'description': f'Fast worker for {intent} tasks',
        'tools': tools,
    })()
    
    pipeline = {
        "reasoner": "qwen2.5:3b",
        "formatter": None,
    }
    
    return CachedFastWorker(spec, pipeline, None, config)


class ReasoningStreamWorker(QThread):
    """Streams LLM output from the reasoning server without blocking the UI thread."""

    token_received = pyqtSignal(str) if 'pyqtSignal' in globals() else None
    sentence_ready = pyqtSignal(str) if 'pyqtSignal' in globals() else None
    completed = pyqtSignal(str, str) if 'pyqtSignal' in globals() else None
    failed = pyqtSignal(str) if 'pyqtSignal' in globals() else None
    cancelled = pyqtSignal() if 'pyqtSignal' in globals() else None
    stream_started = pyqtSignal(str) if 'pyqtSignal' in globals() else None

    def __init__(self, stream_url, stop_url, payload, timeout_sec=140):
        super().__init__()
        self.stream_url = stream_url
        self.stop_url = stop_url
        self.payload = dict(payload or {})
        self.timeout_sec = timeout_sec
        self.request_id = self.payload.get("request_id") or str(uuid.uuid4())
        self.payload["request_id"] = self.request_id
        self._cancel_event = threading.Event()
        self._active_response = None
        self._last_sentiment = "neutral"

    def cancel(self):
        self._cancel_event.set()
        try:
            requests.post(self.stop_url, json={"request_id": self.request_id}, timeout=2)
        except Exception:
            pass

        try:
            if self._active_response is not None:
                self._active_response.close()
        except Exception:
            pass

    def run(self):
        full_text = ""
        if self.stream_started: self.stream_started.emit(self.request_id)

        try:
            response = requests.post(
                self.stream_url,
                json=self.payload,
                stream=True,
                timeout=self.timeout_sec,
            )
            response.raise_for_status()
            self._active_response = response

            for raw_line in response.iter_lines(decode_unicode=True):
                if self._cancel_event.is_set():
                    if self.cancelled: self.cancelled.emit()
                    return

                if not raw_line:
                    continue

                try:
                    packet = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                packet_type = packet.get("type")
                content = packet.get("content", "")

                if packet_type == "token":
                    full_text += content
                    if self.token_received: self.token_received.emit(content)
                elif packet_type == "sentence":
                    if content and self.sentence_ready:
                        self.sentence_ready.emit(content)
                elif packet_type == "done":
                    response_text = packet.get("full_response", full_text)
                    self._last_sentiment = packet.get("sentiment") or self._last_sentiment
                    if self.completed: self.completed.emit(response_text, self._last_sentiment)
                    return
                elif packet_type == "error":
                    if self.failed: self.failed.emit(content or "Reasoning stream failed.")
                    return

            if self._cancel_event.is_set():
                if self.cancelled: self.cancelled.emit()
            else:
                if self.completed: self.completed.emit(full_text, self._last_sentiment)

        except Exception as e:
            if self._cancel_event.is_set():
                if self.cancelled: self.cancelled.emit()
            else:
                if self.failed: self.failed.emit(str(e))
        finally:
            self._active_response = None



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