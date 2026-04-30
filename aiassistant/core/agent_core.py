"""
Core autonomous agent loop for a fully local desktop assistant.

Key architecture decisions for GTX 1660 6GB:
- Calls to Ollama use keep_alive=0 so each model unloads after response.
- Vision and reasoning model calls are sequential, never parallel, to reduce VRAM spikes.
- Prompt context is intentionally short (last 3 turns) for speed and lower memory use.
"""

from __future__ import annotations

import gc
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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


_BOOT_STABILITY_MODE_LEVEL = max(0, _bootstrap_env_int("MARIE_STABILITY_MODE_LEVEL", 0))
_BOOT_DISABLE_SCREEN_CAPTURE = _bootstrap_env_bool("MARIE_DISABLE_SCREEN_CAPTURE", False)
_BOOT_DISABLE_RAG = _bootstrap_env_bool("MARIE_DISABLE_RAG", False)
_BOOT_SAFE_MINIMAL = _bootstrap_env_bool("MARIE_SAFE_MINIMAL", False)

try:
    import ollama
except Exception:  # pragma: no cover - optional dependency
    ollama = None

try:
    import requests
except Exception:  # pragma: no cover - optional dependency
    requests = None

torch = None
if _BOOT_STABILITY_MODE_LEVEL < 2 and not _BOOT_SAFE_MINIMAL:
    try:
        import torch
    except Exception:  # pragma: no cover - optional dependency
        torch = None

from aiassistant.core.crew_orchestrator import run_crew_assist
from aiassistant.infra.config.app_config import CONFIG
from aiassistant.infra.db.database_manager import DatabaseManager

get_rag_context = None
if not (_BOOT_DISABLE_RAG or _BOOT_SAFE_MINIMAL):
    try:
        from aiassistant.infra.rag_memory import get_rag_context
    except Exception:  # pragma: no cover - optional dependency path
        get_rag_context = None


def capture_screen_base64_jpeg(quality: int = 65):
    _ = quality
    return None


if not (_BOOT_DISABLE_SCREEN_CAPTURE or _BOOT_SAFE_MINIMAL or _BOOT_STABILITY_MODE_LEVEL >= 2):
    try:
        from aiassistant.infra.vision.vision_audio import (
            capture_screen_base64_jpeg as _capture_screen_base64_jpeg,
        )

        capture_screen_base64_jpeg = _capture_screen_base64_jpeg
    except Exception:
        pass


# Process-level defaults. Launcher also injects these into child processes.
os.environ.setdefault("OLLAMA_FLASH_ATTENTION", "1")
os.environ.setdefault("OLLAMA_KV_CACHE_TYPE", "q4_0")
os.environ.setdefault("OLLAMA_KEEP_ALIVE", "0")

FORCED_REASONING_MODEL = "llama3.2:3b"
FORCED_VISION_MODEL = "moondream"
HYBRID_MODE = bool(CONFIG.get("runtime", {}).get("hybrid_mode", False))


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
    "online_query. "
    "Do not include JSON or tool syntax in normal user-facing replies. "
    "For simple requests, answer in easy plain language and keep it direct. "
    "For complex requests, provide enough detail to be useful without over-explaining."
)

PROMPT_BEHAVIOR_HINTS = {
    "default": "",
    "concise": (
        "Prefer concise answers by default. Keep most responses to a few short sentences unless the user asks for detail."
    ),
    "detailed": (
        "Provide richer explanations and rationale when useful. Include structured steps for complex requests."
    ),
    "action_first": (
        "Prioritize concrete actions and outcomes before explanation. Lead with what was done or what should be done next."
    ),
    "custom": "",
}


def _run_tool_action_isolated(action: Dict[str, object], timeout_sec: int = 55) -> Dict[str, object]:
    try:
        payload = json.dumps(action, ensure_ascii=True)
    except Exception as exc:
        return {
            "success": False,
            "message": "Failed to serialize tool action.",
            "error": str(exc),
        }

    command = [
        sys.executable,
        "-m",
        "aiassistant.tools.tool_action_runner",
    ]

    try:
        completed = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            text=True,
            timeout=max(5, int(timeout_sec)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": "Tool action timed out in isolated runner.",
            "error": "runner_timeout",
        }
    except Exception as exc:
        return {
            "success": False,
            "message": "Tool action runner failed to start.",
            "error": str(exc),
        }

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
        return {
            "success": True,
            "message": "Tool action completed in isolated runner.",
            "data": {"output": stdout_text[:1200]},
        }

    details = "\n".join(part for part in [stdout_text, stderr_text] if part).strip()[:1800]
    return {
        "success": False,
        "message": "Tool action failed in isolated runner.",
        "error": details or f"runner_exit_code={completed.returncode}",
    }


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
            self.file_path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=True),
                encoding="utf-8",
            )
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

            entry = {
                "command": clean_cmd,
                "outcome": clean_outcome,
            }
            entries.append(entry)
            # Keep memory compact and bounded.
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
                    if out:
                        lines.append(f"- {cmd} => {out}")
                    else:
                        lines.append(f"- {cmd}")

        joined = "\n".join(lines).strip()
        if len(joined) <= max_chars:
            return joined
        return joined[-max_chars:]


@dataclass
class AgentConfig:
    reasoning_model: str = FORCED_REASONING_MODEL
    vision_model: str = FORCED_VISION_MODEL
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


class OfflineAgentCore:
    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        config: Optional[AgentConfig] = None,
    ) -> None:
        self.db = db or DatabaseManager()
        self.config = config or AgentConfig()

        # Force compact model routing for <=6GB GPUs.
        self.config.reasoning_model = FORCED_REASONING_MODEL
        self.config.vision_model = FORCED_VISION_MODEL

        self.session_id = self.db.create_session(label="offline_desktop_assistant")
        self.stop_event = threading.Event()
        self.screen_capture_enabled = False
        self.rag_enabled = bool(self.config.rag_enabled) and not (_BOOT_DISABLE_RAG or _BOOT_SAFE_MINIMAL)
        self.system_prompt_behavior = "default"
        self.system_prompt_custom = ""
        self.online_mode = self._normalize_online_mode(self.config.online_mode)
        self.local_context = LocalContext(
            str(CONFIG.get("memory", {}).get("local_context_file", "./cache/memory.json"))
        )
        self.crew_enabled = bool(self.config.crew_enabled)
        self.crew_mode = str(self.config.crew_mode or "assist").strip().lower()
        self.crew_router = str(self.config.crew_router or "complex_only").strip().lower()
        self.crew_context_max_chars = max(120, int(self.config.crew_context_max_chars or 900))

        # Single lock ensures model calls happen one-at-a-time.
        self._model_lock = threading.Lock()

        self._client = None
        if ollama is not None:
            try:
                self._client = ollama.Client(host=self.config.ollama_host)
            except Exception:
                self._client = None

    def set_screen_capture_enabled(self, enabled: bool) -> None:
        if _BOOT_DISABLE_SCREEN_CAPTURE or _BOOT_SAFE_MINIMAL or _BOOT_STABILITY_MODE_LEVEL >= 2:
            self.screen_capture_enabled = False
            return
        self.screen_capture_enabled = bool(enabled)

    def set_rag_enabled(self, enabled: bool) -> None:
        if _BOOT_DISABLE_RAG or _BOOT_SAFE_MINIMAL:
            self.rag_enabled = False
            return
        self.rag_enabled = bool(enabled)

    def set_online_mode(self, mode: str) -> None:
        self.online_mode = self._normalize_online_mode(mode)

    def set_system_prompt_behavior(self, behavior: str, custom_prompt: str = "") -> None:
        clean_behavior = _collapse_ws(behavior).lower()
        if clean_behavior not in PROMPT_BEHAVIOR_HINTS:
            clean_behavior = "default"

        self.system_prompt_behavior = clean_behavior
        self.system_prompt_custom = str(custom_prompt or "").strip()

    def set_reasoning_model(self, model_name: str) -> None:
        # Enforced for VRAM safety.
        _ = model_name
        self.config.reasoning_model = FORCED_REASONING_MODEL

    def set_vision_model(self, model_name: str) -> None:
        # Enforced for VRAM safety.
        _ = model_name
        self.config.vision_model = FORCED_VISION_MODEL

    def stop(self) -> None:
        self.stop_event.set()

    def reset_stop(self) -> None:
        self.stop_event.clear()

    def process_user_message(self, user_text: str) -> str:
        """
        Main reasoning entry point.

        Sequence:
        1) Save user message.
        2) Optionally gather screen context via vision model.
        3) Run reasoning model.
        4) Execute tool JSON if present.
        5) Reconcile final answer and clean it for UI display.
        """
        text = (user_text or "").strip()
        if not text:
            return "Please type a message first."

        if self.stop_event.is_set():
            return "Processing is currently stopped. Press Resume to continue."

        self._capture_preference_from_text(text)

        self.db.log_interaction(self.session_id, role="user", message=text, category="chat")

        recent_history = self.db.get_recent_turns(
            self.session_id,
            turn_limit=self.config.max_history_turns,
        )

        screen_context = ""
        if self.screen_capture_enabled and not self.stop_event.is_set():
            screen_context = self._describe_screen_if_available()

        rag_context = ""
        if self.rag_enabled and not self.stop_event.is_set():
            rag_context = self._retrieve_rag_context(text)

        crew_context, crew_final = self._get_crew_context(text, rag_context)
        if crew_final:
            cleaned = self.clean_output_for_ui(crew_final) or crew_final
            self.db.log_interaction(
                self.session_id,
                role="assistant",
                message=cleaned,
                category="chat",
            )
            return cleaned

        base_reply = self._reason_over_input(
            text,
            recent_history,
            screen_context,
            rag_context,
            crew_context,
        )
        if self.stop_event.is_set():
            return "Request cancelled."

        if not base_reply:
            safe_reply = "I could not generate a response right now."
            self.db.log_interaction(self.session_id, role="assistant", message=safe_reply, category="chat")
            return safe_reply

        tool_actions, text_without_tools = self._extract_tool_actions(base_reply)

        final_reply_raw = text_without_tools
        if tool_actions and not self.stop_event.is_set():
            tool_results = self._execute_tool_actions(tool_actions)
            final_reply_raw = self._synthesize_after_tools(
                user_text=text,
                base_assistant_text=text_without_tools,
                tool_results=tool_results,
            )

        final_clean = self.clean_output_for_ui(final_reply_raw)
        if not final_clean:
            final_clean = "Done."

        self.db.log_interaction(
            self.session_id,
            role="assistant",
            message=final_clean,
            category="chat",
        )

        lowered_final = final_clean.lower()
        if "error" not in lowered_final and "unavailable" not in lowered_final:
            self.local_context.add_success(text, final_clean)

        self._release_vram()
        return final_clean

    def _describe_screen_if_available(self) -> str:
        """
        Captures screenshot and asks vision model for concise context.

        keep_alive=0 immediately unloads the vision model after this call,
        freeing VRAM before the reasoning model starts.
        """
        img_b64 = capture_screen_base64_jpeg(quality=55)
        if not img_b64:
            return ""

        if self._client is None:
            return ""

        messages = [
            {
                "role": "user",
                "content": "Describe the current screen in under 80 words, focusing on actionable UI context.",
                "images": [img_b64],
            }
        ]

        try:
            with self._model_lock:
                response = self._client.chat(
                    model=self.config.vision_model,
                    messages=messages,
                    options={"temperature": 0.1, "num_ctx": 1024},
                    keep_alive=0,
                )
            return (response.get("message", {}) or {}).get("content", "").strip()
        except Exception:
            return ""
        finally:
            self._release_vram()

    def _reason_over_input(
        self,
        user_text: str,
        recent_history: List[Dict[str, str]],
        screen_context: str,
        rag_context: str,
        crew_context: str,
    ) -> str:
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

        if screen_context:
            context_sections.append(
                "Live screen context:\n"
                f"{screen_context}\n"
                "Use this only if relevant."
            )

        if rag_context:
            context_sections.append(
                "Local knowledge snippets (RAG):\n"
                f"{rag_context}\n"
                "Use only if relevant and do not invent citations."
            )

        if crew_context:
            context_sections.append(
                "CrewAI notes (advisory):\n"
                f"{crew_context}\n"
                "Treat this as optional guidance; verify against local context."
            )

        if len(context_sections) == 1:
            user_payload = user_text
        else:
            user_payload = "\n\n".join(context_sections)

        messages.append({"role": "user", "content": user_payload})

        online_mode = self._normalize_online_mode(self.online_mode)
        if online_mode == "online":
            external = self._reason_with_external(messages)
            if external:
                return external
        elif online_mode == "auto" and self.config.hybrid_mode and self._is_complex_reasoning_request(user_text):
            external = self._reason_with_external(messages)
            if external:
                return external

        if self._client is None:
            return "Ollama client is unavailable. Please check local Ollama service."

        try:
            with self._model_lock:
                response = self._client.chat(
                    model=self.config.reasoning_model,
                    messages=messages,
                    options={
                        "temperature": self.config.temperature,
                        "num_ctx": self.config.num_ctx,
                        "num_predict": max(180, int(self.config.num_predict)),
                    },
                    keep_alive=0,
                )
            if self.stop_event.is_set():
                return ""
            return (response.get("message", {}) or {}).get("content", "").strip()
        except Exception as exc:
            return f"I hit a local model error: {exc}"
        finally:
            self._release_vram()

    def _build_system_prompt(self) -> str:
        base = SYSTEM_PROMPT
        behavior = (self.system_prompt_behavior or "default").strip().lower()
        behavior_hint = PROMPT_BEHAVIOR_HINTS.get(behavior, "")
        custom = self.system_prompt_custom.strip()

        parts = [base]
        if behavior_hint:
            parts.append(behavior_hint)
        if custom:
            parts.append("Additional behavior override:\n" + custom)

        return "\n\n".join(part for part in parts if part).strip()

    def _reason_with_external(self, messages: List[Dict[str, str]]) -> str:
        if requests is None:
            return ""

        api_key = (
            os.environ.get(self.config.external_api_key_env, "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
            or os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("MARIE_GEMINI_API_KEY", "").strip()
        )
        if not api_key:
            return ""

        prompt_parts: List[str] = []
        for message in messages:
            role = str(message.get("role", "user")).strip().title()
            content = str(message.get("content", "")).strip()
            if content:
                prompt_parts.append(f"{role}:\n{content}")

        if not prompt_parts:
            return ""

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.config.external_model}:generateContent"
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "\n\n".join(prompt_parts)}],
                }
            ],
            "generationConfig": {
                "temperature": self.config.temperature,
                "maxOutputTokens": 640,
            },
        }

        try:
            response = requests.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=28,
            )
            if response.status_code >= 400:
                return ""
            data = response.json()
            for candidate in data.get("candidates", []):
                parts = (candidate.get("content") or {}).get("parts", [])
                text_chunks = [str(part.get("text", "")) for part in parts if part.get("text")]
                merged = _collapse_ws(" ".join(text_chunks))
                if merged:
                    return merged
            return ""
        except Exception:
            return ""

    @staticmethod
    def _is_complex_reasoning_request(text: str) -> bool:
        lowered = (text or "").lower()
        words = re.findall(r"[a-zA-Z0-9_]+", lowered)
        if len(words) >= 36:
            return True

        triggers = {
            "analyze",
            "compare",
            "architecture",
            "strategy",
            "tradeoff",
            "design",
            "optimize",
            "debug plan",
            "step-by-step",
            "root cause",
        }
        hits = sum(1 for token in triggers if token in lowered)
        return hits >= 2

    @staticmethod
    def _normalize_online_mode(mode: str) -> str:
        clean = str(mode or "").strip().lower()
        if clean not in {"auto", "online", "offline"}:
            return "auto"
        return clean

    def _retrieve_rag_context(self, user_text: str) -> str:
        if get_rag_context is None:
            return ""

        try:
            snippets = get_rag_context(user_text, top_k=self.config.rag_top_k) or ""
            snippets = snippets.strip()
            if not snippets:
                return ""

            # Keep retrieval compact for faster generation and lower token usage.
            snippets = snippets[:1800]
            self.db.log_interaction(
                self.session_id,
                role="system",
                message=snippets,
                category="rag",
            )
            return snippets
        except Exception:
            return ""

    def _execute_tool_actions(self, actions: List[Dict[str, object]]) -> List[Dict[str, object]]:
        results = []
        for action in actions:
            if self.stop_event.is_set():
                break
            result = _run_tool_action_isolated(action)
            results.append({"action": action, "result": result})

            if isinstance(result, dict) and result.get("success"):
                action_name = str(action.get("action", "tool"))
                outcome = str(result.get("message", "completed"))
                self.local_context.add_success(action_name, outcome)

            self.db.log_interaction(
                self.session_id,
                role="tool",
                message=json.dumps({"action": action, "result": result}, ensure_ascii=True),
                category="tool",
            )
        return results

    def _synthesize_after_tools(
        self,
        user_text: str,
        base_assistant_text: str,
        tool_results: List[Dict[str, object]],
    ) -> str:
        if self._client is None:
            return base_assistant_text

        synthesis_prompt = (
            "Given the user request, draft answer, and tool results, write the final user-facing reply. "
            "Keep it concise, direct, and outcome-focused. "
            "State only what the user needs to know right now. "
            "Do not teach or explain internal steps unless asked. "
            "Do not include JSON or tool call syntax.\n\n"
            f"User request: {user_text}\n\n"
            f"Draft answer: {base_assistant_text}\n\n"
            f"Tool results: {json.dumps(tool_results, ensure_ascii=True)}"
        )

        messages = [
            {"role": "system", "content": "Return only final answer text."},
            {"role": "user", "content": synthesis_prompt},
        ]

        try:
            with self._model_lock:
                response = self._client.chat(
                    model=self.config.reasoning_model,
                    messages=messages,
                    options={
                        "temperature": 0.1,
                        "num_ctx": 1536,
                        "num_predict": max(160, int(self.config.num_predict * 0.75)),
                    },
                    keep_alive=0,
                )
            return (response.get("message", {}) or {}).get("content", "").strip()
        except Exception:
            return base_assistant_text
        finally:
            self._release_vram()

    def _extract_tool_actions(self, raw_text: str) -> Tuple[List[Dict[str, object]], str]:
        actions: List[Dict[str, object]] = []
        text = raw_text or ""

        # Preferred format: <tool>{...json...}</tool>
        tool_blocks = re.findall(r"<tool>(.*?)</tool>", text, flags=re.IGNORECASE | re.DOTALL)
        for block in tool_blocks:
            candidate = block.strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and parsed.get("action"):
                    actions.append(parsed)
            except Exception:
                continue

        # Remove parsed tool blocks from text shown to user.
        text = re.sub(r"<tool>.*?</tool>", " ", text, flags=re.IGNORECASE | re.DOTALL)

        # Optional fallback: detect standalone action JSON lines.
        fallback_json = re.findall(r"\{\s*\"action\"\s*:\s*\"[^\"]+\".*?\}", text, flags=re.DOTALL)
        for snippet in fallback_json:
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

        mode = str(self.crew_mode or "assist").strip().lower()
        if mode not in {"assist", "replace"}:
            mode = "assist"

        router = str(self.crew_router or "complex_only").strip().lower()
        if router == "complex_only" and not self._is_complex_reasoning_request(user_text):
            return "", ""

        memory_context = rag_context or ""
        if memory_context:
            memory_context = memory_context[: self.crew_context_max_chars]

        crew_result = run_crew_assist(
            user_text,
            memory_context=memory_context,
            config=CONFIG.get("crew", {}),
        )
        if not crew_result or not isinstance(crew_result, dict):
            return "", ""

        if not crew_result.get("ok"):
            error = str(crew_result.get("error", "CrewAI unavailable")).strip()
            if error:
                self.db.log_interaction(
                    self.session_id,
                    role="system",
                    message=error,
                    category="crew",
                )
            return "", ""

        summary = str(crew_result.get("summary", "") or "").strip()
        final = str(crew_result.get("final", "") or "").strip()
        if mode == "replace" and final:
            return "", final
        return summary, ""

    @staticmethod
    def clean_output_for_ui(raw_text: str) -> str:
        """
        Strips internal artifacts before showing text in chat UI.

        Removes:
        - <think>...</think> reasoning blocks
        - <tool>...</tool> tool call blocks
        - JSON action objects
        - fenced code blocks (often tool dumps)
        """
        text = raw_text or ""

        patterns = [
            r"<think>.*?</think>",
            r"<tool>.*?</tool>",
            r"```[\s\S]*?```",
            r"\{\s*\"action\"\s*:\s*\"[^\"]+\"[\s\S]*?\}",
        ]

        for pattern in patterns:
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE | re.DOTALL)

        # Remove leftover helper prefixes occasionally emitted by models.
        text = re.sub(r"\b(analysis|reasoning|tool\s*call)\s*:\s*", "", text, flags=re.IGNORECASE)

        return _collapse_ws(text)

    def _capture_preference_from_text(self, text: str) -> None:
        lowered = (text or "").lower()
        if not lowered:
            return

        if "always use dark mode" in lowered:
            self.local_context.set_preference("theme", "dark")
            return
        if "always use light mode" in lowered:
            self.local_context.set_preference("theme", "light")
            return

        match = re.search(r"\balways use\s+([a-z0-9 _\-]{2,80})", lowered)
        if match:
            value = match.group(1).strip(" .,!?:;")
            if value:
                self.local_context.set_preference("always_use", value)

    @staticmethod
    def _release_vram() -> None:
        gc.collect()
        if torch is None:
            return
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                if hasattr(torch.cuda, "ipc_collect"):
                    torch.cuda.ipc_collect()
        except Exception:
            pass


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
