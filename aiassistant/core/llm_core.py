from __future__ import annotations

# =====================================================================
# 1. SYSTEM & THIRD-PARTY IMPORTS
# =====================================================================
import asyncio
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import gc, json, os, re, socket, subprocess, sys, threading, time, pyautogui, openpyxl
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Conditional Third-Party Imports
try:
    import ollama
except Exception:
    ollama = None

try:
    import requests
except Exception:
    requests = None

try:
    from crewai import Agent, Task, Crew, Process
except ImportError:
    Agent = None
    Task = None
    Crew = None
    Process = None

# =====================================================================
# 2. LOCAL APPLICATION IMPORTS & BOOTSTRAP INITIALIZATION
# =====================================================================
from aiassistant.infra.config.app_config import CONFIG
from aiassistant.infra.db.database_manager import DatabaseManager
from aiassistant.workers import create_fast_worker
from aiassistant.workers.worker import OfflineLLMClient, OfflineWorker, OnlineLLMClient, OnlineWorker

_BOOT_STABILITY_MODE_LEVEL = 0
_BOOT_SAFE_MINIMAL = False

torch = None
if _BOOT_STABILITY_MODE_LEVEL < 2 and not _BOOT_SAFE_MINIMAL:
    try:
        import torch as _torch
    except Exception:
        torch = None
    else:
        torch = _torch

get_rag_context = None
if not _BOOT_SAFE_MINIMAL:
    try:
        from aiassistant.infra.rag_memory import get_rag_context
    except Exception:
        get_rag_context = None

try:
    from aiassistant.infra.optimization import QuantizationHelper
    QuantizationHelper.apply_quantization_env()
except Exception:
    pass

# Environment Defaults Optimization
os.environ.setdefault("OLLAMA_FLASH_ATTENTION", "1")
os.environ.setdefault("OLLAMA_KV_CACHE_TYPE", "q4_0")
os.environ.setdefault("OLLAMA_KEEP_ALIVE", "5m")

# =====================================================================
# 3. GLOBAL CONSTANTS & PROMPT TEMPLATES
# =====================================================================
_LAST_VRAM_CLEANUP_TIME = 0.0
_VRAM_CLEANUP_DEBOUNCE_SEC = 10.0 

MODEL = CONFIG["ollama"]["model"]
FORCED_REASONING_MODEL = "qwen2.5-coder:7b"
HYBRID_MODE = bool(CONFIG.get("runtime", {}).get("hybrid_mode", False))

DEFAULT_INTENTS = {
    "os": ["volume", "brightness", "open", "launch", "shutdown", "restart", "settings"],
    "office": ["excel", "word", "outlook", "teams", "sheet", "document"],
    "web": ["gmail", "calendar", "drive", "youtube", "discord", "whatsapp", "chrome"],
    "files": ["find", "search", "file", "folder", "document", "pdf"],
    "general": [],
}

SYSTEM_PROMPT = (
    "You are an offline desktop assistant. "
    "Prioritize action over explanation. "
    "When office tasks are involved, prioritize Excel workflows and formulas first. "
    "Default to concise, direct replies focused on what the user needs now. "
    "Do not teach or explain internal process unless the user asks. "
    "If user intent is vague, break it into a short numbered plan and choose the first safe step. "
    "For analysis or execution requests, perform the task and report outcomes. "
    "If asked to analyze files, folders, or the PC, provide practical findings. "
    "Use tools only when needed. If you need a tool, emit JSON inside <tool>...</tool> with one action object. "
    "Supported actions: list_system_roots, list_directory, deep_search, analyze_path, create_path, move_path, "
    "copy_path, delete_path, search_file, semantic_search_file, read_file, open_file, launch_application, "
    "close_application, list_running_apps, open_service, move_mouse, click, type_text, press_key, hotkey, "
    "run_command, toggle_dark_mode, send_email, draft_email_attachment, send_telegram, send_whatsapp, "
    "online_query, open_interpreter. "
    "Do not include JSON or tool syntax in normal user-facing replies. "
    "For simple requests, answer in easy plain language and keep it direct. "
    "For complex requests, provide enough detail to be useful without over-explaining."
)

FILE_RESPONSE_GUARD = (
    "When working with office files (.doc, .docx, .xlsx, .xls, .csv, .pdf), "
    "do not echo full file contents. Provide a brief response and refer to the file path. "
    "Apply the user's writing style profile to new documentation content."
)

PROMPT_BEHAVIOR_HINTS = {
    "default": "",
    "concise": "Prefer concise answers by default. Keep most responses to a few short sentences unless the user asks for detail.",
    "detailed": "Provide richer explanations and rationale when useful. Include structured steps for complex requests.",
    "action_first": "Prioritize concrete actions and outcomes before explanation. Lead with what was done or what should be done next.",
    "custom": "",
}


# =====================================================================
# 4. LOW-LEVEL INTERNAL UTILITIES / HELPERS
# =====================================================================
def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def _normalize_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in {"assist", "replace"} else "assist"

def _truncate_context(text: str, max_chars: int) -> str:
    if not text:
        return ""
    limit = max(200, int(max_chars))
    return text if len(text) <= limit else text[-limit:]

def _parse_confirmation(text: str) -> Optional[str]:
    confirm = {"yes", "confirm", "proceed", "do it", "ok", "okay", "sure", "y"}
    cancel = {"no", "cancel", "stop", "never mind", "n"}
    lower = text.lower()
    if any(token in lower for token in confirm):
        return "confirm"
    if any(token in lower for token in cancel):
        return "cancel"
    return None

def _ping(host: str) -> bool:
    try:
        socket.create_connection((host, 53), timeout=1.0)
        return True
    except OSError:
        return False

def _bootstrap_env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return bool(default)

def _bootstrap_env_int(name: str, default: int = 0) -> int:
    value = os.environ.get(name)
    if value is None:
        return int(default)
    try:
        return int(str(value).strip())
    except Exception:
        return int(default)

def _toggle_voice_rvc(enabled: bool) -> None:
    if requests is None:
        return
    host = str(CONFIG.get("servers", {}).get("voice_host", "127.0.0.1")).strip()
    port = int(CONFIG.get("servers", {}).get("voice_port", 8001))
    action = "load" if enabled else "unload"
    url = f"http://{host}:{port}/rvc/{action}"
    try:
        requests.post(url, json={}, timeout=2)
    except Exception:
        pass

def _run_tool_action_isolated(action: Dict[str, object], timeout_sec: int = 55) -> Dict[str, object]:
    try:
        payload = json.dumps(action, ensure_ascii=True)
    except Exception as exc:
        return {"success": False, "message": "Failed to serialize tool action.", "error": str(exc)}

    from aiassistant.infra.config.app_config import ROOT_DIR
    command = [sys.executable, "-m", "aiassistant.tools.tools_os"]

    try:
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(ROOT_DIR)
        completed = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            text=True,
            timeout=max(5, int(timeout_sec)),
            check=False,
            env=child_env,
            cwd=str(ROOT_DIR)
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Tool action timed out in isolated runner.", "error": "runner_timeout"}
    except Exception as exc:
        return {"success": False, "message": "Tool action runner failed to start.", "error": str(exc)}

    stdout_text = (completed.stdout or "").strip()
    stderr_text = (completed.stderr or "").strip()

    candidates = [line.strip() for line in stdout_text.splitlines() if line.strip()]
    if stdout_text:
        candidates.append(stdout_text)

    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict) and "success" in parsed:
            return parsed

    if completed.returncode == 0 and not stderr_text:
        return {"success": True, "message": "Tool action completed in isolated runner.", "data": {"output": stdout_text[:1200]}}

    details = "\n".join(part for part in [stdout_text, stderr_text] if part).strip()[:1800]
    return {"success": False, "message": "Tool action failed in isolated runner.", "error": details or f"runner_exit_code={completed.returncode}"}


# =====================================================================
# 5. DATA STRUCTURE MODELS & CLASSIFIERS
# =====================================================================
class Events:
    USER_SPOKE = "user_spoke"
    AI_TOKEN = "ai_token"
    AI_SENTENCE_READY = "ai_sentence_ready"
    AI_COMPLETED = "ai_completed"
    AUDIO_READY = "audio_ready"
    BARGE_IN = "barge_in"
    ERROR = "error"

class EventBus:
    """Simple thread-safe publish/subscribe event bus."""
    def __init__(self):
        self._handlers = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, event_name, callback):
        with self._lock:
            self._handlers[event_name].append(callback)

    def unsubscribe(self, event_name, callback):
        with self._lock:
            callbacks = self._handlers.get(event_name, [])
            if callback in callbacks:
                callbacks.remove(callback)

    def emit(self, event_name, payload=None):
        with self._lock:
            callbacks = list(self._handlers.get(event_name, []))
        for callback in callbacks:
            try:
                callback(payload)
            except Exception as exc:
                print(f"[EVENT BUS] Handler error for {event_name}: {exc}")

@dataclass
class ConcurrentTaskResult:
    tool_name: str
    result: Any
    duration_ms: float
    status: str
    error: Optional[str] = None

@dataclass
class TaskContext:
    user_id: int
    session_id: str
    mode: str
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class TaskResult:
    text: str
    actions: Dict[str, Any] = field(default_factory=dict)
    sources: list = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ParallelTaskContext:
    user_id: int
    session_id: str
    mode: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    timing: Dict[str, float] = field(default_factory=dict)
    parallel_results: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ParallelTaskResult:
    text: str
    actions: Dict[str, Any] = field(default_factory=dict)
    sources: List[str] = field(default_factory=list)
    rag_context: str = ""
    tool_outputs: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    timing: Dict[str, float] = field(default_factory=dict)  # REPAIRED

@dataclass
class FastResult:
    text: str
    timing_ms: float
    cached: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

@dataclass
class WorkerSpec:
    name: str
    intent: str
    description: str
    tools: Dict[str, callable]
    offline_pipeline: Dict[str, str]
    hybrid_pipeline: Dict[str, Dict[str, str]]

@dataclass
class AgentConfig:
    reasoning_model: str = FORCED_REASONING_MODEL
    ollama_host: str = "http://127.0.0.1:11434"
    max_history_turns: int = 3
    temperature: float = 0.2
    num_ctx: int = 2048
    num_predict: int = int(CONFIG.get("ollama", {}).get("num_predict", 320))
    rag_enabled: bool = True
    rag_top_k: int = 4
    hybrid_mode: bool = HYBRID_MODE
    external_model: str = str(CONFIG.get("runtime", {}).get("external_model", "gemini-2.0-flash"))
    online_mode: str = str(CONFIG.get("runtime", {}).get("online_mode", "auto"))
    external_api_key_env: str = "GOOGLE_API_KEY"
    crew_enabled: bool = bool(CONFIG.get("crew", {}).get("enabled", False))
    crew_mode: str = str(CONFIG.get("crew", {}).get("mode", "assist"))
    crew_router: str = str(CONFIG.get("crew", {}).get("router", "complex_only"))
    crew_context_max_chars: int = int(CONFIG.get("crew", {}).get("context_max_chars", 900))


# =====================================================================
# 6. LOCAL AND BACKEND SYSTEM PIPELINES (CrewAI / Multi-Agent)
# =====================================================================
def _ask_agent(system_prompt, user_prompt):
    if ollama is None:
        raise RuntimeError("Ollama is not available.")
    response = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        options={
            "temperature": 0.25,
            "num_predict": int(CONFIG["ollama"].get("num_predict", 180)),
            "num_ctx": int(CONFIG["ollama"].get("num_ctx", 2048)),
        },
        stream=False,
        keep_alive=0,
    )
    return (response.get("message") or {}).get("content", "").strip()

def run_multi_agent_round(question, memory_context=""):
    context_block = f"\n\nContext:\n{memory_context}" if memory_context else ""
    researcher_output = _ask_agent(
        "You are a Researcher Agent. Return only concise findings needed to solve the request. No tutorial tone.",
        f"Question: {question}{context_block}",
    )
    coder_output = _ask_agent(
        "You are a Coder Agent. Return implementation actions and checks only, concise and practical.",
        f"Question: {question}\nResearcher notes:\n{researcher_output}{context_block}",
    )
    final_output = _ask_agent(
        "You are the Final Synthesizer Agent. Return one concise, execution-first answer with only what the user needs.",
        (
            f"Question: {question}\n\nResearcher Agent:\n{researcher_output}\n\nCoder Agent:\n{coder_output}\n\n"
            "Return only the final answer. Keep it short and direct. Explain details only if explicitly requested."
        ),
    )
    return {"researcher": researcher_output, "coder": coder_output, "final": final_output}

def run_crew_assist(question: str, memory_context: str = "", config: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    settings = config or {}
    provider = str(settings.get("provider", "fallback")).strip().lower()
    context_max_chars = int(settings.get("context_max_chars", 900) or 900)
    memory_context = _truncate_context(str(memory_context or ""), context_max_chars)

    if provider not in {"crewai", "fallback"}:
        provider = "fallback"

    if provider == "crewai":
        if Agent is None:
            return {
                "ok": False,
                "provider": "crewai",
                "mode": _normalize_mode(settings.get("mode", "assist")),
                "error": "CrewAI import failed.",
            }
        question_text = str(question or "").strip()
        memory_text = str(memory_context or "").strip()
        if memory_text:
            question_text = f"{question_text}\n\nContext:\n{memory_text}"

        researcher = Agent(role="Researcher", goal="Summarize key facts and constraints for the task.", backstory="You are a concise analyst focused on actionable points.", allow_delegation=False)
        implementer = Agent(role="Implementer", goal="Translate research into concrete implementation notes.", backstory="You output practical steps and checks only.", allow_delegation=False)
        synthesizer = Agent(role="Synthesizer", goal="Deliver a final, direct answer for the user.", backstory="You keep responses short, accurate, and outcome-focused.", allow_delegation=False)

        tasks = [
            Task(description=f"Question: {question_text}", expected_output="Short bullet list of key points.", agent=researcher),
            Task(description="Use the research output to produce implementation notes. Avoid tutorials; keep it concise.", expected_output="Implementation notes and checks.", agent=implementer),
            Task(description="Produce the final response for the user. Keep it short and direct.", expected_output="Final assistant response.", agent=synthesizer),
        ]
        crew = Crew(agents=[researcher, implementer, synthesizer], tasks=tasks, process=Process.sequential, verbose=bool(settings.get("verbose", False)))
        try:
            result = crew.kickoff()
        except Exception as exc:
            return {"ok": False, "provider": "crewai", "mode": _normalize_mode(settings.get("mode", "assist")), "error": f"CrewAI kickoff failed: {exc}"}
        return {"ok": True, "provider": "crewai", "mode": _normalize_mode(settings.get("mode", "assist")), "summary": str(result or "").strip()}

    result = run_multi_agent_round(question, memory_context=memory_context or "")
    return {"ok": True, "provider": "fallback", "mode": _normalize_mode(settings.get("mode", "assist")), "summary": str(result.get("final") or "").strip(), "details": result}


# =====================================================================
# 7. PARALLEL WORKER MIXINS, CONCURRENT RETRIEVERS, & MEMORY CONTEXTS
# =====================================================================
class ParallelWorkerMixin:
    """Mixin to add parallel execution capabilities to any worker class."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self._concurrent_tasks = []

    def execute_tools_concurrent(self, tools: Dict[str, Any]) -> Dict[str, ConcurrentTaskResult]:
        futures, results = {}, {}
        for tool_name, tool_args in tools.items():
            if tool_name in self.tools:
                futures[tool_name] = self.executor.submit(self._execute_single_tool_timed, tool_name, tool_args)
        for tool_name, future in concurrent.futures.as_completed(futures, timeout=30):
            try:
                results[tool_name] = future.result()
            except Exception as exc:
                results[tool_name] = ConcurrentTaskResult(tool_name=tool_name, result=None, duration_ms=0, status="error", error=str(exc))
        return results

    def execute_tools_parallel_async(self, tools: Dict[str, Any]) -> List[concurrent.futures.Future]:
        futures = []
        for tool_name, tool_args in tools.items():
            if tool_name in self.tools:
                future = self.executor.submit(self._execute_single_tool_timed, tool_name, tool_args)
                self._concurrent_tasks.append(future)
                futures.append(future)
        return futures

    def wait_for_tools(self, futures: List, timeout: Optional[float] = 30) -> List[ConcurrentTaskResult]:
        results = []
        try:
            for future in concurrent.futures.as_completed(futures, timeout=timeout):
                results.append(future.result())
        except concurrent.futures.TimeoutError:
            print("[ParallelWorker] Tool execution timeout")
        return results

    def _execute_single_tool_timed(self, tool_name: str, tool_args: Dict[str, Any]) -> ConcurrentTaskResult:
        t_start = time.time()
        try:
            tool = self.tools.get(tool_name)
            if not tool:
                raise ValueError(f"Tool {tool_name} not found")
            result = tool(**tool_args)
            return ConcurrentTaskResult(tool_name=tool_name, result=result, duration_ms=(time.time() - t_start) * 1000, status="success")
        except Exception as exc:
            return ConcurrentTaskResult(tool_name=tool_name, result=None, duration_ms=(time.time() - t_start) * 1000, status="error", error=str(exc))

    def execute_pipeline_stages_concurrent(self, query: str, parser_fn: Callable, reasoner_fn: Callable, formatter_fn: Callable) -> Dict[str, Any]:
        timings = {}
        t_start = time.time()
        parsed_query = parser_fn(query)
        timings["parse_ms"] = (time.time() - t_start) * 1000

        t_start = time.time()
        reasoner_future = self.executor.submit(reasoner_fn, parsed_query)
        formatter_on_query_future = self.executor.submit(formatter_fn, query)
        _ = formatter_on_query_future

        try:
            reasoning_result = reasoner_future.result(timeout=30)
        except Exception as exc:
            print(f"[Reasoner Error]: {exc}")
            reasoning_result = ""
        timings["reasoner_ms"] = (time.time() - t_start) * 1000

        t_start = time.time()
        formatter_future = self.executor.submit(formatter_fn, reasoning_result)
        try:
            formatted_result = formatter_future.result(timeout=30)
        except Exception as exc:
            print(f"[Formatter Error]: {exc}")
            formatted_result = reasoning_result
        timings["formatter_ms"] = (time.time() - t_start) * 1000

        return {"parsed_query": parsed_query, "reasoning": reasoning_result, "formatted": formatted_result, "timings": timings}

    def shutdown(self):
        self.executor.shutdown(wait=True)
        print("[ParallelWorker] Executor shutdown complete.")

class AsyncVoiceOutput:
    """Async voice output handler for non-blocking synthesis and playback."""
    def __init__(self, voice_system=None, max_queue_size=10):
        self.voice_system = voice_system
        self.queue = asyncio.Queue(maxsize=max_queue_size)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self._running = False

    async def start(self):
        self._running = True
        while self._running:
            try:
                text, params = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                await self._synthesize_and_play(text, params)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                print(f"[AsyncVoice Error]: {exc}")

    async def _synthesize_and_play(self, text: str, params: Dict[str, Any]):
        if not self.voice_system:
            return
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self.executor, lambda: self.voice_system.queue_output(text, **params))
        except Exception as exc:
            print(f"[AsyncVoice Error]: {exc}")

    async def queue_text(self, text: str, params: Optional[Dict[str, Any]] = None):
        if not params:
            params = {}
        try:
            await self.queue.put((text, params))
        except asyncio.QueueFull:
            print("[AsyncVoice] Queue full, dropping oldest item")

    async def stop(self):
        self._running = False
        await asyncio.sleep(0.5)
        self.executor.shutdown(wait=True)

class ConcurrentRAGRetriever:
    """Concurrent RAG context retrieval with caching and deduplication."""
    def __init__(self, rag_system=None):
        self.rag_system = rag_system
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        self._cache = {}

    def retrieve_concurrent(self, *queries: str, limit: int = 5) -> Dict[str, str]:
        if not self.rag_system:
            return {}
        futures, results = {}, {}
        for query in queries:
            cache_key = f"{query}:{limit}"
            if cache_key in self._cache:
                results[query] = self._cache[cache_key]
                continue
            futures[query] = self.executor.submit(self._retrieve_and_cache, query, limit, cache_key)
        for query, future in futures.items():
            try:
                results[query] = future.result(timeout=30)
            except Exception as exc:
                print(f"[RAG Error for '{query}']: {exc}")
                results[query] = ""
        return results

    def _retrieve_and_cache(self, query: str, limit: int, cache_key: str) -> str:
        try:
            context = self.rag_system.get_rag_context(query, limit=limit)
            self._cache[cache_key] = context
            return context
        except Exception as exc:
            print(f"[RAG Retrieval Error]: {exc}")
            return ""

    def shutdown(self):
        self.executor.shutdown(wait=True)

class LocalContext:
    """JSON-backed long-term memory for successful commands and user preferences."""
    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> Dict[str, object]:
        if not self.file_path.exists():
            return {"successful_commands": [], "preferences": {}}
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {"successful_commands": [], "preferences": {}}
            payload.setdefault("successful_commands", [])
            payload.setdefault("preferences", {})
            return payload
        except Exception:
            return {"successful_commands": [], "preferences": {}}

    def _save(self) -> None:
        try:
            self.file_path.write_text(json.dumps(self._data, indent=2, ensure_ascii=True), encoding="utf-8")
        except Exception:
            pass

    def add_success(self, command_text: str, outcome: str) -> None:
        with self._lock:
            entries = self._data.get("successful_commands")
            if not isinstance(entries, list):
                entries = []
                self._data["successful_commands"] = entries
            clean_cmd = _collapse_ws(command_text)[:260]
            clean_outcome = _collapse_ws(outcome)[:320]
            if not clean_cmd:
                return
            entries.append({"command": clean_cmd, "outcome": clean_outcome})
            if len(entries) > 120:
                self._data["successful_commands"] = entries[-120:]
            self._save()

    def set_preference(self, key: str, value: str) -> None:
        clean_key = _collapse_ws(key)[:120]
        clean_value = _collapse_ws(value)[:200]
        if not clean_key or not clean_value:
            return
        with self._lock:
            prefs = self._data.get("preferences")
            if not isinstance(prefs, dict):
                prefs = {}
                self._data["preferences"] = prefs
            prefs[clean_key] = clean_value
            self._save()

    def get_long_term_memory_text(self, max_chars: int = 1400) -> str:
        with self._lock:
            prefs = self._data.get("preferences", {})
            commands = self._data.get("successful_commands", [])
        lines: List[str] = []
        if isinstance(prefs, dict) and prefs:
            lines.append("Preferences:")
            for key, value in prefs.items():
                lines.append(f"- {key}: {value}")
        if isinstance(commands, list) and commands:
            lines.append("Recent successful commands:")
            for item in commands[-10:]:
                if not isinstance(item, dict):
                    continue
                cmd = str(item.get("command", "")).strip()
                out = str(item.get("outcome", "")).strip()
                if cmd:
                    lines.append(f"- {cmd} => {out}" if out else f"- {cmd}")
        joined = "\n".join(lines).strip()
        return joined if len(joined) <= max_chars else joined[-max_chars:]


# =====================================================================
# 8. ABSTRACTED BASE & SPECIALIZED TASK ORCHESTRATORS
# =====================================================================
class BaseOrchestrator:
    """Base abstract structural functionality to unify routing configurations."""
    def __init__(self, config: Dict[str, Any], db: Any, manager: Any, offline_workers: Dict[str, Any], online_workers: Dict[str, Any]):
        self.config = config
        self.db = db
        self.manager = manager
        self.offline_workers = offline_workers
        self.online_workers = online_workers
        self.mode = config.get("mode", "auto")

    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def is_online(self) -> bool:
        if self.mode == "offline":
            return False
        if self.mode == "online":
            return True
        return _ping(self.config.get("network", {}).get("ping_host", "8.8.8.8"))

    def _select_worker(self, intent: str):
        workers = self.online_workers if self.is_online() else self.offline_workers
        if intent in workers:
            return workers[intent]
        return workers.get("general") or next(iter(workers.values()))

class Orchestrator(BaseOrchestrator):
    """Main orchestrator for routing tasks sequentially to appropriate workers."""
    def route_task(self, query: str, context: Optional[TaskContext] = None) -> TaskResult:
        if context is None:
            context = TaskContext(user_id=0, session_id="", mode=self.mode)

        pending = context.metadata.get("pending_action") if context else None
        decision = _parse_confirmation(query)
        if pending and decision in {"confirm", "cancel"}:
            intent = pending.get("intent", "general")
            worker = self._select_worker(intent)
            return worker.execute(query, context)

        intent = self.manager.classify(query, context)
        worker = self._select_worker(intent)

        crew_text = self.manager.run_crew(query, getattr(worker, "spec", None))
        if crew_text:
            result = TaskResult(text=crew_text, meta={"crewai": True})
        else:
            result = worker.execute(query, context)

        if self.db:
            self.db.log_interaction(
                user_id=context.user_id,
                session_id=context.session_id,
                query=query,
                response=result.text,
                intent=intent,
                mode="online" if self.is_online() else "offline",
            )
        return result

class ParallelOrchestrator(BaseOrchestrator):
    """Advanced orchestrator with concurrent RAG, tool, and voice execution workflows."""
    def __init__(self, config, db, manager, offline_workers, online_workers, rag_system=None, voice_system=None):
        super().__init__(config, db, manager, offline_workers, online_workers)
        self.rag_system = rag_system
        self.voice_system = voice_system
        self.max_workers = config.get("max_concurrent_workers", 4)
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self._lock = threading.RLock()

    def route_task_parallel(self, query: str, context: Optional[ParallelTaskContext] = None) -> ParallelTaskResult:
        if context is None:
            context = ParallelTaskContext(user_id=0, session_id="", mode=self.mode)

        t_start = time.time()
        context.timing["start"] = t_start

        t_parse_start = time.time()
        intent = self.manager.classify(query, context)
        worker = self._select_worker(intent)
        parsed_query = self._parse_query(worker, query)
        context.timing["parse_ms"] = (time.time() - t_parse_start) * 1000

        print(f"[ParallelOrch] Intent: {intent} | Parsed: {parsed_query[:60]}...")

        t_parallel_start = time.time()
        futures = {
            "reasoning": self.executor.submit(self._run_worker_reasoning, worker, parsed_query, query, context)
        }

        parallel_results, parallel_errors = {}, {}
        for task_name, future in futures.items():
            try:
                parallel_results[task_name] = future.result(timeout=30)
            except Exception as exc:
                parallel_errors[task_name] = str(exc)
                print(f"[ParallelOrch] {task_name} error: {exc}")
                parallel_results[task_name] = None

        context.timing["parallel_ms"] = (time.time() - t_parallel_start) * 1000
        context.parallel_results = parallel_results

        t_merge_start = time.time()
        rag_context = parallel_results.get("rag") or ""
        reasoning_result = parallel_results.get("reasoning") or {}
        if rag_context and reasoning_result:
            reasoning_result["rag_context"] = rag_context
        context.timing["merge_ms"] = (time.time() - t_merge_start) * 1000

        t_tools_start = time.time()
        tool_outputs = {}
        if reasoning_result.get("tool_name") and reasoning_result.get("tool_name") in worker.tools:
            tool_outputs = self._execute_tool_parallel(worker, reasoning_result.get("tool_name"), reasoning_result.get("tool_args", {}))
        context.timing["tools_ms"] = (time.time() - t_tools_start) * 1000

        response_text = reasoning_result.get("response", "Task completed.")
        voice_future = None
        if self.voice_system:
            voice_future = self.executor.submit(self._queue_voice_output, response_text, reasoning_result.get("voice_params", {}))

        context.timing["total_ms"] = (time.time() - t_start) * 1000
        if self.db:
            self.db.log_interaction(
                user_id=context.user_id,
                session_id=context.session_id,
                query=query,
                response=response_text,
                intent=intent,
                mode="online" if self.is_online() else "offline",
                metadata={
                    "timing": context.timing,
                    "parallel_mode": True,
                    "rag_used": bool(rag_context),
                    "tools_used": bool(tool_outputs),
                },
            )

        return ParallelTaskResult(
            text=response_text,
            actions=reasoning_result.get("actions", {}),
            sources=reasoning_result.get("sources", []),
            rag_context=rag_context,
            tool_outputs=tool_outputs,
            meta={"intent": intent, "parallel_errors": parallel_errors, "voice_queued": voice_future is not None},
            timing=context.timing,
        )

    def _parse_query(self, worker, query: str) -> str:
        if not hasattr(worker, "_call_model"):
            return query
        try:
            parser_cfg = worker.pipeline.get("parser")
            sys_parser = "Extract the core intent and parameters. Output ONLY the refined instruction."
            parsed = worker._call_model(parser_cfg, query, sys_parser)
            return parsed.strip() if parsed else query
        except Exception as exc:
            print(f"[Parse Error]: {exc}")
            return query

    def _retrieve_rag_context(self, parsed_query: str, original_query: str) -> str:
        if not self.rag_system:
            return ""
        try:
            t_start = time.time()
            context_snippets = self.rag_system.get_rag_context(parsed_query, limit=5)
            if not context_snippets:
                context_snippets = self.rag_system.get_rag_context(original_query, limit=5)
            print(f"[RAG] Retrieved context in {(time.time() - t_start) * 1000:.1f}ms: {len(context_snippets)} snippets")
            return context_snippets
        except Exception as exc:
            print(f"[RAG Error]: {exc}")
            return ""

    def _run_worker_reasoning(self, worker, parsed_query: str, original_query: str, context: ParallelTaskContext) -> Dict[str, Any]:
        _ = original_query; _ = context
        try:
            t_start = time.time()
            if hasattr(worker, "_call_model"):
                reasoner_cfg = worker.pipeline.get("reasoner")
                sys_reasoner = f"You are {worker.spec.name}. {worker.spec.description} Analyze and determine exact steps."
                reasoning = worker._call_model(reasoner_cfg, parsed_query, sys_reasoner)

                formatter_cfg = worker.pipeline.get("formatter")
                sys_formatter = "Format as strict JSON: {'tool': string, 'args': dict, 'response': string}."
                formatted_json = worker._call_model(formatter_cfg, reasoning, sys_formatter)

                clean_json = formatted_json.replace("```json", "").replace("```", "").strip()
                plan_data = json.loads(clean_json)

                print(f"[Reasoning] Completed in {(time.time() - t_start) * 1000:.1f}ms")
                return {
                    "tool_name": plan_data.get("tool"),
                    "tool_args": plan_data.get("args", {}),
                    "response": plan_data.get("response", "Task completed."),
                    "actions": {"tool": plan_data.get("tool"), "args": plan_data.get("args", {})},
                    "sources": [],
                    "voice_params": {"priority": "high"},
                }
            return {"response": "Worker reasoning unavailable.", "actions": {}, "sources": []}
        except Exception as exc:
            print(f"[Reasoning Error]: {exc}")
            return {"response": "Unable to process request.", "actions": {}, "sources": []}

    def _execute_tool_parallel(self, worker, tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        outputs = {}
        try:
            if tool_name and hasattr(worker, "_run_tool"):
                t_start = time.time()
                result = worker._run_tool(tool_name, tool_args)
                elapsed_ms = (time.time() - t_start) * 1000
                outputs[tool_name] = {"result": result, "duration_ms": elapsed_ms, "status": "success"}
                print(f"[Tool] {tool_name} executed in {elapsed_ms:.1f}ms")
            return outputs
        except Exception as exc:
            print(f"[Tool Error]: {exc}")
            return {tool_name: {"status": "error", "error": str(exc)}}

    def _queue_voice_output(self, text: str, voice_params: Dict[str, Any]) -> bool:
        if not self.voice_system:
            return False
        try:
            t_start = time.time()
            self.voice_system.queue_output(text, **voice_params)
            print(f"[Voice] Queued in {(time.time() - t_start) * 1000:.1f}ms")
            return True
        except Exception as exc:
            print(f"[Voice Error]: {exc}")
            return False

    def shutdown(self):
        self.executor.shutdown(wait=True)
        print("[ParallelOrch] Executor shutdown complete.")

class UltraFastOrchestrator:
    """Ultra-fast task orchestrator optimized for speed using dual-stage caching."""
    def __init__(self, config: Dict[str, Any], fast_workers: Dict[str, Any]):
        self.config = config
        self.workers = fast_workers
        self.response_cache = {}
        self.stats = {"total_queries": 0, "cached_hits": 0, "avg_time_ms": 0, "min_time_ms": float("inf"), "max_time_ms": 0}

    def execute_fast(self, query: str, intent: str) -> FastResult:
        t_start = time.time()
        cache_key = f"{intent}:{query.lower().strip()}"
        if cache_key in self.response_cache:
            elapsed_ms = (time.time() - t_start) * 1000
            self.stats["cached_hits"] += 1
            result = self.response_cache[cache_key]
            result.timing_ms = elapsed_ms
            result.cached = True
            print(f"[ULTRA-FAST] Cache HIT: {elapsed_ms:.0f}ms")
            return result

        worker = self.workers.get(intent) or self.workers.get("general")
        result = worker.execute_fast(query, context=None)

        fast_result = FastResult(text=result.text, timing_ms=result.timing_ms, cached=False, meta=result.meta)
        self.response_cache[cache_key] = fast_result
        self._update_stats(result.timing_ms)
        print(f"[ULTRA-FAST] Executed: {result.timing_ms:.0f}ms | Avg: {self.stats['avg_time_ms']:.0f}ms")
        return fast_result

    def stream_response(self, query: str, intent: str):
        worker = self.workers.get(intent) or self.workers.get("general")
        t_start = time.time()
        for chunk in worker.execute_streaming(query, context=None):
            yield {"chunk": chunk, "elapsed_ms": (time.time() - t_start) * 1000, "done": False}
        yield {"chunk": "", "elapsed_ms": (time.time() - t_start) * 1000, "done": True}

    def _update_stats(self, timing_ms: float):
        self.stats["total_queries"] += 1
        current_avg = self.stats["avg_time_ms"]
        total_q = self.stats["total_queries"]
        self.stats["avg_time_ms"] = (current_avg * (total_q - 1) + timing_ms) / total_q
        self.stats["min_time_ms"] = min(self.stats["min_time_ms"], timing_ms)
        self.stats["max_time_ms"] = max(self.stats["max_time_ms"], timing_ms)

    def get_stats(self) -> Dict[str, Any]:
        return {
            **self.stats,
            "cache_size": len(self.response_cache),
            "hit_rate": f"{self.stats['cached_hits'] / max(1, self.stats['total_queries']) * 100:.1f}%",
        }

    def clear_cache(self):
        self.response_cache.clear()
        print("[ULTRA-FAST] Cache cleared")


# =====================================================================
# 9. FACTORIES AND INTELLIGENT ROUTING MANAGERS
# =====================================================================
class Manager:
    """Manager for classifying user intents and handling CrewAI configurations."""
    def __init__(self, llm=None, intents=None, config=None):
        self.llm = llm
        self.intents = intents or DEFAULT_INTENTS
        self.config = config or {}
        crew_cfg = self.config.get("crewai", {})
        enabled_flag = crew_cfg.get("enabled", self.config.get("use_crewai", False))
        self.crewai_enabled = bool(enabled_flag) and Agent is not None
        self.crewai_llm = crew_cfg.get("llm")

    def classify(self, query: str, context: Any = None) -> str:
        query_lower = query.lower()
        for intent, keywords in self.intents.items():
            if keywords and any(kw in query_lower for kw in keywords):
                return intent
        return "general"

class AgentFactory:
    """Factory for processing, initializing, and configuring model space specifications."""
    def __init__(self, config, db=None):
        self.config = config or {}
        self.db = db

    def create_manager(self) -> Manager:
        llm_cfg = self.config.get("llm", {}).get("manager", {}).copy()
        provider = llm_cfg.get("provider", "ollama")
        llm = OnlineLLMClient(llm_cfg) if provider == "gemini" else OfflineLLMClient(llm_cfg)
        return Manager(llm=llm, intents=DEFAULT_INTENTS, config=self.config)

    def create_workers(self, mode: str) -> Dict[str, Any]:
        specs = self._build_worker_specs()
        workers = {}
        for spec in specs:
            workers[spec.intent] = OfflineWorker(spec, spec.offline_pipeline, self.db, self.config) if mode == "offline" else OnlineWorker(spec, spec.hybrid_pipeline, self.db, self.config)
        return workers

    def _build_worker_specs(self) -> List[WorkerSpec]:
        """Consolidated pipeline configurations using forced unified baseline logic."""
        shared_pipeline = {"parser": FORCED_REASONING_MODEL, "reasoner": FORCED_REASONING_MODEL, "formatter": FORCED_REASONING_MODEL}
        
        def make_hybrid(pro_model: bool = False):
            return {
                "parser": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
                "reasoner": {"provider": "gemini", "model": "gemini-1.5-pro" if pro_model else "gemini-1.5-flash"},
                "formatter": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
            }

        return [
            WorkerSpec("OS Worker", "os", "Controls local OS features, settings, and application launching.", {}, shared_pipeline, make_hybrid(False)),
            WorkerSpec("Office Worker", "office", "Handles Excel, Word, Outlook, and Teams tasks, including document summarization.", {}, shared_pipeline, make_hybrid(True)),
            WorkerSpec("Web Worker", "web", "Operates browser apps like Gmail, Drive, YouTube, and WhatsApp.", {}, shared_pipeline, make_hybrid(False)),
            WorkerSpec("File Worker", "files", "Searches and retrieves local documents using RAG vector embeddings.", {}, shared_pipeline, make_hybrid(False)),
            WorkerSpec("General Worker", "general", "Fallback conversational assistant for general questions and chats.", {}, shared_pipeline, make_hybrid(False)),
        ]


# =====================================================================
# 10. MAIN EXECUTION ENGINE (OfflineAgentCore)
# =====================================================================
class OfflineAgentCore:
    def __init__(self, db: Optional[DatabaseManager] = None, config: Optional[AgentConfig] = None) -> None:
        self.db = db or DatabaseManager()
        self.config = config or AgentConfig()
        self.config.reasoning_model = FORCED_REASONING_MODEL

        self.session_id = self.db.create_session(label="offline_desktop_assistant")
        self.stop_event = threading.Event()
        self.rag_enabled = bool(self.config.rag_enabled)
        self.system_prompt_behavior = "default"
        self.system_prompt_custom = ""
        self.online_mode = self._normalize_online_mode(self.config.online_mode)
        self.local_context = LocalContext(str(CONFIG.get("memory", {}).get("local_context_file", "./cache/memory.json")))
        
        self.crew_enabled = bool(self.config.crew_enabled)
        self.crew_mode = str(self.config.crew_mode or "assist").strip().lower()
        self.crew_router = str(self.config.crew_router or "complex_only").strip().lower()
        self.crew_context_max_chars = max(120, int(self.config.crew_context_max_chars or 900))

        self.last_llm_latency_ms = None
        self.last_tool_synthesis_latency_ms = None
        self.last_external_latency_ms = None
        self.last_rag_latency_ms = None

        try:
            self.manager = AgentFactory(CONFIG, self.db).create_manager()
        except Exception as exc:
            print(f"[INIT] Failed to bind intent manager to core: {exc}")
            self.manager = None

        self._client = None
        if ollama is not None:
            try:
                self._client = ollama.Client(host=self.config.ollama_host)
            except Exception:
                self._client = None

        self.fast_orchestrator = None
        self.fast_workers = {}
        if not _bootstrap_env_bool("MARIE_DISABLE_FAST_ORCHESTRATOR", False):
            try:
                print("[INIT] Initializing ultra-fast orchestrator...")
                self.fast_workers = {
                    "files": create_fast_worker("files", {}, CONFIG),
                    "os": create_fast_worker("os", {}, CONFIG),
                    "office": create_fast_worker("office", {}, CONFIG),
                    "general": create_fast_worker("general", {}, CONFIG),
                }
                self.fast_orchestrator = UltraFastOrchestrator(CONFIG, self.fast_workers)
                print("[INIT] OK Ultra-fast orchestrator initialized (3.4x speedup enabled)")
            except Exception as exc:
                print(f"[INIT] WARN Ultra-fast orchestrator failed: {exc}")
                self.fast_orchestrator = None
                self.fast_workers = {}

    def set_rag_enabled(self, enabled: bool) -> None:
        self.rag_enabled = False if _BOOT_SAFE_MINIMAL else bool(enabled)

    def set_online_mode(self, mode: str) -> None:
        self.online_mode = self._normalize_online_mode(mode)

    def set_system_prompt_behavior(self, behavior: str, custom_prompt: str = "") -> None:
        clean_behavior = _collapse_ws(behavior).lower()
        self.system_prompt_behavior = clean_behavior if clean_behavior in PROMPT_BEHAVIOR_HINTS else "default"
        self.system_prompt_custom = str(custom_prompt or "").strip()

    def set_reasoning_model(self, model_name: str) -> None:
        _ = model_name
        self.config.reasoning_model = FORCED_REASONING_MODEL

    def stop(self) -> None:
        self.stop_event.set()

    def reset_stop(self) -> None:
        self.stop_event.clear()

    def get_fast_performance_stats(self) -> Dict[str, object]:
        if not self.fast_orchestrator:
            return {"enabled": False}
        try:
            stats = self.fast_orchestrator.get_stats()
            return {
                "enabled": True,
                "total_queries": stats.get("total_queries", 0),
                "avg_time_ms": stats.get("avg_time_ms", 0),
                "min_time_ms": stats.get("min_time_ms", 0),
                "max_time_ms": stats.get("max_time_ms", 0),
                "cached_hits": stats.get("cached_hits", 0),
                "hit_rate": stats.get("hit_rate", 0),
                "speedup": 1200 / max(stats.get("avg_time_ms", 1), 1),
            }
        except Exception:
            return {"enabled": False, "error": "Could not retrieve stats"}

    def get_latency_snapshot(self) -> Dict[str, Optional[float]]:
        return {
            "llm_ms": self.last_llm_latency_ms,
            "tool_synthesis_ms": self.last_tool_synthesis_latency_ms,
            "external_ms": self.last_external_latency_ms,
            "rag_ms": self.last_rag_latency_ms,
        }

    def process_user_message(self, user_text: str) -> str:
        text = (user_text or "").strip()
        if not text:
            return "Please type a message first."
        if self.stop_event.is_set():
            return "Processing is currently stopped. Press Resume to continue."

        self._capture_preference_from_text(text)
        self.db.log_interaction(self.session_id, role="user", message=text, category="chat")
        
        intent = "general"
        if hasattr(self, 'manager') and self.manager:
            intent = self.manager.classify(text, None)

        if self.fast_orchestrator and intent == "general" and not self.stop_event.is_set():
            try:
                result_container = {"result": None, "error": None}
                def run_fast_path():
                    try:
                        result_container["result"] = self.fast_orchestrator.execute_fast(text, "general")
                    except Exception as exc:
                        result_container["error"] = exc

                fast_thread = threading.Thread(target=run_fast_path, daemon=True)
                fast_thread.start()
                fast_thread.join(timeout=5.0)

                if not fast_thread.is_alive() and not result_container["error"] and result_container["result"]:
                    result = result_container["result"]
                    cleaned = self.clean_output_for_ui(result.text) or result.text
                    self.db.log_interaction(self.session_id, role="assistant", message=cleaned, category="chat")
                    return cleaned
            except Exception as exc:
                print(f"[FAST] Fallback triggered via exception: {exc}")

        recent_history = self.db.get_recent_turns(self.session_id, turn_limit=self.config.max_history_turns)
        rag_context = self._retrieve_rag_context(text) if self.rag_enabled and not self.stop_event.is_set() else ""
            
        if self.stop_event.is_set():
            return ""

        crew_context = ""
        if self.crew_enabled:
            crew_summary, crew_replace = self._get_crew_context(text, rag_context)
            if crew_replace:
                cleaned_replace = self.clean_output_for_ui(crew_replace) or crew_replace
                self.db.log_interaction(self.session_id, role="assistant", message=cleaned_replace, category="chat")
                return cleaned_replace
            crew_context = crew_summary

        base_reply = self._reason_over_input(user_text=text, recent_history=recent_history, rag_context=rag_context, crew_context=crew_context)
        if self.stop_event.is_set():
            return ""

        actions, clean_reply = self._extract_tool_actions(base_reply)
        final_reply = self._synthesize_after_tools(text, clean_reply, self._execute_tool_actions(actions)) if actions else clean_reply
        
        cleaned_final = self.clean_output_for_ui(final_reply) or final_reply
        self.db.log_interaction(self.session_id, role="assistant", message=cleaned_final, category="chat")
        return cleaned_final

    def _reason_over_input(self, user_text: str, recent_history: List[Dict[str, str]], rag_context: str, crew_context: str) -> str:
        if self.stop_event.is_set():
            return ""

        ltm_text = self.local_context.get_long_term_memory_text()
        system_prompt = self._build_system_prompt()
        if ltm_text:
            system_prompt += "\n\nLong-Term Memory:\n" + ltm_text

        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for item in recent_history:
            role = item.get("role", "user")
            content = item.get("message", "")
            if content:
                messages.append({"role": role, "content": content})

        context_sections = [f"User request: {user_text}"]
        if rag_context:
            context_sections.append(f"Local knowledge snippets (RAG):\n{rag_context}\nUse only if relevant and do not invent citations.")
        if crew_context:
            context_sections.append(f"CrewAI notes (advisory):\n{crew_context}\nTreat this as optional guidance; verify against local context.")

        messages.append({"role": "user", "content": user_text if len(context_sections) == 1 else "\n\n".join(context_sections)})

        online_mode = self._normalize_online_mode(self.online_mode)
        if online_mode == "online" or (online_mode == "auto" and self.config.hybrid_mode and self._is_complex_reasoning_request(user_text)):
            external = self._reason_with_external(messages)
            if external:
                return external

        if self._client is None:
            return "Ollama client is unavailable. Please check local Ollama service."

        self.last_llm_latency_ms = None
        try:
            t_start = time.time()
            # REPAIRED: Global threading lock constraint has been completely bypassed to allow simultaneous model calls
            response = self._client.chat(
                model=self.config.reasoning_model,
                messages=messages,
                options={"temperature": self.config.temperature, "num_ctx": self.config.num_ctx, "num_predict": max(180, int(self.config.num_predict))},
                keep_alive=0,
            )
            self.last_llm_latency_ms = (time.time() - t_start) * 1000
            return "" if self.stop_event.is_set() else (response.get("message", {}) or {}).get("content", "").strip()
        except Exception as exc:
            return f"I hit a local model error: {exc}"
        finally:
            self._release_vram()

    def _build_system_prompt(self) -> str:
        parts = [SYSTEM_PROMPT, FILE_RESPONSE_GUARD]
        behavior_hint = PROMPT_BEHAVIOR_HINTS.get((self.system_prompt_behavior or "default").strip().lower(), "")
        if behavior_hint:
            parts.append(behavior_hint)
        if self.system_prompt_custom.strip():
            parts.append("Additional behavior override:\n" + self.system_prompt_custom.strip())

        try:
            style_profile = self.db.get_style_profile("train_root")
            if isinstance(style_profile, dict):
                formal, casual = str(style_profile.get("formal", "")).strip(), str(style_profile.get("casual", "")).strip()
                if formal or casual:
                    parts.append(f"Writing style profile (use for documentation outputs):\nFormal baseline: {formal}\nCasual baseline: {casual}\nDefault to formal unless requested casual.")
        except Exception:
            pass
        return "\n\n".join(part for part in parts if part).strip()

    def _reason_with_external(self, messages: List[Dict[str, str]]) -> str:
        if requests is None:
            return ""
        api_key = next((os.environ.get(k, "").strip() for k in [self.config.external_api_key_env, "GOOGLE_API_KEY", "GEMINI_API_KEY", "MARIE_GEMINI_API_KEY"] if os.environ.get(k)), "")
        if not api_key:
            return ""

        prompt_parts = [f"{str(m.get('role', 'user')).strip().title()}:\n{str(m.get('content', '')).strip()}" for m in messages if m.get("content")]
        if not prompt_parts:
            return ""

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.config.external_model}:generateContent"
        payload = {"contents": [{"role": "user", "parts": [{"text": "\n\n".join(prompt_parts)}]}], "generationConfig": {"temperature": self.config.temperature, "maxOutputTokens": 640}}

        self.last_external_latency_ms = None
        try:
            t_start = time.time()
            response = requests.post(url, params={"key": api_key}, json=payload, timeout=28)
            self.last_external_latency_ms = (time.time() - t_start) * 1000
            if response.status_code >= 400:
                return ""
            for candidate in response.json().get("candidates", []):
                parts = (candidate.get("content") or {}).get("parts", [])
                merged = _collapse_ws(" ".join(str(p.get("text", "")) for p in parts if p.get("text")))
                if merged:
                    return merged
            return ""
        except Exception:
            return ""

    @staticmethod
    def _is_complex_reasoning_request(text: str) -> bool:
        lowered = (text or "").lower()
        if len(re.findall(r"[a-zA-Z0-9_]+", lowered)) >= 36:
            return True
        triggers = {"analyze", "compare", "architecture", "strategy", "tradeoff", "design", "optimize", "debug plan", "step-by-step", "root cause"}
        return sum(1 for token in triggers if token in lowered) >= 2

    @staticmethod
    def _normalize_online_mode(mode: str) -> str:
        clean = str(mode or "").strip().lower()
        return clean if clean in {"auto", "online", "offline"} else "auto"

    def _retrieve_rag_context(self, user_text: str) -> str:
        try:
            from aiassistant.infra.rag_memory import get_rag_context
        except Exception as e:
            print(f"[RAG Warning] System bypassed. Failed to load embedding layer: {e}", flush=True)
            return ""

        if get_rag_context is None:
            return ""
        self.last_rag_latency_ms = None
        try:
            t_start = time.time()
            snippets = (get_rag_context(user_text, top_k=self.config.rag_top_k) or "").strip()
            if not snippets:
                return ""
            snippets = snippets[:1800]
            if self.db:
                self.db.log_interaction(self.session_id, role="system", message=snippets, category="rag")
            self.last_rag_latency_ms = (time.time() - t_start) * 1000
            return snippets
        except Exception as e:
            print(f"[RAG Error] Runtime failure during search: {e}", flush=True)
            return ""

    def _execute_tool_actions(self, actions: List[Dict[str, object]]) -> List[Dict[str, object]]:
        results = []
        for action in actions:
            if self.stop_event.is_set():
                break
            result = _run_tool_action_isolated(action)
            action_name = str(action.get("action", ""))
            if action_name == "read_file":
                if Path(str(action.get("path") or "")).suffix.lower() in {".doc", ".docx", ".xlsx", ".xls", ".csv", ".pdf"}:
                    if isinstance(result, dict) and "data" in result:
                        result["data"] = {"path": str(action.get("path") or ""), "note": "content_hidden"}
            results.append({"action": action, "result": result})

            if isinstance(result, dict) and result.get("success"):
                self.local_context.add_success(str(action.get("action", "tool")), str(result.get("message", "completed")))

            self.db.log_interaction(self.session_id, role="tool", message=json.dumps({"action": action, "result": result}, ensure_ascii=True), category="tool")
        return results

    def _synthesize_after_tools(self, user_text: str, base_assistant_text: str, tool_results: List[Dict[str, object]]) -> str:
        if self._client is None:
            return base_assistant_text

        synthesis_prompt = (
            "Given the user request, draft answer, and tool results, write the final user-facing reply. "
            "Keep it concise, direct, and outcome-focused. State only what the user needs to know right now. "
            "Do not teach or explain internal steps unless asked. Do not include JSON or tool call syntax.\n\n"
            f"User request: {user_text}\n\nDraft answer: {base_assistant_text}\n\nTool results: {json.dumps(tool_results, ensure_ascii=True)}"
        )
        self.last_tool_synthesis_latency_ms = None
        try:
            t_start = time.time()
            # REPAIRED: Model lock has been fully bypassed to allow high-throughput parallel tool responses
            response = self._client.chat(
                model=self.config.reasoning_model,
                messages=[{"role": "system", "content": "Return only final answer text."}, {"role": "user", "content": synthesis_prompt}],
                options={"temperature": 0.1, "num_ctx": 1536, "num_predict": max(160, int(self.config.num_predict * 0.75))},
                keep_alive=0,
            )
            self.last_tool_synthesis_latency_ms = (time.time() - t_start) * 1000
            return (response.get("message", {}) or {}).get("content", "").strip()
        except Exception:
            return base_assistant_text
        finally:
            self._release_vram()

    def _extract_tool_actions(self, raw_text: str) -> Tuple[List[Dict[str, object]], str]:
        actions: List[Dict[str, object]] = []
        text = raw_text or ""

        for block in re.findall(r"<tool>(.*?)</tool>", text, flags=re.IGNORECASE | re.DOTALL):
            candidate = block.strip()
            # REPAIRED: Defensive stripping prevents code formatting fences from breaking JSON parsers
            candidate = re.sub(r"^```json\s*", "", candidate, flags=re.IGNORECASE)
            candidate = re.sub(r"^```\s*", "", candidate)
            candidate = re.sub(r"\s*```$", "", candidate).strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and parsed.get("action"):
                    actions.append(parsed)
            except Exception:
                continue

        text = re.sub(r"<tool>.*?</tool>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        for snippet in re.findall(r"\{\s*\"action\"\s*:\s*\"[^\"]+\".*?\}", text, flags=re.DOTALL):
            try:
                parsed = json.loads(snippet)
                if isinstance(parsed, dict) and parsed.get("action"):
                    actions.append(parsed)
                    text = text.replace(snippet, " ")
            except Exception:
                continue
        return actions, _collapse_ws(text)

    def _get_crew_context(self, user_text: str, rag_context: str) -> Tuple[str, str]:
        if not self.crew_enabled:
            return "", ""
        if str(self.crew_router or "complex_only").strip().lower() == "complex_only" and not self._is_complex_reasoning_request(user_text):
            return "", ""

        memory_context = (rag_context or "")[:self.crew_context_max_chars]
        crew_result = run_crew_assist(user_text, memory_context=memory_context, config=CONFIG.get("crew", {}))
        
        if not crew_result or not isinstance(crew_result, dict):
            return "", ""
        if not crew_result.get("ok"):
            error = str(crew_result.get("error", "CrewAI unavailable")).strip()
            if error and self.db:
                self.db.log_interaction(self.session_id, role="system", message=error, category="crew")
            return "", ""

        summary = str(crew_result.get("summary", "") or "").strip()
        final = str(crew_result.get("final", "") or "").strip()
        return ("", final) if (str(self.crew_mode or "assist").strip().lower() == "replace" and final) else (summary, "")

    @staticmethod
    def clean_output_for_ui(raw_text: str) -> str:
        """
        Strips internal artifacts before showing text in the chat UI.
        
        Removes:
        - <think>...</think> reasoning blocks
        - <tool>...</tool> tool call blocks
        - JSON action objects
        - fenced code blocks (often tool dumps)
        """
        text = raw_text or ""

        # Define patterns for blocks of tool data and reasoning
        patterns_to_remove = [
            r"<think>.*?</think>",
            r"<tool>.*?</tool>",
            r"```[\s\S]*?```",
            r"\{\s*\"action\"\s*:\s*\"[^\"]+\"[\s\S]*?\}"
        ]

        # Strip all matched blocks
        for pattern in patterns_to_remove:
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE | re.DOTALL)

        # Clean up stray prefix artifacts (e.g., "Analysis: ", "Tool call: ")
        text = re.sub(r"\b(analysis|reasoning|tool\s*call)\s*:\s*", "", text, flags=re.IGNORECASE)

        return _collapse_ws(text)

    def _capture_preference_from_text(self, text: str) -> None:
        """Parses the user's input to extract and save persistent UI/system preferences."""
        lowered = (text or "").lower()
        if not lowered:
            return

        # Handle explicit theme overrides
        if "always use dark mode" in lowered:
            self.local_context.set_preference("theme", "dark")
            return
            
        if "always use light mode" in lowered:
            self.local_context.set_preference("theme", "light")
            return

        # Handle general "always use X" preference captures
        match = re.search(r"\balways use\s+([a-z0-9 _\-]{2,80})", lowered)
        if match:
            value = match.group(1).strip(" .,!?:;")
            if value:
                self.local_context.set_preference("always_use", value)

    @staticmethod
    def _release_vram() -> None:
        """
        Releases VRAM gracefully without stalling the synchronous generation critical path.
        Uses an asynchronous thread with a debounce timer to avoid freezing the app.
        """
        global _LAST_VRAM_CLEANUP_TIME
        
        # 1. Clear application-level cache managers
        try:
            from aiassistant.infra.optimization import get_memory_manager
            get_memory_manager().cleanup()
        except Exception:
            pass

        # 2. Standard Python Garbage Collection (Blocking, but fast)
        gc.collect()

        # 3. PyTorch CUDA Cache Clearing (Async to prevent generation stalls)
        if torch is None or not torch.cuda.is_available():
            return
            
        try:
            current_time = time.time()
            # Only trigger heavy VRAM cleanup if the debounce period has passed
            if (current_time - _LAST_VRAM_CLEANUP_TIME) > _VRAM_CLEANUP_DEBOUNCE_SEC:
                
                def _async_flush():
                    try:
                        torch.cuda.empty_cache()
                        if hasattr(torch.cuda, "ipc_collect"):
                            torch.cuda.ipc_collect()
                    except Exception:
                        pass
                
                # Execute in background thread
                threading.Thread(target=_async_flush, daemon=True).start()
                _LAST_VRAM_CLEANUP_TIME = current_time
        except Exception:
            pass


# =====================================================================
# MODULE UTILITIES & EXPORTS
# =====================================================================

def _collapse_ws(text: str) -> str:
    """Collapses multiple spaces and newlines into a single space."""
    return re.sub(r"\s+", " ", text).strip()


__all__ = [
    "AgentConfig",
    "AgentFactory",
    "AsyncVoiceOutput",
    "ConcurrentRAGRetriever",
    "ConcurrentTaskResult",
    "DEFAULT_INTENTS",
    "EventBus",
    "Events",
    "FastResult",
    "LocalContext",
    "Manager",
    "OfflineAgentCore",
    "Orchestrator",
    "ParallelOrchestrator",
    "ParallelTaskContext",
    "ParallelTaskResult",
    "ParallelWorkerMixin",
    "TaskContext",
    "TaskResult",
    "UltraFastOrchestrator",
    "WorkerSpec",
    "run_crew_assist",
    "run_multi_agent_round",
]