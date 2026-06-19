import re
import json
import time
import uuid
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Iterator

import requests

# --- Optional Dependencies ---
try:
    import google.generativeai as genai
except ImportError:
    genai = None

try:
    from PyQt5.QtCore import QThread, pyqtSignal
except ImportError:
    class QThread: pass
    def pyqtSignal(*args, **kwargs): return None


# ==========================================
# 1. DATA MODELS
# ==========================================

@dataclass
class TaskResult:
    """Standard result object containing response text, actions, and metadata."""
    text: str
    actions: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    sources: list = field(default_factory=list)


@dataclass
class FastTaskResult:
    """Result object optimized with timing metrics."""
    text: str
    actions: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    timing_ms: float = 0.0


@dataclass
class ToolPlan:
    """Plan for tool execution, extracted from user query."""
    tool: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    summary: str = ""


# ==========================================
# 2. LLM CLIENTS
# ==========================================

class OfflineLLMClient:
    """
    Unified client for calling local LLMs via Ollama API.
    Handles both standard generation and real-time streaming.
    """
    def __init__(self, config: Dict[str, Any]):
        self.base_url = config.get("base_url", "http://localhost:11434")
        self.default_model = config.get("model", "qwen2.5-coder:7b")
        self.fast_model = config.get("fast_model", "qwen2.5:3b")  
        self.timeout = config.get("timeout", 120)

    def generate(self, prompt: str, system: str = "", model: str = None, stream: bool = False, options: dict = None) -> Any:
        target_model = model or self.default_model
        opts = options or {"temperature": 0.2, "num_ctx": 4096}
        
        payload = {
            "model": target_model,
            "prompt": prompt,
            "system": system,
            "stream": stream,
            "options": opts
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
            return resp.json().get("response", "").strip()
            
        except Exception as e:
            print(f"[OfflineLLM Error - {target_model}]: {e}")
            return "" if not stream else iter([])

    def _stream_response(self, response) -> Iterator[str]:
        for line in response.iter_lines():
            if line:
                try:
                    chunk = json.loads(line)
                    if text := chunk.get("response", ""):
                        yield text
                except json.JSONDecodeError:
                    continue


class OnlineLLMClient:
    """
    Client for calling Google Gemini API.
    Handles authentication and online generation requests.
    """
    def __init__(self, config: Dict[str, Any]):
        self.api_key = config.get("api_key")
        self.default_model = config.get("model", "gemini-1.5-flash")
        self.temperature = config.get("temperature", 0.2)

        if genai and self.api_key:
            genai.configure(api_key=self.api_key)

    def generate(self, prompt: str, system: str = "", model: str = None) -> str:
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


# ==========================================
# 3. CORE WORKERS
# ==========================================

class BaseWorker:
    """Base class defining the standard task execution pipeline."""
    def __init__(self, spec, pipeline: dict, db, config):
        self.spec = spec
        self.pipeline = pipeline
        self.db = db
        self.config = config
        self.tools = getattr(spec, "tools", {})

    def _call_model(self, step_config, prompt: str, system: str) -> str:
        """To be implemented by subclasses (Offline/Online)"""
        raise NotImplementedError

    def execute(self, query, context) -> TaskResult:
        """Executes the task processing pipeline including intent checking and memory handling."""
        pending = context.metadata.get("pending_action") if context else None
        
        # Handle pending user confirmations
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
        
        # Build Context
        user_id = getattr(context, "user_id", None) if context else None
        session_id = getattr(context, "session_id", None) if context else None
        rag_context = ""
        if self.db and user_id and hasattr(self.db, "build_memory_context"):
            rag_context = self.db.build_memory_context(user_id, session_id)

        # Single-pass vs Multi-pass Logic
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
            # Multi-pass Logic (Parse -> Reason -> Format)
            parser_cfg = self.pipeline.get("parser")
            sys_parser = "Extract the core intent and parameters from the user query. Output ONLY the refined instruction."
            parsed_query = self._call_model(parser_cfg, query, sys_parser) or query
            
            reasoner_cfg = self.pipeline.get("reasoner")
            sys_reasoner = f"You are {self.spec.name}. {self.spec.description} Analyze the request and determine the exact steps. Available tools: {list(self.tools.keys())}."
            if rag_context:
                sys_reasoner += f"\n\n[Context Memory]\n{rag_context}"                   
            reasoning = self._call_model(reasoner_cfg, parsed_query, sys_reasoner) or parsed_query
            
            formatter_cfg = self.pipeline.get("formatter")
            sys_formatter = "Format the reasoning into strict JSON. Use keys: 'tool' (string name of tool, or null), 'args' (dict of arguments), 'response' (what to say to the user)."
            formatted_json = self._call_model(formatter_cfg, reasoning, sys_formatter)        

        # Parse output payload
        try:
            if not formatted_json:
                raise ValueError("Model returned an empty response")
                
            clean_json = formatted_json.replace("```json", "").replace("```", "").strip()
            if json_match := re.search(r"\{[\s\S]*\}", clean_json):
                clean_json = json_match.group(0)
            
            plan_data = json.loads(clean_json)
            if not isinstance(plan_data, dict):
                raise json.JSONDecodeError("JSON did not deserialize to a dict", clean_json, 0)
            
            tool_name = plan_data.get("tool")
            tool_args = plan_data.get("args", {})
            response_text = plan_data.get("response", "Task completed.")
            
        except (json.JSONDecodeError, AttributeError, ValueError):
            print("[Warning] LLM JSON parsing failed. Falling back to regex tool selection.")
            fallback_plan = self._select_tool_plan(query)
            tool_name = fallback_plan.tool
            tool_args = fallback_plan.args or {}
            response_text = fallback_plan.summary or "I am processing your request."

        # Execute Tool if requested
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
        tool = self.tools.get(name)
        if not tool:
            return None
        try:
            return tool(**args)
        except Exception as e:
            return f"Error executing {name}: {str(e)}"

    def _select_tool_plan(self, query: str) -> ToolPlan:
        """Regex Fallback if JSON generation fails."""
        lower = query.lower()
        if self.spec.intent == "office" and _looks_like_meeting(lower):
            emails = _extract_emails(query)
            attendees = _extract_labeled_emails(query, ["with", "attendees", "invite", "invites"]) or emails
            subject = _extract_phrase(query, "subject") or _extract_phrase(query, "title")
            body = _extract_phrase(query, "agenda") or _extract_phrase(query, "notes")
            location = _extract_location(query)
            start = _extract_datetime_phrase(query)
            duration = _extract_duration_minutes(query) or 30
            
            if attendees and start:
                return ToolPlan(
                    tool="create_outlook_meeting",
                    args={"attendees": attendees, "subject": subject, "start": start, "duration_minutes": duration, "location": location, "body": body},
                    summary=f"schedule meeting with {', '.join(attendees)} at {start}",
                )
        return ToolPlan()


class OfflineWorker(BaseWorker):
    """Uses purely offline models for the pipeline."""
    def __init__(self, spec, pipeline: dict, db, config):
        super().__init__(spec, pipeline, db, config)
        self.offline_client = OfflineLLMClient(config.get("llm", {}).get("offline", {}))

    def _call_model(self, step_model: str, prompt: str, system: str) -> str:
        return self.offline_client.generate(prompt, system=system, model=step_model)


class OnlineWorker(BaseWorker):
    """Hybrid online/offline routing worker based on step configuration."""
    def __init__(self, spec, pipeline: dict, db, config):
        super().__init__(spec, pipeline, db, config)
        self.online_client = OnlineLLMClient(config.get("llm", {}).get("online", {}))
        self.offline_client = OfflineLLMClient(config.get("llm", {}).get("offline", {}))

    def _call_model(self, step_config: dict, prompt: str, system: str) -> str:
        provider = step_config.get("provider")
        model_name = step_config.get("model")
        
        if provider == "gemini":
            return self.online_client.generate(prompt, system=system, model=model_name)
        return self.offline_client.generate(prompt, system=system, model=model_name)


# ==========================================
# 4. FAST / SPECIALIZED WORKERS
# ==========================================

class FastOfflineWorker:
    """Optimized worker that skips standard parsing to achieve maximum speed."""
    def __init__(self, spec, pipeline: dict, db, config):
        self.spec = spec
        self.pipeline = pipeline
        self.db = db
        self.config = config
        self.tools = getattr(spec, "tools", {})
        self.llm = OfflineLLMClient(config.get("llm", {}).get("offline", {}))

    def execute_fast(self, query: str, context) -> FastTaskResult:
        t_start = time.time()
        user_id = getattr(context, "user_id", None) if context else None
        session_id = getattr(context, "session_id", None) if context else None
        
        rag_context = ""
        if self.db and user_id and hasattr(self.db, "build_memory_context"):
            rag_context = self.db.build_memory_context(user_id, session_id)

        memory_block = f"Relevant System Context Memory:\n{rag_context}\n\n" if rag_context else ""
        combined_prompt = (
            f"{memory_block}User Query: {query}\n"
            f"You are {self.spec.name}. {self.spec.description}\n"
            f"Available tools: {list(self.tools.keys())}\n"
            "RESPOND ONLY with valid JSON (no markdown wrapper blocks):\n"
            '{"tool": "tool_name_or_null", "args": {}, "response": "user response"}'
        )

        sys_prompt = "You are a fast AI assistant. Output ONLY valid JSON."
        options = {"temperature": 0.1, "num_ctx": 2048, "num_predict": 256}
        
        reasoning_json = self.llm.generate(
            combined_prompt,
            system=sys_prompt,
            model=self.llm.fast_model,
            options=options
        )

        try:
            if not reasoning_json:
                raise ValueError("Empty fast response.")
            clean_json = str(reasoning_json).replace("```json", "").replace("```", "").strip()
            plan = json.loads(clean_json)
            if not isinstance(plan, dict):
                raise ValueError("JSON payload is not a dictionary.")
                
        except (json.JSONDecodeError, ValueError, AttributeError) as parse_err:
            print(f"[FastPass Warning] LLM parsing failed ({parse_err}). Falling back.")
            plan = {
                "tool": None, 
                "args": {}, 
                "response": "I encountered an error parsing instructions, processing manually."
            }

        tool_name = plan.get("tool")
        tool_args = plan.get("args", {})
        tool_output = None
        
        if tool_name and tool_name in self.tools:
            try:
                tool_output = self.tools[tool_name](**tool_args)
            except Exception as tool_err:
                print(f"[FastPass Tool Crash] Tool '{tool_name}' failed: {tool_err}")
                tool_output = f"Execution error: {str(tool_err)}"
        
        elapsed_ms = (time.time() - t_start) * 1000
        
        return FastTaskResult(
            text=plan.get("response", "Task completed via ultra-fast tracking."),
            actions={"tool": tool_name, "args": tool_args, "output": tool_output},
            meta={"optimized": True, "stages_skipped": 1, "rag_applied": bool(rag_context)},
            timing_ms=elapsed_ms,
        )

    def execute_streaming(self, query: str, context) -> Iterator[str]:
        combined_prompt = f"User Query: {query}\nYou are {self.spec.name}. {self.spec.description}\nRespond concisely."
        return self.llm.generate(
            combined_prompt,
            system="Respond quickly and concisely.",
            model=self.llm.fast_model,
            stream=True
        )


class CachedFastWorker(FastOfflineWorker):
    """Fast worker with an internal memory cache to rapidly serve repeat requests."""
    def __init__(self, spec, pipeline: dict, db, config):
        super().__init__(spec, pipeline, db, config)
        self._cache = {}
        self._cache_hits = 0
        self._cache_misses = 0

    def execute_fast(self, query: str, context) -> FastTaskResult:
        t_start = time.time()
        cache_key = query.lower().strip()
        
        if cache_key in self._cache:
            self._cache_hits += 1
            cached_result = self._cache[cache_key]
            elapsed_ms = (time.time() - t_start) * 1000
            
            print(f"[Cache HIT] {cache_key[:40]:40s} ({elapsed_ms:.0f}ms)")
            return FastTaskResult(
                text=cached_result["text"],
                actions=cached_result["actions"],
                meta={"cached": True, "cache_hits": self._cache_hits},
                timing_ms=elapsed_ms,
            )
        
        self._cache_misses += 1
        result = super().execute_fast(query, context)
        
        self._cache[cache_key] = {"text": result.text, "actions": result.actions}
        print(f"[Cache MISS] {cache_key[:40]:40s} ({result.timing_ms:.0f}ms)")
        return result


def create_fast_worker(intent: str, tools: Dict[str, callable], config: Dict[str, Any]):
    """Factory builder to rapidly deploy CachedFastWorker instances."""
    spec = type('FastSpec', (), {
        'name': f'{intent.title()}Worker',
        'intent': intent,
        'description': f'Fast worker for {intent} tasks',
        'tools': tools,
    })()
    pipeline = {"reasoner": "qwen2.5:3b", "formatter": None}
    return CachedFastWorker(spec, pipeline, None, config)


class ReasoningStreamWorker(QThread):
    """Streams LLM output in the background without blocking UI (PyQt)."""
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
        try: requests.post(self.stop_url, json={"request_id": self.request_id}, timeout=2)
        except Exception: pass
        try:
            if self._active_response: self._active_response.close()
        except Exception: pass

    def run(self):
        full_text = ""
        if self.stream_started: self.stream_started.emit(self.request_id)

        try:
            response = requests.post(
                self.stream_url, json=self.payload, stream=True, timeout=self.timeout_sec
            )
            response.raise_for_status()
            self._active_response = response

            for raw_line in response.iter_lines(decode_unicode=True):
                if self._cancel_event.is_set():
                    if self.cancelled: self.cancelled.emit()
                    return

                if not raw_line: continue

                try: packet = json.loads(raw_line)
                except json.JSONDecodeError: continue

                packet_type = packet.get("type")
                content = packet.get("content", "")

                if packet_type == "token":
                    full_text += content
                    if self.token_received: self.token_received.emit(content)
                elif packet_type == "sentence":
                    if content and self.sentence_ready: self.sentence_ready.emit(content)
                elif packet_type == "done":
                    response_text = packet.get("full_response", full_text)
                    self._last_sentiment = packet.get("sentiment", self._last_sentiment)
                    if self.completed: self.completed.emit(response_text, self._last_sentiment)
                    return
                elif packet_type == "error":
                    if self.failed: self.failed.emit(content or "Reasoning stream failed.")
                    return

            if self._cancel_event.is_set():
                if self.cancelled: self.cancelled.emit()
            elif self.completed:
                self.completed.emit(full_text, self._last_sentiment)

        except Exception as e:
            if self._cancel_event.is_set():
                if self.cancelled: self.cancelled.emit()
            elif self.failed:
                self.failed.emit(str(e))
        finally:
            self._active_response = None


# ==========================================
# 5. UTILITY & REGEX HELPERS
# ==========================================

def _extract_number(text: str) -> int:
    match = re.search(r"(\d{1,3})", text)
    return max(0, min(100, int(match.group(1)))) if match else 50

def _parse_confirmation(text: str) -> Optional[str]:
    lower = text.lower()
    if any(t in lower for t in {"yes", "confirm", "proceed", "do it", "ok", "okay", "sure", "y"}):
        return "confirm"
    if any(t in lower for t in {"no", "cancel", "stop", "never mind", "n"}):
        return "cancel"
    return None

def _looks_like_meeting(text: str) -> bool:
    return any(t in text for t in ["meeting", "invite", "schedule", "calendar", "appointment"])

def _extract_emails(text: str):
    return re.findall(r"[\w\.-]+@[\w\.-]+", text)

def _extract_labeled_emails(text: str, labels):
    for label in labels:
        if match := re.search(rf"{label}\s+([^\n]+)", text, flags=re.IGNORECASE):
            return re.findall(r"[\w\.-]+@[\w\.-]+", match.group(1))
    return []

def _extract_phrase(text: str, anchor: str) -> str:
    match = re.search(rf"{anchor}\s+(.+)", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""

def _extract_location(text: str) -> str:
    if match := re.search(r"location\s+([^\n]+)", text, flags=re.IGNORECASE):
        return match.group(1).strip()
    if match := re.search(r"in\s+room\s+([^\n]+)", text, flags=re.IGNORECASE):
        return match.group(1).strip()
    return ""

def _extract_datetime_phrase(text: str) -> str:
    match = re.search(r"on\s+([\w\-/]+)", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""

def _extract_duration_minutes(text: str) -> Optional[int]:
    match = re.search(r"(\d+)\s*(minutes|minute|mins|min|hours|hour)", text, flags=re.IGNORECASE)
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2).lower()
    return value * 60 if unit.startswith("hour") else value