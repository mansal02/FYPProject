"""
Main PyQt5 GUI for the offline desktop assistant.

This UI now restores key legacy capabilities while keeping the newer offline
agent flow:
- Login/register with optional remembered auto-login
- Persistent user preferences (voice/model/toggles)
- Settings tab (voice/model/logout)
- RAD memory check tab
- Optional voice-command input
- Optional live screen context input and optional live screen preview
"""

from __future__ import annotations

import argparse
import faulthandler
import html
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from PyQt5.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QFont, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from aiassistant.core.agent_core import AgentConfig, OfflineAgentCore
from aiassistant.infra.config.app_config import CONFIG
from aiassistant.infra.db.database_manager import DatabaseManager


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
_BOOT_DISABLE_LIVE2D = _bootstrap_env_bool("MARIE_DISABLE_LIVE2D", False)
_BOOT_DISABLE_VOICE_INPUT = _bootstrap_env_bool("MARIE_DISABLE_VOICE_INPUT", False)
_BOOT_DISABLE_TTS = _bootstrap_env_bool("MARIE_DISABLE_TTS", False)
_BOOT_DISABLE_SCREEN_CAPTURE = _bootstrap_env_bool("MARIE_DISABLE_SCREEN_CAPTURE", False)
_BOOT_DISABLE_SCREEN_PREVIEW = _bootstrap_env_bool("MARIE_DISABLE_SCREEN_PREVIEW", False)
_BOOT_DISABLE_LEGACY_ACTIONS = _bootstrap_env_bool("MARIE_DISABLE_LEGACY_ACTIONS", False)
_BOOT_SAFE_MINIMAL = _bootstrap_env_bool("MARIE_SAFE_MINIMAL", False)
_BOOT_SKIP_MEDIA_STACK = _BOOT_SAFE_MINIMAL or (
    _BOOT_STABILITY_MODE_LEVEL >= 2
    or (
        _BOOT_DISABLE_VOICE_INPUT
        and _BOOT_DISABLE_TTS
        and _BOOT_DISABLE_SCREEN_CAPTURE
        and _BOOT_DISABLE_SCREEN_PREVIEW
    )
)

_DEFAULT_BOOT_LOG = str(
    (
        Path(str(CONFIG.get("paths", {}).get("db_path", "./cache/assistant_sessions.db"))).resolve().parent
        / "main_gui_boot.log"
    ).resolve()
)
_BOOT_TRACE_PATH = str(
    os.environ.get(
        "MARIE_GUI_BOOT_LOG",
        _DEFAULT_BOOT_LOG if _BOOT_STABILITY_MODE_LEVEL > 0 else "",
    )
    or ""
).strip()
_FAULT_HANDLER_STREAM = None


def _write_boot_trace(step: str) -> None:
    if not _BOOT_TRACE_PATH:
        return
    try:
        trace_path = Path(_BOOT_TRACE_PATH)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} pid={os.getpid()} step={step}\n")
    except Exception:
        pass


def _enable_native_fault_tracing() -> None:
    global _FAULT_HANDLER_STREAM
    if not _bootstrap_env_bool("MARIE_FAULTHANDLER", True):
        return
    if not _BOOT_TRACE_PATH:
        return
    try:
        trace_path = Path(_BOOT_TRACE_PATH)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        if _FAULT_HANDLER_STREAM is None:
            _FAULT_HANDLER_STREAM = trace_path.open("a", encoding="utf-8")
        faulthandler.enable(_FAULT_HANDLER_STREAM, all_threads=True)
    except Exception:
        pass


_enable_native_fault_tracing()
_write_boot_trace(
    f"module:loaded stability={_BOOT_STABILITY_MODE_LEVEL} safe_minimal={int(_BOOT_SAFE_MINIMAL)} "
    f"skip_media={int(_BOOT_SKIP_MEDIA_STACK)}"
)


class _NullCameraState:
    def __init__(self) -> None:
        self.emotion = "unavailable"
        self.finger_x = None
        self.finger_y = None
        self.tracking_ok = False


class _NullCameraTracker:
    def __init__(self, camera_index: int = 0) -> None:
        self.camera_index = camera_index

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def set_mouse_control(self, enabled: bool) -> None:
        _ = enabled

    def set_emotion_detection(self, enabled: bool) -> None:
        _ = enabled

    def get_state(self) -> _NullCameraState:
        return _NullCameraState()


class _NullSpeechListener:
    def __init__(self, energy_threshold: int = 650, pause_threshold: float = 0.8) -> None:
        _ = energy_threshold
        _ = pause_threshold
        self.available = False
        self.last_error = "Speech listener disabled in stability mode."

    def get_status(self) -> Dict[str, object]:
        return {
            "available": False,
            "offline_backend": "none",
            "last_error": self.last_error,
        }

    def listen_once(
        self,
        timeout: float = 2.0,
        phrase_time_limit: float = 6.0,
        wake_words: Optional[list[str]] = None,
        allow_online_fallback: bool = False,
        allow_commands_without_wake: bool = False,
    ) -> Optional[str]:
        _ = timeout
        _ = phrase_time_limit
        _ = wake_words
        _ = allow_online_fallback
        _ = allow_commands_without_wake
        return None

    def start_background_listening(
        self,
        callback,
        wake_words: Optional[list[str]] = None,
        allow_online_fallback: bool = False,
        allow_commands_without_wake: bool = False,
    ) -> None:
        _ = callback
        _ = wake_words
        _ = allow_online_fallback
        _ = allow_commands_without_wake

    def stop_background_listening(self) -> None:
        return

    def start_wake_word_listener(
        self,
        callback,
        wake_phrase: str = "hey agent",
        access_key: Optional[str] = None,
        keyword_path: Optional[str] = None,
    ) -> bool:
        _ = callback
        _ = wake_phrase
        _ = access_key
        _ = keyword_path
        return False

    def stop_wake_word_listener(self) -> None:
        return


class _NullTextToSpeechEngine:
    def __init__(
        self,
        mode: str = "silent",
        piper_exe: Optional[str] = None,
        piper_model_path: Optional[str] = None,
        speaking_speed: float = 1.0,
    ) -> None:
        _ = mode
        _ = piper_exe
        _ = piper_model_path
        self.speaking_speed = float(speaking_speed)

    @staticmethod
    def list_system_voices() -> List[Dict[str, str]]:
        return []

    def is_available(self) -> bool:
        return False

    def get_active_mode(self) -> str:
        return "silent"

    def set_mode(
        self,
        mode: str,
        piper_exe: Optional[str] = None,
        piper_model_path: Optional[str] = None,
    ) -> None:
        _ = mode
        _ = piper_exe
        _ = piper_model_path

    def set_pyttsx3_voice(self, voice_id: str) -> None:
        _ = voice_id

    def set_speaking_speed(self, speaking_speed: float) -> None:
        self.speaking_speed = float(speaking_speed)

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def interrupt(self) -> None:
        return

    def speak(self, text: str) -> None:
        _ = text


PYAUTOGUI_AVAILABLE = False


def capture_screen_snapshot() -> Dict[str, object]:
    return {"error": "Screen capture is disabled in stability mode."}


CameraTracker = _NullCameraTracker
SpeechListener = _NullSpeechListener
TextToSpeechEngine = _NullTextToSpeechEngine

if not _BOOT_SKIP_MEDIA_STACK:
    try:
        from aiassistant.infra.vision.screen_vision import (
            PYAUTOGUI_AVAILABLE as _PYAUTOGUI_AVAILABLE,
            capture_screen_snapshot as _capture_screen_snapshot,
        )
        from aiassistant.infra.vision.vision_audio import (
            CameraTracker as _CameraTracker,
            SpeechListener as _SpeechListener,
            TextToSpeechEngine as _TextToSpeechEngine,
        )

        PYAUTOGUI_AVAILABLE = _PYAUTOGUI_AVAILABLE
        capture_screen_snapshot = _capture_screen_snapshot
        CameraTracker = _CameraTracker
        SpeechListener = _SpeechListener
        TextToSpeechEngine = _TextToSpeechEngine
        _write_boot_trace("module:media_stack_loaded")
    except Exception:
        _write_boot_trace("module:media_stack_load_failed")
        pass


def _get_action_handler_sections() -> Dict[str, List[str]]:
    if _BOOT_DISABLE_LEGACY_ACTIONS or _BOOT_SAFE_MINIMAL:
        return {}
    try:
        from aiassistant.tools.action import ActionHandler

        return ActionHandler.get_supported_command_sections()
    except Exception:
        return {}

try:
    from aiassistant.infra.voice.voice_db import CHARACTERS, get_character_data
    _write_boot_trace("module:voice_db_loaded")
except Exception:
    CHARACTERS = {}
    _write_boot_trace("module:voice_db_failed")

    def get_character_data(_char_id):
        raise RuntimeError("voice_db is unavailable")


pygame = None
live2d = None
win32con = None
win32gui = None
DOUBLEBUF = NOFRAME = OPENGL = 0
LIVE2D_AVAILABLE = False

if not (_BOOT_DISABLE_LIVE2D or _BOOT_SAFE_MINIMAL):
    try:
        import pygame
        from pygame.locals import DOUBLEBUF, NOFRAME, OPENGL
        from live2d import v3 as live2d
        import win32con
        import win32gui

        LIVE2D_AVAILABLE = True
        _write_boot_trace("module:live2d_stack_loaded")
    except Exception:
        pygame = None
        live2d = None
        win32con = None
        win32gui = None
        DOUBLEBUF = NOFRAME = OPENGL = 0
        LIVE2D_AVAILABLE = False
        _write_boot_trace("module:live2d_stack_failed")


AUTO_LOGIN_FILE = str(CONFIG.get("paths", {}).get("auto_login_file", "./.marie_autologin.json"))
DEFAULT_REASONING_MODEL = str(CONFIG.get("ollama", {}).get("model", "llama3.2:3b"))
DEFAULT_VISION_MODEL = str(CONFIG.get("vision", {}).get("vision_model", "moondream") or "moondream")
DEFAULT_LIVE2D_MODEL = str(CONFIG.get("paths", {}).get("default_live2d_model", ""))
DEFAULT_TTS_SPEED = float(CONFIG.get("voice", {}).get("speaking_speed", 1.0))

VOICE_PROFILE_LABELS = {
    "tachyon": "English UK - Bright",
    "jalter": "English US - Warm",
    "miku": "English US - Clear",
}

PROMPT_BEHAVIOR_OPTIONS = [
    ("Balanced (default)", "default"),
    ("Concise replies", "concise"),
    ("Detailed replies", "detailed"),
    ("Action-first", "action_first"),
    ("Custom guidance", "custom"),
]


def _env_bool(name: str, default: bool = False) -> bool:
    return _bootstrap_env_bool(name, default)


def _env_int(name: str, default: int = 0) -> int:
    return _bootstrap_env_int(name, default)


def save_auto_login_user(user_id: int) -> None:
    try:
        auto_path = Path(AUTO_LOGIN_FILE)
        auto_path.parent.mkdir(parents=True, exist_ok=True)
        with auto_path.open("w", encoding="utf-8") as handle:
            json.dump({"user_id": int(user_id)}, handle)
    except Exception:
        pass


def load_auto_login_user() -> Optional[int]:
    auto_path = Path(AUTO_LOGIN_FILE)
    if not auto_path.exists():
        return None

    try:
        with auto_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        user_id = data.get("user_id")
        if isinstance(user_id, int):
            return user_id
    except Exception:
        return None
    return None


def clear_auto_login_user() -> None:
    try:
        auto_path = Path(AUTO_LOGIN_FILE)
        if auto_path.exists():
            auto_path.unlink()
    except Exception:
        pass


_VOICE_NOISE_KEYWORDS = {
    "agent",
    "assistant",
    "browse",
    "close",
    "files",
    "find",
    "hey",
    "launch",
    "locate",
    "marie",
    "mute",
    "open",
    "pause",
    "play",
    "run",
    "search",
    "service",
    "start",
    "stop",
    "unmute",
    "volume",
}


def _sanitize_action_output_for_chat(output: str) -> str:
    raw = str(output or "").replace("\r\n", "\n")
    if not raw:
        return ""

    cleaned_lines: List[str] = []
    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[ACTION][TOOLS] Data:"):
            continue
        if stripped.startswith("[ACTION][TOOLS][ERROR] Data:"):
            continue
        if stripped.startswith("[ACTION][TOOLS] Found 0 lexical file match(es)."):
            continue
        cleaned_lines.append(stripped)

    deduped: List[str] = []
    for line in cleaned_lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)

    return "\n".join(deduped).strip()


def _is_noisy_voice_transcript(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())
    tokens = [token for token in normalized.split() if token]
    if len(tokens) < 20:
        return False

    unique_ratio = len(set(tokens)) / float(len(tokens))
    keyword_ratio = sum(1 for token in tokens if token in _VOICE_NOISE_KEYWORDS) / float(len(tokens))

    if len(tokens) >= 45 and keyword_ratio >= 0.72 and unique_ratio <= 0.56:
        return True
    if len(tokens) >= 80 and unique_ratio <= 0.50:
        return True
    return False


def run_action_command_isolated(text: str, timeout_sec: int = 50) -> tuple[bool, str]:
    clean_text = str(text or "").strip()
    if not clean_text:
        return True, ""

    command = [
        sys.executable,
        "-m",
        "aiassistant.tools.action_runner",
        "--text",
        clean_text,
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(5, int(timeout_sec)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "[ACTION] Command timed out in isolated runner."
    except Exception as exc:
        return False, f"[ACTION] Isolated runner failed to start: {exc}"

    chunks = []
    if completed.stdout:
        chunks.append(completed.stdout.strip())
    if completed.stderr:
        chunks.append(completed.stderr.strip())
    output = "\n".join(part for part in chunks if part).strip()
    output = _sanitize_action_output_for_chat(output)

    if completed.returncode == 0:
        return True, output

    if not output:
        output = f"[ACTION] Isolated runner exited with code {completed.returncode}."
    return False, output


def _looks_like_legacy_action_command(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False

    prefixes = (
        "files ",
        "software ",
        "service ",
        "launch ",
        "start ",
        "quit ",
        "exit ",
        "email ",
        "telegram ",
        "whatsapp ",
        "search file ",
        "find file ",
        "locate file ",
        "open file ",
        "open folder ",
        "open document ",
        "open ",
        "close ",
        "play ",
        "write ",
        "note ",
        "type ",
        "take a note ",
        "search web ",
        "open website ",
        "browse ",
        "research ",
        "web research ",
        "research web ",
        "excel ",
        "word ",
        "powerpoint ",
        "copy selected text",
        "copy now",
        "paste clipboard",
        "paste now",
        "save clipboard to rad as ",
        "scan apps",
        "update apps",
        "system check",
        "check system",
        "malware scan",
        "scan for malware",
        "run malware scan",
        "security quick scan",
    )
    if lowered.startswith(prefixes):
        return True

    if re.search(r"\b(?:open|close|launch|start|quit|exit)\b\s+[a-z0-9]", lowered):
        return True

    if re.search(r"\b(?:open|search|find|locate|look\s+for)\b.*\b(?:file|files|folder|folders|document|documents|path)\b", lowered):
        return True

    return any(token in lowered for token in ("volume up", "volume down", "mute", "unmute"))


def _looks_like_stability_safe_action_command(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False

    safe_prefixes = (
        "open ",
        "close ",
        "software open ",
        "software close ",
        "service open ",
        "open website ",
        "browse ",
        "play ",
    )
    if lowered.startswith(safe_prefixes):
        return True

    if re.search(r"\b(?:open|close|launch|start|quit|exit)\b\s+[a-z0-9]", lowered):
        return True

    if re.search(r"\b(?:open|launch|start|close|quit|exit)\b.*\b(?:youtube|discord|chrome|browser|spotify|steam|telegram|whatsapp|outlook|notepad|word|excel|powerpoint)\b", lowered):
        return True

    return False


class LoginDialog(QDialog):
    def __init__(self, db: DatabaseManager) -> None:
        super().__init__()
        self.db = db
        self.auth_result: Optional[Dict[str, object]] = None

        self.setWindowTitle("MARIE Login")
        self.setFixedSize(340, 255)

        layout = QVBoxLayout(self)

        heading = QLabel("<h2>Welcome back</h2>")
        layout.addWidget(heading)

        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Username")
        layout.addWidget(self.user_input)

        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Password")
        self.pass_input.setEchoMode(QLineEdit.Password)
        self.pass_input.returnPressed.connect(self.handle_login)
        layout.addWidget(self.pass_input)

        self.remember_cb = QCheckBox("Remember this account (auto-login next time)")
        self.remember_cb.setChecked(True)
        layout.addWidget(self.remember_cb)

        row = QHBoxLayout()
        self.login_btn = QPushButton("Login")
        self.login_btn.clicked.connect(self.handle_login)
        row.addWidget(self.login_btn)

        self.register_btn = QPushButton("Register")
        self.register_btn.clicked.connect(self.handle_register)
        row.addWidget(self.register_btn)

        layout.addLayout(row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    def handle_login(self) -> None:
        username = self.user_input.text().strip()
        password = self.pass_input.text().strip()

        auth_data = self.db.login_user(username, password)
        if not auth_data:
            self.status_label.setText("Invalid username or password.")
            return

        self.auth_result = auth_data
        self.accept()

    def handle_register(self) -> None:
        username = self.user_input.text().strip()
        password = self.pass_input.text().strip()

        ok, message = self.db.register_user(username, password)
        if ok:
            QMessageBox.information(self, "Account", message)
        else:
            QMessageBox.warning(self, "Account", message)


class AgentWorker(QThread):
    finished_text = pyqtSignal(str)
    failed_text = pyqtSignal(str)

    def __init__(self, agent: OfflineAgentCore, message: str) -> None:
        super().__init__()
        self.agent = agent
        self.message = message

    def run(self) -> None:
        try:
            reply = self.agent.process_user_message(self.message)
            self.finished_text.emit(reply)
        except Exception as exc:
            self.failed_text.emit(f"Assistant error: {exc}")


class SpeechBridge(QObject):
    transcribed = pyqtSignal(str)


class AssistantMainWindow(QMainWindow):
    def __init__(self, db: DatabaseManager, user_id: int, username: str) -> None:
        super().__init__()
        _write_boot_trace("window:init:start")

        self.db = db
        self.current_user_id = int(user_id)
        self.current_username = str(username)
        self.preferences = self.db.get_user_preference(self.current_user_id)
        self.stability_mode_level = max(0, _env_int("MARIE_STABILITY_MODE_LEVEL", 0))
        self.force_disable_voice_input = _env_bool("MARIE_DISABLE_VOICE_INPUT", False)
        self.force_disable_tts = _env_bool("MARIE_DISABLE_TTS", False)
        self.force_disable_screen_capture = _env_bool("MARIE_DISABLE_SCREEN_CAPTURE", False)
        self.force_disable_screen_preview = _env_bool("MARIE_DISABLE_SCREEN_PREVIEW", False)
        self.force_disable_legacy_actions = _env_bool("MARIE_DISABLE_LEGACY_ACTIONS", False)
        self.force_disable_rag = _env_bool("MARIE_DISABLE_RAG", False)
        self.allow_stability_safe_actions = _env_bool("MARIE_ALLOW_STABILITY_APP_ACTIONS", True)

        if self.force_disable_voice_input:
            self.preferences["voice_input_enabled"] = False
        if self.force_disable_tts:
            self.preferences["tts_enabled"] = False
        if self.force_disable_screen_capture:
            self.preferences["screen_capture_enabled"] = False
        if self.force_disable_screen_preview:
            self.preferences["screen_preview_enabled"] = False
        if self.force_disable_rag:
            self.preferences["rag_enabled"] = False

        self.logout_requested = False
        self._status_core = "Ready"
        self._tts_unavailable_notified = False
        self._latest_user_text = ""
        self._discard_next_agent_reply = False
        self._cancel_pending_reset = False
        self.chat_view_mode = "full"
        self.avatar_hidden = False
        self.system_prompt_behavior = str(
            self.preferences.get("system_prompt_behavior", "default")
        ).strip().lower() or "default"
        self.system_prompt_custom = str(self.preferences.get("system_prompt_custom", ""))

        self.reasoning_model = DEFAULT_REASONING_MODEL
        self.vision_model = DEFAULT_VISION_MODEL
        self.model_path = str(
            self.preferences.get("preferred_live2d_model") or DEFAULT_LIVE2D_MODEL
        )
        self.voice_profile = str(self.preferences.get("preferred_voice") or "system_default")
        self.speaking_speed = float(self.preferences.get("speaking_speed", DEFAULT_TTS_SPEED))

        self.agent = OfflineAgentCore(
            db=self.db,
            config=AgentConfig(
                reasoning_model=self.reasoning_model,
                vision_model=self.vision_model,
                rag_enabled=bool(self.preferences.get("rag_enabled", True)),
                hybrid_mode=bool(CONFIG.get("runtime", {}).get("hybrid_mode", False)),
                external_model=str(CONFIG.get("runtime", {}).get("external_model", "gemini-2.0-flash")),
            ),
        )
        _write_boot_trace("window:init:agent_ready")
        self.agent.set_system_prompt_behavior(
            self.system_prompt_behavior,
            self.system_prompt_custom,
        )
        self.allow_legacy_text_actions = bool(CONFIG.get("actions", {}).get("allow_legacy_text_commands", True))
        if self.force_disable_legacy_actions:
            self.allow_legacy_text_actions = False
        self.actions: Optional[object] = None

        self.camera = CameraTracker(camera_index=0)
        voice_cfg = CONFIG.get("voice", {})
        raw_voice_energy_threshold = voice_cfg.get("energy_threshold", 650)
        try:
            voice_energy_threshold = max(600, int(float(raw_voice_energy_threshold)))
        except (TypeError, ValueError):
            voice_energy_threshold = 650

        self.speech = SpeechListener(energy_threshold=voice_energy_threshold)
        self.voice_allow_online_fallback = bool(voice_cfg.get("allow_online_fallback", True))
        self.voice_allow_commands_without_wake_word = bool(
            voice_cfg.get("allow_commands_without_wake_word", True)
        )
        self.voice_mode = "Silent-Command"
        self.speech_bridge = SpeechBridge()
        self.speech_bridge.transcribed.connect(self._on_speech_transcribed)
        self.wake_words = self._build_wake_words()

        self.tts = self._create_tts_engine(self.voice_profile, self.speaking_speed)
        self.tts.start()
        _write_boot_trace("window:init:audio_ready")

        self.live2d_enabled = bool(CONFIG.get("ui", {}).get("enable_live2d", False))
        self.transparent_face = bool(CONFIG.get("ui", {}).get("transparent_face", False))
        self.model = None
        self.screen = None
        self.t_breath = 0.0
        self.last_blink = time.time()
        self.anim_timer: Optional[QTimer] = None

        self.worker: Optional[AgentWorker] = None

        self.screen_preview_timer = QTimer(self)
        self.screen_preview_timer.timeout.connect(self._refresh_screen_preview)
        self.screen_preview_timer.setInterval(1600)

        self.setWindowTitle(f"Offline Desktop Assistant | {self.current_username}")
        self.resize(1180, 780)

        self._build_ui()
        self._apply_styles()
        self._load_preferences_into_ui()
        _write_boot_trace("window:init:ui_ready")

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_status_strip)
        self.status_timer.start(1000)

        if self.live2d_enabled:
            QTimer.singleShot(150, self.init_live2d_embedding)
        else:
            self._set_avatar_status("Avatar disabled for stability mode.")

        if not self.force_disable_tts and not self.tts.is_available():
            self._append_chat(
                "System",
                "Voice output is unavailable. Install pyttsx3 or add piper.exe to the piper folder.",
            )

        if self.stability_mode_level > 0:
            active_limits: List[str] = []
            if not self.live2d_enabled:
                active_limits.append("Live2D OFF")
            if self.force_disable_voice_input:
                active_limits.append("Voice input OFF")
            if self.force_disable_tts:
                active_limits.append("TTS OFF")
            if self.force_disable_screen_capture:
                active_limits.append("Screen input OFF")
            if self.force_disable_screen_preview:
                active_limits.append("Screen preview OFF")
            if self.force_disable_legacy_actions:
                if self.allow_stability_safe_actions:
                    active_limits.append("Legacy actions LIMITED (app/site control only)")
                else:
                    active_limits.append("Legacy actions OFF")
            if self.force_disable_rag:
                active_limits.append("RAG OFF")

            suffix = ", ".join(active_limits) if active_limits else "minimal limits"
            self._append_chat(
                "System",
                f"Stability mode {self.stability_mode_level} active: {suffix}.",
            )

        _write_boot_trace("window:init:done")

    # --- UI construction ---
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs)

        self.chat_tab = QWidget()
        self.history_tab = QWidget()
        self.settings_tab = QWidget()
        self.rad_tab = QWidget()
        self.help_tab = QWidget()

        self.tabs.addTab(self.chat_tab, "Assistant")
        self.tabs.addTab(self.history_tab, "History")
        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.addTab(self.rad_tab, "RAD")
        self.tabs.addTab(self.help_tab, "Help")

        self._build_chat_tab()
        self._build_history_tab()
        self._build_settings_tab()
        self._build_rad_tab()
        self._build_help_tab()

    def _build_chat_tab(self) -> None:
        layout = QHBoxLayout(self.chat_tab)

        self.face_container = QFrame()
        self.face_container.setObjectName("avatarFrame")
        self.face_container.setFixedSize(460, 620)
        self.face_container.setStyleSheet(
            "background-color: rgba(0, 0, 0, 0); border: 2px solid #c8d5e5; border-radius: 10px;"
            if self.transparent_face
            else "background-color: #0f1520; border: 2px solid #c8d5e5; border-radius: 10px;"
        )
        face_layout = QVBoxLayout(self.face_container)
        self.face_placeholder = QLabel("Avatar starting...")
        self.face_placeholder.setAlignment(Qt.AlignCenter)
        self.face_placeholder.setStyleSheet("color: #aab8c8; font-size: 13px;")
        face_layout.addWidget(self.face_placeholder)
        layout.addWidget(self.face_container)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        title = QLabel(f"Local Desktop Assistant | user: {self.current_username}")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        right_layout.addWidget(title)

        self.chat_box = QTextEdit()
        self.chat_box.setReadOnly(True)
        self.chat_box.setPlaceholderText("Chat history appears here...")
        right_layout.addWidget(self.chat_box, stretch=1)

        controls_row = QHBoxLayout()
        self.input_line = QLineEdit()
        self.input_line.setPlaceholderText("Type a command... (example: summarize current window notes)")
        self.input_line.returnPressed.connect(self.on_send_clicked)
        controls_row.addWidget(self.input_line, stretch=1)

        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.on_send_clicked)
        controls_row.addWidget(self.send_btn)
        right_layout.addLayout(controls_row)

        safety_row = QHBoxLayout()
        self.stop_btn = QPushButton("STOP PROCESS")
        self.stop_btn.setMinimumHeight(50)
        self.stop_btn.clicked.connect(self.on_stop_clicked)
        safety_row.addWidget(self.stop_btn)

        self.resume_btn = QPushButton("Resume")
        self.resume_btn.setMinimumHeight(50)
        self.resume_btn.clicked.connect(self.on_resume_clicked)
        safety_row.addWidget(self.resume_btn)
        right_layout.addLayout(safety_row)

        view_row = QHBoxLayout()
        full_view_btn = QPushButton("Full View")
        full_view_btn.clicked.connect(lambda: self._apply_chat_view_mode("full"))
        view_row.addWidget(full_view_btn)

        chat_only_btn = QPushButton("Just Chat")
        chat_only_btn.clicked.connect(lambda: self._apply_chat_view_mode("chat_only"))
        view_row.addWidget(chat_only_btn)

        mini_btn = QPushButton("Mini Box")
        mini_btn.clicked.connect(lambda: self._apply_chat_view_mode("mini"))
        view_row.addWidget(mini_btn)

        self.avatar_toggle_btn = QPushButton("Hide Character")
        self.avatar_toggle_btn.clicked.connect(
            lambda: self._set_avatar_hidden(not self.avatar_hidden)
        )
        view_row.addWidget(self.avatar_toggle_btn)

        right_layout.addLayout(view_row)

        toggles = QHBoxLayout()
        self.screen_toggle = QCheckBox("Use live screen as input")
        self.screen_toggle.stateChanged.connect(self.on_screen_toggle_changed)
        toggles.addWidget(self.screen_toggle)

        self.screen_preview_toggle = QCheckBox("Show live screen preview")
        self.screen_preview_toggle.stateChanged.connect(self.on_screen_preview_toggle_changed)
        toggles.addWidget(self.screen_preview_toggle)

        self.camera_toggle = QCheckBox("Enable camera tracking")
        self.camera_toggle.stateChanged.connect(self.on_camera_toggle_changed)
        toggles.addWidget(self.camera_toggle)

        self.finger_toggle = QCheckBox("Finger controls mouse")
        self.finger_toggle.stateChanged.connect(self.on_finger_toggle_changed)
        toggles.addWidget(self.finger_toggle)

        right_layout.addLayout(toggles)

        toggles2 = QHBoxLayout()
        self.voice_toggle = QCheckBox("Voice command listening")
        self.voice_toggle.stateChanged.connect(self.on_voice_toggle_changed)
        toggles2.addWidget(self.voice_toggle)

        self.voice_mode_btn = QPushButton("Mode: Silent-Command")
        self.voice_mode_btn.clicked.connect(self.on_voice_mode_button_clicked)
        toggles2.addWidget(self.voice_mode_btn)

        self.rag_toggle = QCheckBox("Use local RAG memory")
        self.rag_toggle.stateChanged.connect(self.on_rag_toggle_changed)
        toggles2.addWidget(self.rag_toggle)

        self.tts_toggle = QCheckBox("Speak assistant replies")
        self.tts_toggle.stateChanged.connect(self.on_tts_toggle_changed)
        toggles2.addWidget(self.tts_toggle)

        self.desktop_mate_toggle = QCheckBox("Desktop mate mode")
        self.desktop_mate_toggle.stateChanged.connect(self.on_desktop_mate_toggle)
        toggles2.addWidget(self.desktop_mate_toggle)

        right_layout.addLayout(toggles2)

        self._chat_optional_controls = [
            self.screen_toggle,
            self.screen_preview_toggle,
            self.camera_toggle,
            self.finger_toggle,
            self.voice_toggle,
            self.voice_mode_btn,
            self.rag_toggle,
            self.tts_toggle,
            self.desktop_mate_toggle,
        ]

        self.screen_preview_label = QLabel("Live screen preview is disabled.")
        self.screen_preview_label.setObjectName("previewFrame")
        self.screen_preview_label.setAlignment(Qt.AlignCenter)
        self.screen_preview_label.setMinimumHeight(175)
        self.screen_preview_label.setWordWrap(True)
        right_layout.addWidget(self.screen_preview_label)

        self.status_label = QLabel("Status: Ready")
        right_layout.addWidget(self.status_label)

        layout.addWidget(right_panel, stretch=1)

    def _build_history_tab(self) -> None:
        layout = QVBoxLayout(self.history_tab)

        top_row = QHBoxLayout()
        self.history_session_combo = QComboBox()
        self.history_session_combo.setMinimumWidth(460)
        top_row.addWidget(self.history_session_combo)

        refresh_btn = QPushButton("Refresh Sessions")
        refresh_btn.clicked.connect(self._refresh_history_sessions)
        top_row.addWidget(refresh_btn)

        load_btn = QPushButton("Load Selected")
        load_btn.clicked.connect(self._load_selected_session_history)
        top_row.addWidget(load_btn)

        delete_btn = QPushButton("Delete Selected Session")
        delete_btn.clicked.connect(self.on_delete_selected_history_session)
        top_row.addWidget(delete_btn)

        layout.addLayout(top_row)

        self.history_box = QTextEdit()
        self.history_box.setReadOnly(True)
        self.history_box.setPlaceholderText("Select a session and click Load Selected.")
        layout.addWidget(self.history_box, stretch=1)

        self._refresh_history_sessions()
        self._select_current_session_in_history()
        self._load_selected_session_history()

    def _build_settings_tab(self) -> None:
        layout = QVBoxLayout(self.settings_tab)

        heading = QLabel("Runtime Preferences")
        heading.setFont(QFont("Segoe UI", 14, QFont.Bold))
        layout.addWidget(heading)

        form = QFormLayout()

        self.voice_profile_combo = QComboBox()
        self._populate_voice_options()
        form.addRow("Voice style", self.voice_profile_combo)

        self.speaking_speed_spin = QDoubleSpinBox()
        self.speaking_speed_spin.setRange(0.4, 2.5)
        self.speaking_speed_spin.setSingleStep(0.05)
        self.speaking_speed_spin.setValue(self.speaking_speed)
        form.addRow("Speaking speed", self.speaking_speed_spin)

        self.reasoning_model_input = QLineEdit(self.reasoning_model)
        self.reasoning_model_input.setReadOnly(True)
        self.reasoning_model_input.setToolTip("Enforced for low-VRAM runtime: llama3.2:3b")
        form.addRow("Reasoning model", self.reasoning_model_input)

        self.vision_model_input = QLineEdit(self.vision_model)
        self.vision_model_input.setReadOnly(True)
        self.vision_model_input.setToolTip("Enforced for low-VRAM runtime: moondream")
        form.addRow("Vision model", self.vision_model_input)

        model_row = QHBoxLayout()
        self.model_path_input = QLineEdit(self.model_path)
        model_row.addWidget(self.model_path_input, stretch=1)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self._browse_live2d_model)
        model_row.addWidget(browse_btn)
        model_wrap = QWidget()
        model_wrap.setLayout(model_row)
        form.addRow("Live2D model", model_wrap)

        self.prompt_behavior_combo = QComboBox()
        for label, value in PROMPT_BEHAVIOR_OPTIONS:
            self.prompt_behavior_combo.addItem(label, value)
        form.addRow("System prompt behavior", self.prompt_behavior_combo)

        self.prompt_custom_input = QTextEdit()
        self.prompt_custom_input.setPlaceholderText(
            "Optional custom instructions for assistant behavior."
        )
        self.prompt_custom_input.setMinimumHeight(90)
        form.addRow("Custom prompt notes", self.prompt_custom_input)

        self.pref_tts_cb = QCheckBox("Enable voice output")
        self.pref_voice_input_cb = QCheckBox("Enable voice command input")
        self.pref_screen_input_cb = QCheckBox("Use live screen as model input")
        self.pref_screen_preview_cb = QCheckBox("Show live screen preview")
        self.pref_rag_cb = QCheckBox("Enable local RAG memory")
        self.pref_desktop_cb = QCheckBox("Desktop mate mode")

        prefs_wrap = QWidget()
        prefs_layout = QVBoxLayout(prefs_wrap)
        prefs_layout.setContentsMargins(0, 0, 0, 0)
        prefs_layout.addWidget(self.pref_tts_cb)
        prefs_layout.addWidget(self.pref_voice_input_cb)
        prefs_layout.addWidget(self.pref_screen_input_cb)
        prefs_layout.addWidget(self.pref_screen_preview_cb)
        prefs_layout.addWidget(self.pref_rag_cb)
        prefs_layout.addWidget(self.pref_desktop_cb)
        form.addRow("Feature toggles", prefs_wrap)

        layout.addLayout(form)

        self.tts_status_label = QLabel("")
        layout.addWidget(self.tts_status_label)

        button_row = QHBoxLayout()
        save_btn = QPushButton("Save & Apply")
        save_btn.clicked.connect(self.on_save_preferences_clicked)
        button_row.addWidget(save_btn)

        test_voice_btn = QPushButton("Test Voice")
        test_voice_btn.clicked.connect(self.on_test_voice_clicked)
        button_row.addWidget(test_voice_btn)

        reload_avatar_btn = QPushButton("Reload Avatar")
        reload_avatar_btn.clicked.connect(self.on_reload_avatar_clicked)
        button_row.addWidget(reload_avatar_btn)

        forget_btn = QPushButton("Forget Auto-Login")
        forget_btn.clicked.connect(self.on_forget_autologin_clicked)
        button_row.addWidget(forget_btn)

        logout_btn = QPushButton("Log Out")
        logout_btn.clicked.connect(self.on_logout_clicked)
        button_row.addWidget(logout_btn)

        layout.addLayout(button_row)
        layout.addStretch(1)

    def _build_rad_tab(self) -> None:
        layout = QVBoxLayout(self.rad_tab)

        form_row = QHBoxLayout()
        self.rad_category_combo = QComboBox()
        self.rad_category_combo.addItems(["user_fact", "preference", "task", "note"])
        form_row.addWidget(self.rad_category_combo)

        self.rad_key_input = QLineEdit()
        self.rad_key_input.setPlaceholderText("Key (example: favorite_food)")
        form_row.addWidget(self.rad_key_input)

        self.rad_value_input = QLineEdit()
        self.rad_value_input.setPlaceholderText("Value (example: sushi)")
        form_row.addWidget(self.rad_value_input)

        add_btn = QPushButton("Add Fact")
        add_btn.clicked.connect(self.on_add_rad_clicked)
        form_row.addWidget(add_btn)

        layout.addLayout(form_row)

        self.rad_table = QTableWidget()
        self.rad_table.setColumnCount(6)
        self.rad_table.setHorizontalHeaderLabels(
            ["ID", "Category", "Key", "Value", "Confidence", "Created"]
        )
        self.rad_table.hideColumn(0)
        self.rad_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.rad_table, stretch=1)

        row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._load_rad_table)
        row.addWidget(refresh_btn)

        delete_btn = QPushButton("Delete Selected")
        delete_btn.clicked.connect(self.on_delete_rad_clicked)
        row.addWidget(delete_btn)

        row.addStretch(1)
        layout.addLayout(row)

        self._load_rad_table()

    def _build_help_tab(self) -> None:
        layout = QVBoxLayout(self.help_tab)
        help_text = QTextEdit()
        help_text.setReadOnly(True)
        voice_samples = []
        for wake in self.wake_words[:3]:
            voice_samples.append(f"{wake} open chrome")
            voice_samples.append(f"{wake} search web local llm optimization")
        voice_samples = voice_samples[:4]

        section_html = []
        section_html.append("<h2>Commands and Voice Tasks</h2>")
        section_html.append("<p>Use the same commands by typing or speaking.</p>")

        section_html.append("<h3>Voice Usage</h3><ul>")
        section_html.append(
            "<li>Enable <b>Voice command listening</b> in Assistant tab."
            " Speak a wake word, then your command.</li>"
        )
        section_html.append(
            f"<li>Wake words: <code>{html.escape(', '.join(self.wake_words))}</code></li>"
        )
        for sample in voice_samples:
            section_html.append(f"<li>Example: <code>{html.escape(sample)}</code></li>")
        section_html.append(
            "<li>UI quick commands: <code>hide character</code>, <code>show character</code>, "
            "<code>just chat</code>/<code>chat only</code>, <code>mini box</code>/<code>mini mode</code>, "
            "<code>full mode</code>/<code>full view</code>/<code>normal mode</code>/<code>show all</code>.</li>"
        )
        section_html.append(
            "<li>Safety commands: <code>stop</code>, <code>cancel</code>, <code>stop process</code>, "
            "<code>cancel process</code>, <code>stop response</code>, <code>cancel response</code>.</li>"
        )
        section_html.append("</ul>")

        supported_sections = _get_action_handler_sections()
        for title, commands in supported_sections.items():
            section_html.append(f"<h3>{html.escape(title)}</h3><ul>")
            for command in commands:
                section_html.append(f"<li><code>{html.escape(str(command))}</code></li>")
            section_html.append("</ul>")
        if not supported_sections:
            section_html.append(
                "<p>Extended command list is hidden in the current stability profile.</p>"
            )

        section_html.append("<h3>Communication Setup Notes</h3><ul>")
        section_html.append(
            "<li>Email: <code>provider outlook</code> can use installed Outlook desktop on Windows. "
            "For SMTP send-now, use assistant tool action <code>send_email</code> with SMTP credentials.</li>"
        )
        section_html.append(
            "<li>Telegram: requires bot token and chat_id in command.</li>"
        )
        section_html.append(
            "<li>WhatsApp text command uses WhatsApp Web mode. For API mode, use tool action "
            "<code>send_whatsapp</code> with Twilio fields.</li>"
        )
        section_html.append("</ul>")

        section_html.append("<h3>Memory Agent Commands</h3><ul>")
        section_html.append("<li><code>python -m aiassistant.infra.memory_agent --once</code></li>")
        section_html.append("<li><code>python -m aiassistant.infra.memory_agent</code></li>")
        section_html.append(
            "<li><code>python -m aiassistant.infra.memory_agent --query &quot;your question&quot; --top-k 4</code></li>"
        )
        section_html.append("</ul>")

        section_html.append("<h3>Safety and Controls</h3><ul>")
        section_html.append("<li><b>STOP PROCESS</b> interrupts active reasoning immediately.</li>")
        section_html.append("<li>Use <b>Resume</b> to continue after a stop.</li>")
        section_html.append(
            "<li>Voice output uses pyttsx3 (system voice) or Piper voices when piper.exe is available.</li>"
        )
        section_html.append("</ul>")

        help_text.setHtml("".join(section_html))
        layout.addWidget(help_text)

    # --- history tab ---
    def _refresh_history_sessions(self) -> None:
        selected_session = self.history_session_combo.currentData()
        self.history_session_combo.clear()

        sessions = self.db.list_sessions(limit=80)
        for session in sessions:
            sid = session["session_id"]
            started = session["started_at"]
            count = session.get("message_count", 0)
            label = session.get("label") or "session"
            text = f"{started} | {label} | {count} msg | {sid}"
            self.history_session_combo.addItem(text, sid)

        if selected_session:
            index = self.history_session_combo.findData(selected_session)
            if index >= 0:
                self.history_session_combo.setCurrentIndex(index)

    def _select_current_session_in_history(self) -> None:
        current_session_id = self.agent.session_id
        index = self.history_session_combo.findData(current_session_id)
        if index >= 0:
            self.history_session_combo.setCurrentIndex(index)

    def _load_selected_session_history(self) -> None:
        session_id = self.history_session_combo.currentData()
        if not session_id:
            self.history_box.setPlainText("No session selected.")
            return

        rows = self.db.get_all_session_messages(session_id)
        if not rows:
            self.history_box.setPlainText("No messages found in this session.")
            return

        parts = []
        for row in rows:
            ts = html.escape(str(row.get("timestamp", "")))
            role = html.escape(str(row.get("role", "")))
            cat = html.escape(str(row.get("category", "")))
            msg = html.escape(str(row.get("message", ""))).replace("\n", "<br>")
            parts.append(f"<p><b>[{ts}] {role}</b> <i>({cat})</i><br>{msg}</p>")

        self.history_box.setHtml("".join(parts))

    def on_delete_selected_history_session(self) -> None:
        session_id = str(self.history_session_combo.currentData() or "").strip()
        if not session_id:
            return

        if session_id == self.agent.session_id:
            QMessageBox.information(
                self,
                "History",
                "Current active session cannot be deleted. Select a past session.",
            )
            return

        confirm = QMessageBox.question(
            self,
            "Delete Session",
            "Delete this past session and all its messages? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        if self.db.delete_session_history(session_id):
            self._refresh_history_sessions()
            self._select_current_session_in_history()
            self._load_selected_session_history()
            self._append_chat("System", f"Deleted history session {session_id}.")
        else:
            QMessageBox.warning(self, "History", "Selected session could not be deleted.")

    # --- styles ---
    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #e9f1fa, stop:1 #f9fcff);
            }
            QWidget {
                color: #1a2430;
                font-family: Segoe UI;
                font-size: 14px;
            }
            QTabWidget::pane {
                border: 1px solid #cdd9e8;
                border-radius: 10px;
                background: #ffffff;
            }
            QTabBar::tab {
                background: #dce7f5;
                padding: 10px 16px;
                margin-right: 4px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                border: 1px solid #cdd9e8;
                border-bottom: none;
            }
            QTextEdit, QLineEdit, QComboBox, QDoubleSpinBox, QTableWidget {
                background: #ffffff;
                border: 1px solid #c7d4e4;
                border-radius: 8px;
                padding: 8px;
            }
            QPushButton {
                background: #1f77d8;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #1b69bf;
            }
            QPushButton#danger {
                background: #d83d45;
            }
            QPushButton#danger:hover {
                background: #c2363e;
            }
            QFrame#previewFrame, QLabel#previewFrame {
                border: 1px dashed #b8c7da;
                border-radius: 10px;
                background: #f6f9fd;
                color: #6c7a8f;
            }
            """
        )
        self.stop_btn.setObjectName("danger")
        self.stop_btn.style().unpolish(self.stop_btn)
        self.stop_btn.style().polish(self.stop_btn)

    # --- initialization helpers ---
    def _load_preferences_into_ui(self) -> None:
        self._set_checkbox_checked(self.screen_toggle, bool(self.preferences.get("screen_capture_enabled", False)))
        self._set_checkbox_checked(self.screen_preview_toggle, bool(self.preferences.get("screen_preview_enabled", False)))
        self._set_checkbox_checked(self.voice_toggle, bool(self.preferences.get("voice_input_enabled", False)))
        self._set_checkbox_checked(self.rag_toggle, bool(self.preferences.get("rag_enabled", True)))
        self._set_checkbox_checked(self.tts_toggle, bool(self.preferences.get("tts_enabled", True)))
        self._set_checkbox_checked(self.desktop_mate_toggle, bool(self.preferences.get("desktop_mate_enabled", False)))

        if self.force_disable_screen_capture:
            self._set_checkbox_checked(self.screen_toggle, False)
            self.screen_toggle.setEnabled(False)
        if self.force_disable_screen_preview:
            self._set_checkbox_checked(self.screen_preview_toggle, False)
            self.screen_preview_toggle.setEnabled(False)
        if self.force_disable_voice_input:
            self._set_checkbox_checked(self.voice_toggle, False)
            self.voice_toggle.setEnabled(False)
            self.voice_mode_btn.setEnabled(False)
        if self.force_disable_tts:
            self._set_checkbox_checked(self.tts_toggle, False)
            self.tts_toggle.setEnabled(False)
        if self.force_disable_rag:
            self._set_checkbox_checked(self.rag_toggle, False)
            self.rag_toggle.setEnabled(False)

        behavior_index = self.prompt_behavior_combo.findData(self.system_prompt_behavior)
        if behavior_index < 0:
            behavior_index = self.prompt_behavior_combo.findData("default")
        if behavior_index >= 0:
            self.prompt_behavior_combo.setCurrentIndex(behavior_index)
        self.prompt_custom_input.setPlainText(self.system_prompt_custom)
        self.agent.set_system_prompt_behavior(
            self.system_prompt_behavior,
            self.system_prompt_custom,
        )

        self.agent.set_screen_capture_enabled(self.screen_toggle.isChecked())
        self.agent.set_rag_enabled(self.rag_toggle.isChecked())

        self._set_voice_input_enabled(self.voice_toggle.isChecked(), announce=False)
        self._set_screen_preview_enabled(self.screen_preview_toggle.isChecked(), announce=False)
        self._set_desktop_mate_enabled(self.desktop_mate_toggle.isChecked(), announce=False)
        self._refresh_chat_view_visibility()
        self._sync_preference_checkbox_states()
        self._refresh_tts_status_label()

    @staticmethod
    def _set_checkbox_checked(widget: QCheckBox, value: bool) -> None:
        widget.blockSignals(True)
        widget.setChecked(bool(value))
        widget.blockSignals(False)

    def _build_wake_words(self) -> List[str]:
        configured = str(CONFIG.get("voice", {}).get("wake_word", "hey agent")).strip().lower()
        chunks = [c.strip() for c in re.split(r"[,;|]", configured) if c.strip()]
        words = ["hey agent", "assistant", "marie"]
        words.extend(chunks)

        deduped: List[str] = []
        for word in words:
            if word not in deduped:
                deduped.append(word)
        return deduped

    def _detect_piper_executable(self) -> Optional[str]:
        piper_dir = Path(str(CONFIG.get("paths", {}).get("piper_dir", "./piper")))
        candidates = [
            piper_dir / "piper.exe",
            piper_dir / "piper",
            piper_dir / "bin" / "piper.exe",
            piper_dir / "bin" / "piper",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate.resolve())
        return None

    def _create_tts_engine(self, profile: str, speaking_speed: float) -> object:
        if self.force_disable_tts:
            return TextToSpeechEngine(mode="silent", speaking_speed=speaking_speed)

        piper_exe = self._detect_piper_executable()
        clean_profile = (profile or "system_default").strip()

        if clean_profile.startswith("character:"):
            char_id = clean_profile.split(":", 1)[1].strip().lower()
            if char_id:
                try:
                    _, model_path = get_character_data(char_id)
                except Exception:
                    model_path = ""

                if piper_exe and model_path and Path(model_path).exists():
                    return TextToSpeechEngine(
                        mode="piper",
                        piper_exe=piper_exe,
                        piper_model_path=model_path,
                        speaking_speed=speaking_speed,
                    )

        engine = TextToSpeechEngine(
            mode="auto",
            piper_exe=piper_exe,
            speaking_speed=speaking_speed,
        )

        if clean_profile.startswith("pyttsx3:"):
            voice_id = clean_profile.split(":", 1)[1].strip()
            if voice_id:
                engine.set_mode("pyttsx3", piper_exe=piper_exe)
                engine.set_pyttsx3_voice(voice_id)

        return engine

    def _populate_voice_options(self) -> None:
        self.voice_profile_combo.clear()
        self.voice_profile_combo.addItem("System default (auto)", "system_default")

        for char_id in sorted(CHARACTERS.keys()):
            data = CHARACTERS.get(char_id, {})
            display_name = VOICE_PROFILE_LABELS.get(char_id) or str(data.get("name") or char_id)
            self.voice_profile_combo.addItem(
                f"Voice pack: {display_name}",
                f"character:{char_id}",
            )

        if not self.force_disable_tts and not _BOOT_SKIP_MEDIA_STACK:
            for voice in TextToSpeechEngine.list_system_voices():
                voice_id = str(voice.get("id", "")).strip()
                voice_name = str(voice.get("name", voice_id)).strip()
                if voice_id:
                    self.voice_profile_combo.addItem(
                        f"System voice: {voice_name}",
                        f"pyttsx3:{voice_id}",
                    )

        index = self.voice_profile_combo.findData(self.voice_profile)
        if index < 0:
            self.voice_profile_combo.addItem(f"Current: {self.voice_profile}", self.voice_profile)
            index = self.voice_profile_combo.findData(self.voice_profile)
        if index >= 0:
            self.voice_profile_combo.setCurrentIndex(index)

    def _refresh_tts_status_label(self) -> None:
        active_mode = self.tts.get_active_mode()
        if active_mode == "pyttsx3":
            self.tts_status_label.setText("TTS backend: pyttsx3 (system voice)")
            return
        if active_mode == "piper":
            self.tts_status_label.setText("TTS backend: Piper character voice")
            return
        self.tts_status_label.setText(
            "TTS backend: unavailable. Install pyttsx3 or add piper.exe in the piper folder."
        )

    def _sync_preference_checkbox_states(self) -> None:
        if hasattr(self, "pref_tts_cb"):
            self._set_checkbox_checked(self.pref_tts_cb, self.tts_toggle.isChecked())
        if hasattr(self, "pref_voice_input_cb"):
            self._set_checkbox_checked(self.pref_voice_input_cb, self.voice_toggle.isChecked())
        if hasattr(self, "pref_screen_input_cb"):
            self._set_checkbox_checked(self.pref_screen_input_cb, self.screen_toggle.isChecked())
        if hasattr(self, "pref_screen_preview_cb"):
            self._set_checkbox_checked(self.pref_screen_preview_cb, self.screen_preview_toggle.isChecked())
        if hasattr(self, "pref_rag_cb"):
            self._set_checkbox_checked(self.pref_rag_cb, self.rag_toggle.isChecked())
        if hasattr(self, "pref_desktop_cb"):
            self._set_checkbox_checked(self.pref_desktop_cb, self.desktop_mate_toggle.isChecked())

    def _persist_runtime_preferences(self, extra: Optional[Dict[str, object]] = None) -> None:
        payload: Dict[str, object] = {
            "preferred_voice": self.voice_profile,
            "preferred_reasoning_model": self.reasoning_model,
            "preferred_vision_model": self.vision_model,
            "preferred_live2d_model": self.model_path,
            "tts_enabled": self.tts_toggle.isChecked(),
            "voice_input_enabled": self.voice_toggle.isChecked(),
            "screen_capture_enabled": self.screen_toggle.isChecked(),
            "screen_preview_enabled": self.screen_preview_toggle.isChecked(),
            "rag_enabled": self.rag_toggle.isChecked(),
            "desktop_mate_enabled": self.desktop_mate_toggle.isChecked(),
            "speaking_speed": self.speaking_speed,
            "system_prompt_behavior": self.system_prompt_behavior,
            "system_prompt_custom": self.system_prompt_custom,
        }
        if extra:
            payload.update(extra)

        self.db.save_user_preference(self.current_user_id, payload)
        self.preferences.update(payload)

    # --- avatar ---
    def _set_avatar_status(self, message: str) -> None:
        if hasattr(self, "face_placeholder"):
            self.face_placeholder.setText(message)
            self.face_placeholder.show()

    def _set_live2d_rendering_enabled(self, enabled: bool) -> None:
        if self.anim_timer is None:
            return

        if enabled and self.model is not None:
            if not self.anim_timer.isActive():
                self.anim_timer.start(16)
            return

        if self.anim_timer.isActive():
            self.anim_timer.stop()

    def init_live2d_embedding(self) -> None:
        if not self.live2d_enabled:
            self._set_avatar_status("Avatar disabled for stability mode.")
            return

        if not LIVE2D_AVAILABLE:
            self._set_avatar_status("Avatar unavailable: missing live2d/pygame/win32 dependencies.")
            self._append_chat("System", "Live2D runtime is unavailable in this environment.")
            return

        if os.name != "nt":
            self._set_avatar_status("Avatar embedding currently supports Windows only.")
            self._append_chat("System", "Live2D embedding is currently supported on Windows only.")
            return

        model_file = Path(self.model_path).expanduser().resolve()
        if not model_file.exists():
            self._set_avatar_status(f"Avatar model file not found:\n{model_file}")
            self._append_chat("System", f"Live2D model not found at {model_file}")
            return

        try:
            pygame.init()
            os.environ["SDL_VIDEO_WINDOW_POS"] = "-1000,-1000"
            self.screen = pygame.display.set_mode((450, 600), DOUBLEBUF | OPENGL | NOFRAME)

            pygame_hwnd = pygame.display.get_wm_info().get("window")
            if not pygame_hwnd:
                raise RuntimeError("Unable to acquire pygame window handle for embedding.")

            parent_hwnd = int(self.face_container.winId())
            win32gui.SetParent(pygame_hwnd, parent_hwnd)
            win32gui.SetWindowPos(
                pygame_hwnd,
                win32con.HWND_TOP,
                0,
                0,
                450,
                600,
                win32con.SWP_SHOWWINDOW,
            )

            live2d.init()
            live2d.glInit()

            original_cwd = os.getcwd()
            try:
                os.chdir(str(model_file.parent))
                self.model = live2d.LAppModel()
                self.model.LoadModelJson(str(model_file))
                self.model.Resize(450, 600)
            finally:
                os.chdir(original_cwd)

            self.t_breath = 0.0
            self.last_blink = time.time()

            self.face_placeholder.hide()
            if self.anim_timer is None:
                self.anim_timer = QTimer(self)
                self.anim_timer.timeout.connect(self.update_live2d_frame)
            self._set_live2d_rendering_enabled(True)

            self._append_chat("System", "Avatar loaded.")
        except Exception as exc:
            self._set_avatar_status(f"Avatar failed to initialize: {exc}")
            self._append_chat("System", f"Avatar initialization failed: {exc}")

    def update_live2d_frame(self) -> None:
        if not self.model or not LIVE2D_AVAILABLE:
            return

        for _ in pygame.event.get():
            pass

        self.t_breath += 0.05
        self.model.SetParameterValue("ParamBreath", (math.sin(self.t_breath) + 1) / 2)
        self.model.SetParameterValue("ParamMouthOpenY", 0.0)
        self.model.SetParameterValue("Param85", 1.0)

        if time.time() > self.last_blink + 3:
            self.model.SetParameterValue("ParamEyeLOpen", 0.0)
            self.model.SetParameterValue("ParamEyeROpen", 0.0)
            if time.time() > self.last_blink + 3.2:
                self.last_blink = time.time()
        else:
            self.model.SetParameterValue("ParamEyeLOpen", 1.0)
            self.model.SetParameterValue("ParamEyeROpen", 1.0)

        self.model.Update()
        if self.transparent_face:
            live2d.clearBuffer(0.0, 0.0, 0.0, 0.0)
        else:
            live2d.clearBuffer(0.08, 0.1, 0.14, 1.0)
        self.model.Draw()
        pygame.display.flip()

    # --- interaction helpers ---
    def _append_chat(self, speaker: str, text: str) -> None:
        safe_speaker = html.escape(str(speaker))
        safe_text = html.escape(str(text)).replace("\n", "<br>")
        self.chat_box.append(f"<b>{safe_speaker}:</b> {safe_text}")

    def _set_status_core(self, status: str) -> None:
        self._status_core = status
        self._update_status_strip()

    def _update_status_strip(self) -> None:
        extra = []
        if self.camera_toggle.isChecked():
            cam_state = self.camera.get_state()
            extra.append(f"Camera {'ok' if cam_state.tracking_ok else 'off'}")
            extra.append(f"Emotion {cam_state.emotion}")

        if self.screen_toggle.isChecked():
            extra.append("Screen input ON")
        if self.voice_toggle.isChecked():
            extra.append("Voice cmd ON")

        suffix = ""
        if extra:
            suffix = " | " + " | ".join(extra)
        self.status_label.setText(f"Status: {self._status_core}{suffix}")

    @staticmethod
    def _normalize_command_text(text: str) -> str:
        lowered = str(text or "").lower()
        lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
        return re.sub(r"\s+", " ", lowered).strip()

    def _is_cancel_command(self, normalized: str) -> bool:
        if not normalized:
            return False
        cancel_phrases = {
            "stop",
            "cancel",
            "stop process",
            "cancel process",
            "stop response",
            "cancel response",
            "stop now",
            "cancel now",
        }
        if normalized in cancel_phrases:
            return True
        return bool(re.fullmatch(r"(stop|cancel)(\s+the)?(\s+current)?(\s+voice|\s+process|\s+response)?", normalized))

    def _try_handle_local_ui_command(self, raw_text: str, source: str) -> bool:
        normalized = self._normalize_command_text(raw_text)
        if not normalized:
            return False

        if self._is_cancel_command(normalized):
            self._cancel_all_processing(source=f"{source} command")
            return True

        if (
            "hide character" in normalized
            or "hide chara" in normalized
            or "hide avatar" in normalized
        ):
            self._set_avatar_hidden(True)
            return True

        if (
            "show character" in normalized
            or "show chara" in normalized
            or "show avatar" in normalized
        ):
            self._set_avatar_hidden(False)
            return True

        if "just chat" in normalized or "chat only" in normalized:
            self._apply_chat_view_mode("chat_only")
            return True

        if "mini box" in normalized or "mini mode" in normalized or normalized == "mini":
            self._apply_chat_view_mode("mini")
            return True

        if (
            "full mode" in normalized
            or "normal mode" in normalized
            or "show all" in normalized
            or "full view" in normalized
        ):
            self._apply_chat_view_mode("full")
            return True

        return False

    def _cancel_all_processing(self, source: str) -> None:
        worker_running = bool(self.worker and self.worker.isRunning())

        self.agent.stop()
        self._cancel_pending_reset = worker_running
        self._discard_next_agent_reply = worker_running
        self._latest_user_text = ""

        try:
            self.tts.interrupt()
        except Exception:
            pass

        self.speech.stop_background_listening()
        self.speech.stop_wake_word_listener()
        if self.voice_toggle.isChecked():
            QTimer.singleShot(180, self._restart_voice_listeners_if_enabled)

        if not worker_running:
            self.agent.reset_stop()
            self._cancel_pending_reset = False

        self._set_status_core("Cancellation requested")
        self._append_chat("System", f"Cancelled current process ({source}).")

    def _restart_voice_listeners_if_enabled(self) -> None:
        if not self.voice_toggle.isChecked():
            return
        self._start_voice_listener_backend()

    # --- chat actions ---
    def on_send_clicked(self) -> None:
        message = self.input_line.text().strip()
        if not message:
            return

        self.input_line.clear()
        self._append_chat("You", message)

        if self._try_handle_local_ui_command(message, source="text"):
            return

        if self.worker and self.worker.isRunning():
            self._set_status_core("Busy processing current request")
            return

        self.agent.reset_stop()
        self._discard_next_agent_reply = False
        self._cancel_pending_reset = False
        self._latest_user_text = message

        legacy_candidate = _looks_like_legacy_action_command(message)
        allow_isolated_action = False
        if legacy_candidate:
            if self.allow_legacy_text_actions:
                allow_isolated_action = True
            elif (
                self.force_disable_legacy_actions
                and self.allow_stability_safe_actions
                and _looks_like_stability_safe_action_command(message)
            ):
                allow_isolated_action = True

        if allow_isolated_action:
            avatar_was_running = bool(
                self.anim_timer is not None and self.anim_timer.isActive()
            )
            if avatar_was_running:
                self._set_live2d_rendering_enabled(False)
            try:
                ok, action_result = run_action_command_isolated(message)
            finally:
                if avatar_was_running:
                    self._set_live2d_rendering_enabled(True)

            if action_result:
                self._append_chat("Action" if ok else "Action Error", action_result)
                # Keep software-control commands isolated from in-process tool execution.
                self._set_status_core("Ready")
                return

            if ok:
                # Action command finished with only filtered debug output.
                self._set_status_core("Ready")
                return

            # No meaningful action output: continue with reasoning path as fallback.

        self.worker = AgentWorker(self.agent, message)
        self.worker.finished_text.connect(self._on_agent_reply)
        self.worker.failed_text.connect(self._on_agent_error)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

        self._set_status_core("Thinking")

    def on_stop_clicked(self) -> None:
        self._cancel_all_processing(source="stop button")

    def on_resume_clicked(self) -> None:
        if self.worker and self.worker.isRunning():
            self._append_chat("System", "Wait for the current worker to finish cancelling.")
            return
        self.agent.reset_stop()
        self._cancel_pending_reset = False
        self._discard_next_agent_reply = False
        self._set_status_core("Ready")
        self._append_chat("System", "Processing resumed.")

    # --- toggle handlers ---
    def _set_screen_input_enabled(self, enabled: bool, announce: bool = True) -> None:
        if self.force_disable_screen_capture:
            enabled = False
        enabled = bool(enabled)
        self._set_checkbox_checked(self.screen_toggle, enabled)
        self.agent.set_screen_capture_enabled(enabled)
        self._sync_preference_checkbox_states()
        self._persist_runtime_preferences()
        if announce:
            self._append_chat("System", f"Live screen input {'enabled' if enabled else 'disabled'}.")

    def on_screen_toggle_changed(self, state: int) -> None:
        self._set_screen_input_enabled(state == Qt.Checked)

    def _set_screen_preview_enabled(self, enabled: bool, announce: bool = True) -> None:
        if self.force_disable_screen_preview:
            enabled = False
        enabled = bool(enabled)
        if enabled and not PYAUTOGUI_AVAILABLE:
            QMessageBox.warning(
                self,
                "Screen Preview",
                "PyAutoGUI is not available in this environment, so live screen preview cannot start.",
            )
            enabled = False

        self._set_checkbox_checked(self.screen_preview_toggle, enabled)

        if enabled:
            self.screen_preview_timer.start()
            self._refresh_screen_preview()
        else:
            self.screen_preview_timer.stop()
            self.screen_preview_label.setPixmap(QPixmap())
            self.screen_preview_label.setText("Live screen preview is disabled.")

        self._sync_preference_checkbox_states()
        self._persist_runtime_preferences()
        if announce:
            self._append_chat("System", f"Live screen preview {'enabled' if enabled else 'disabled'}.")

    def on_screen_preview_toggle_changed(self, state: int) -> None:
        self._set_screen_preview_enabled(state == Qt.Checked)

    def _start_voice_listener_backend(self) -> str:
        # Ensure listener backends are not duplicated.
        self.speech.stop_background_listening()
        self.speech.stop_wake_word_listener()

        wake_backend = "none"
        wake_backend = "pvporcupine"
        started = self.speech.start_wake_word_listener(
            callback=self._on_wake_word_detected,
            wake_phrase="hey agent",
        )
        if not started:
            wake_backend = "speech-fallback"
            self.speech.start_background_listening(
                callback=lambda text: self.speech_bridge.transcribed.emit(text),
                wake_words=self.wake_words,
                allow_online_fallback=self.voice_allow_online_fallback,
                allow_commands_without_wake=self.voice_allow_commands_without_wake_word,
            )

        return wake_backend

    def _set_voice_input_enabled(self, enabled: bool, announce: bool = True) -> None:
        if self.force_disable_voice_input:
            enabled = False

        enabled = bool(enabled)
        if enabled and not self.speech.available:
            speech_status = self.speech.get_status()
            detail = str(speech_status.get("last_error", "")).strip()
            message = "SpeechRecognition microphone input is not available in this environment."
            if detail:
                message = f"{message}\n\n{detail}"
            QMessageBox.warning(self, "Speech", message)
            enabled = False

        self._set_checkbox_checked(self.voice_toggle, enabled)

        wake_backend = "none"
        if enabled:
            wake_backend = self._start_voice_listener_backend()
        else:
            self.speech.stop_background_listening()
            self.speech.stop_wake_word_listener()

        self.voice_mode = "Voice-Active" if enabled else "Silent-Command"
        self._update_voice_mode_button()

        self._sync_preference_checkbox_states()
        self._persist_runtime_preferences()
        if announce:
            if enabled:
                self._append_chat("System", f"Voice command listening enabled ({wake_backend}).")
            else:
                self._append_chat("System", "Voice command listening disabled.")

    def _update_voice_mode_button(self) -> None:
        self.voice_mode_btn.setText(f"Mode: {self.voice_mode}")

    def toggle_voice(self) -> str:
        next_enabled = not self.voice_toggle.isChecked()
        self._set_voice_input_enabled(next_enabled, announce=False)
        return self.voice_mode

    def on_voice_mode_button_clicked(self) -> None:
        mode = self.toggle_voice()
        self._append_chat("System", f"Voice mode switched to {mode}.")

    def _on_wake_word_detected(self) -> None:
        heard = self.speech.listen_once(
            timeout=3.0,
            phrase_time_limit=8.0,
            wake_words=None,
            allow_online_fallback=self.voice_allow_online_fallback,
        )
        if not heard:
            return

        if self.worker and self.worker.isRunning():
            candidate = self._strip_wake_word_prefix(heard)
            if self._is_cancel_command(self._normalize_command_text(candidate)):
                self.speech_bridge.transcribed.emit(heard)
            return

        self.speech_bridge.transcribed.emit(heard)

    def on_voice_toggle_changed(self, state: int) -> None:
        self._set_voice_input_enabled(state == Qt.Checked)

    def _set_rag_enabled(self, enabled: bool, announce: bool = True) -> None:
        if self.force_disable_rag:
            enabled = False
        enabled = bool(enabled)
        self._set_checkbox_checked(self.rag_toggle, enabled)
        self.agent.set_rag_enabled(enabled)
        self._sync_preference_checkbox_states()
        self._persist_runtime_preferences()
        if announce:
            self._append_chat("System", f"RAG memory {'enabled' if enabled else 'disabled'}.")

    def on_rag_toggle_changed(self, state: int) -> None:
        self._set_rag_enabled(state == Qt.Checked)

    def _set_tts_enabled(self, enabled: bool, announce: bool = True) -> None:
        if self.force_disable_tts:
            enabled = False

        enabled = bool(enabled)
        self._set_checkbox_checked(self.tts_toggle, enabled)
        self._sync_preference_checkbox_states()
        self._persist_runtime_preferences()
        if announce:
            self._append_chat("System", f"Voice output {'enabled' if enabled else 'disabled'}.")

    def on_tts_toggle_changed(self, state: int) -> None:
        self._set_tts_enabled(state == Qt.Checked)

    def _refresh_chat_view_visibility(self) -> None:
        mode = self.chat_view_mode
        show_full_controls = mode == "full"
        show_avatar = (
            show_full_controls
            and (not self.avatar_hidden)
            and (not self.desktop_mate_toggle.isChecked())
        )

        if hasattr(self, "tabs"):
            self.tabs.setCurrentWidget(self.chat_tab)
            self.tabs.tabBar().setVisible(show_full_controls)

        if hasattr(self, "face_container"):
            self.face_container.setVisible(show_avatar)
        self._set_live2d_rendering_enabled(self.live2d_enabled and show_avatar)

        for widget in getattr(self, "_chat_optional_controls", []):
            widget.setVisible(show_full_controls)

        if hasattr(self, "screen_preview_label"):
            self.screen_preview_label.setVisible(show_full_controls)

        if show_full_controls and self.screen_preview_toggle.isChecked():
            self.screen_preview_timer.start()
            self._refresh_screen_preview()
        else:
            self.screen_preview_timer.stop()
            if hasattr(self, "screen_preview_label"):
                self.screen_preview_label.setPixmap(QPixmap())
                self.screen_preview_label.setText("Live screen preview is disabled.")

        if hasattr(self, "status_label"):
            self.status_label.setVisible(mode != "mini")

    def _apply_chat_view_mode(self, mode: str, announce: bool = True) -> None:
        normalized = str(mode or "").strip().lower()
        if normalized not in {"full", "chat_only", "mini"}:
            normalized = "full"

        self.chat_view_mode = normalized
        self._refresh_chat_view_visibility()

        if not self.desktop_mate_toggle.isChecked():
            if normalized == "mini":
                self.resize(560, 420)
            elif normalized == "chat_only":
                self.resize(920, 700)
            else:
                self.resize(1180, 780)

        if normalized == "mini":
            self.raise_()
            self.activateWindow()

        if announce:
            labels = {
                "full": "Full view mode enabled.",
                "chat_only": "Chat-only mode enabled.",
                "mini": "Mini box mode enabled.",
            }
            self._append_chat("System", labels.get(normalized, "View mode updated."))

    def _set_avatar_hidden(self, hidden: bool, announce: bool = True) -> None:
        self.avatar_hidden = bool(hidden)
        if hasattr(self, "avatar_toggle_btn"):
            self.avatar_toggle_btn.setText("Show Character" if self.avatar_hidden else "Hide Character")
        self._refresh_chat_view_visibility()
        if announce:
            state = "hidden" if self.avatar_hidden else "visible"
            self._append_chat("System", f"Character is now {state}.")

    def _set_desktop_mate_enabled(self, enabled: bool, announce: bool = True) -> None:
        enabled = bool(enabled)
        self._set_checkbox_checked(self.desktop_mate_toggle, enabled)
        self._apply_desktop_mate_mode(enabled)
        self._sync_preference_checkbox_states()
        self._persist_runtime_preferences()
        if announce:
            self._append_chat("System", f"Desktop mate mode {'enabled' if enabled else 'disabled'}.")

    def on_desktop_mate_toggle(self, state: int) -> None:
        self._set_desktop_mate_enabled(state == Qt.Checked)

    def on_camera_toggle_changed(self, state: int) -> None:
        enabled = state == Qt.Checked
        if enabled:
            self.camera.start()
            self._append_chat("System", "Camera tracking started.")
        else:
            self.camera.stop()
            self._append_chat("System", "Camera tracking stopped.")

    def on_finger_toggle_changed(self, state: int) -> None:
        enabled = state == Qt.Checked
        self.camera.set_mouse_control(enabled)
        self._append_chat("System", f"Finger mouse control {'enabled' if enabled else 'disabled'}.")

    # --- screen preview ---
    def _refresh_screen_preview(self) -> None:
        if not self.screen_preview_toggle.isChecked():
            return

        payload = capture_screen_snapshot()
        if payload.get("error"):
            self.screen_preview_label.setPixmap(QPixmap())
            self.screen_preview_label.setText(str(payload["error"]))
            return

        image_path = payload.get("image_path", "")
        if not image_path or not Path(str(image_path)).exists():
            self.screen_preview_label.setPixmap(QPixmap())
            self.screen_preview_label.setText("No screen snapshot available yet.")
            return

        pix = QPixmap(str(image_path))
        if pix.isNull():
            self.screen_preview_label.setPixmap(QPixmap())
            self.screen_preview_label.setText("Preview image could not be loaded.")
            return

        scaled = pix.scaled(
            self.screen_preview_label.width() - 12,
            self.screen_preview_label.height() - 12,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.screen_preview_label.setText("")
        self.screen_preview_label.setPixmap(scaled)

        title = str(payload.get("window_title", "")).strip()
        if title:
            self.screen_preview_label.setToolTip(f"Active window: {title}")

    # --- worker callbacks ---
    def _on_agent_reply(self, text: str) -> None:
        if self._discard_next_agent_reply:
            self._discard_next_agent_reply = False
            self._append_chat("System", "Response cancelled.")
            self._latest_user_text = ""
            return

        safe_text = self.agent.clean_output_for_ui(text)
        if safe_text:
            self._append_chat("Assistant", safe_text)

        auto_saved = self.db.auto_store_important_conversation_data(
            user_id=self.current_user_id,
            user_text=self._latest_user_text,
            assistant_text=safe_text,
        )
        if auto_saved:
            preview_items = [f"{item.get('key')}={item.get('value')}" for item in auto_saved[:3]]
            preview = "; ".join(preview_items)
            self._append_chat("System", f"[RAD auto-saved] {preview}")
            self._load_rad_table()
        self._latest_user_text = ""

        self._refresh_history_sessions()
        self._select_current_session_in_history()
        self._load_selected_session_history()

        if self.tts_toggle.isChecked():
            if self.tts.is_available():
                self.tts.speak(safe_text)
            elif not self._tts_unavailable_notified:
                self._append_chat(
                    "System",
                    "Voice output is enabled but no TTS backend is available.",
                )
                self._tts_unavailable_notified = True

    def _on_agent_error(self, err: str) -> None:
        if self._discard_next_agent_reply:
            self._discard_next_agent_reply = False
            self._latest_user_text = ""
            return

        self._latest_user_text = ""
        self._append_chat("System", err)

    def _on_worker_finished(self) -> None:
        if self._cancel_pending_reset:
            self.agent.reset_stop()
            self._cancel_pending_reset = False
        self._set_status_core("Ready")

    def _strip_wake_word_prefix(self, text: str) -> str:
        cleaned = text.strip()
        lowered = cleaned.lower()
        for wake in self.wake_words:
            if lowered == wake:
                return ""
            prefix = wake + " "
            if lowered.startswith(prefix):
                return cleaned[len(prefix):].strip(" ,:;")
        return cleaned

    def _on_speech_transcribed(self, text: str) -> None:
        if not text:
            return

        cleaned = self._strip_wake_word_prefix(text)
        if not cleaned:
            return

        if _is_noisy_voice_transcript(cleaned):
            self._set_status_core("Ignored noisy voice transcript")
            return

        self._append_chat("Mic", cleaned)
        if self._try_handle_local_ui_command(cleaned, source="voice"):
            return

        if self.worker and self.worker.isRunning():
            return

        self.input_line.setText(cleaned)
        self.on_send_clicked()

    # --- settings actions ---
    def _browse_live2d_model(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "Select Live2D Model",
            "",
            "Live2D Model (*.model3.json)",
        )
        if file_name:
            self.model_path_input.setText(file_name)

    def on_test_voice_clicked(self) -> None:
        if self.force_disable_tts:
            self._append_chat("System", "Voice test is disabled in stability mode.")
            return

        self.tts.speak("Voice test successful. Settings were applied.")

    def on_reload_avatar_clicked(self) -> None:
        if not self.live2d_enabled:
            QMessageBox.information(
                self,
                "Avatar",
                "Avatar is disabled for stability. Set ui.enable_live2d: true in config.yaml and restart to enable.",
            )
            return

        if self.anim_timer is not None:
            self.anim_timer.stop()
        if LIVE2D_AVAILABLE:
            try:
                live2d.dispose()
            except Exception:
                pass
        self.model = None
        self.face_placeholder.show()
        self.init_live2d_embedding()

    def on_forget_autologin_clicked(self) -> None:
        clear_auto_login_user()
        QMessageBox.information(self, "Auto-login", "Remembered login has been cleared.")

    def on_save_preferences_clicked(self) -> None:
        # Model routing is intentionally fixed to stay within low VRAM.
        self.reasoning_model = DEFAULT_REASONING_MODEL
        self.vision_model = DEFAULT_VISION_MODEL
        self.reasoning_model_input.setText(self.reasoning_model)
        self.vision_model_input.setText(self.vision_model)
        self.model_path = self.model_path_input.text().strip() or DEFAULT_LIVE2D_MODEL
        self.speaking_speed = float(self.speaking_speed_spin.value())

        self.agent.set_reasoning_model(self.reasoning_model)
        self.agent.set_vision_model(self.vision_model)

        self.system_prompt_behavior = str(
            self.prompt_behavior_combo.currentData() or "default"
        ).strip().lower() or "default"
        self.system_prompt_custom = self.prompt_custom_input.toPlainText().strip()
        self.agent.set_system_prompt_behavior(
            self.system_prompt_behavior,
            self.system_prompt_custom,
        )

        new_voice_profile = str(self.voice_profile_combo.currentData() or "system_default")
        voice_changed = new_voice_profile != self.voice_profile

        if voice_changed:
            old_tts = self.tts
            self.voice_profile = new_voice_profile
            self.tts = self._create_tts_engine(self.voice_profile, self.speaking_speed)
            self.tts.start()
            old_tts.stop()
            self._tts_unavailable_notified = False
        else:
            self.tts.set_speaking_speed(self.speaking_speed)

        self._set_screen_input_enabled(self.pref_screen_input_cb.isChecked(), announce=False)
        self._set_screen_preview_enabled(self.pref_screen_preview_cb.isChecked(), announce=False)
        self._set_voice_input_enabled(self.pref_voice_input_cb.isChecked(), announce=False)
        self._set_rag_enabled(self.pref_rag_cb.isChecked(), announce=False)
        self._set_tts_enabled(self.pref_tts_cb.isChecked(), announce=False)
        self._set_desktop_mate_enabled(self.pref_desktop_cb.isChecked(), announce=False)

        self._persist_runtime_preferences(
            {
                "preferred_voice": self.voice_profile,
                "preferred_reasoning_model": self.reasoning_model,
                "preferred_vision_model": self.vision_model,
                "preferred_live2d_model": self.model_path,
                "speaking_speed": self.speaking_speed,
                "system_prompt_behavior": self.system_prompt_behavior,
                "system_prompt_custom": self.system_prompt_custom,
            }
        )
        self._refresh_tts_status_label()
        self._append_chat("System", "Preferences saved and applied.")
        QMessageBox.information(self, "Preferences", "Preferences saved and applied.")

    def on_logout_clicked(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Log Out",
            "Log out now? The app window will close.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        clear_auto_login_user()
        self.logout_requested = True
        self.close()

    # --- RAD tab actions ---
    def _load_rad_table(self) -> None:
        rows = self.db.get_rad_data(self.current_user_id, limit=500)
        self.rad_table.setRowCount(0)

        for row_index, row_data in enumerate(rows):
            self.rad_table.insertRow(row_index)
            self.rad_table.setItem(row_index, 0, QTableWidgetItem(str(row_data.get("id", ""))))
            self.rad_table.setItem(row_index, 1, QTableWidgetItem(str(row_data.get("category", ""))))
            self.rad_table.setItem(row_index, 2, QTableWidgetItem(str(row_data.get("key_data", ""))))
            self.rad_table.setItem(row_index, 3, QTableWidgetItem(str(row_data.get("value_data", ""))))
            self.rad_table.setItem(
                row_index,
                4,
                QTableWidgetItem(f"{float(row_data.get('confidence_score', 1.0)):.2f}"),
            )
            self.rad_table.setItem(row_index, 5, QTableWidgetItem(str(row_data.get("created_at", ""))))

    def on_add_rad_clicked(self) -> None:
        category = self.rad_category_combo.currentText().strip() or "user_fact"
        key_data = self.rad_key_input.text().strip()
        value_data = self.rad_value_input.text().strip()

        if not key_data or not value_data:
            QMessageBox.warning(self, "RAD", "Both key and value are required.")
            return

        self.db.add_rad_data(self.current_user_id, category, key_data, value_data)
        self.rad_key_input.clear()
        self.rad_value_input.clear()
        self._load_rad_table()
        self._append_chat("System", f"RAD fact saved: {key_data} = {value_data}")

    def on_delete_rad_clicked(self) -> None:
        row = self.rad_table.currentRow()
        if row < 0:
            return

        rad_id_item = self.rad_table.item(row, 0)
        if rad_id_item is None:
            return

        try:
            rad_id = int(rad_id_item.text())
        except (TypeError, ValueError):
            return

        if self.db.delete_rad_data(self.current_user_id, rad_id):
            self._load_rad_table()

    # --- desktop mate mode ---
    def _apply_desktop_mate_mode(self, enabled: bool) -> None:
        if enabled:
            flags = Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            self.setWindowFlags(flags)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setWindowOpacity(0.93)
            self.resize(760, 560)
        else:
            flags = Qt.Window
            self.setWindowFlags(flags)
            self.setAttribute(Qt.WA_TranslucentBackground, False)
            self.setWindowOpacity(1.0)
            if self.chat_view_mode == "mini":
                self.resize(560, 420)
            elif self.chat_view_mode == "chat_only":
                self.resize(920, 700)
            else:
                self.resize(1180, 780)

        self._refresh_chat_view_visibility()

        self.show()

    # --- lifecycle ---
    def closeEvent(self, event) -> None:  # type: ignore[override]
        _write_boot_trace("window:close_event")
        try:
            self.agent.stop()
            self.camera.stop()
            self.speech.stop_background_listening()
            self.speech.stop_wake_word_listener()
            self.tts.stop()

            if self.anim_timer is not None:
                self.anim_timer.stop()

            if LIVE2D_AVAILABLE:
                try:
                    live2d.dispose()
                except Exception:
                    pass
                try:
                    pygame.quit()
                except Exception:
                    pass

            self.db.logout_user(self.current_user_id)
        except Exception:
            pass

        event.accept()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline desktop assistant GUI")
    parser.add_argument("--force-login", action="store_true", help="Ignore remembered auto-login")
    parser.add_argument("--reset-login", action="store_true", help="Clear remembered auto-login and exit")
    return parser.parse_args()


def main() -> None:
    _write_boot_trace("main:start")
    args = _parse_args()
    _write_boot_trace("main:args_parsed")

    if args.reset_login:
        clear_auto_login_user()
        _write_boot_trace("main:reset_login_exit")
        print("[LOGIN] Remembered auto-login cleared.")
        return

    app = QApplication(sys.argv)
    _write_boot_trace("main:qapplication_created")
    db = DatabaseManager(db_path=str(CONFIG.get("paths", {}).get("db_path", "cache/assistant_sessions.db")))
    _write_boot_trace("main:database_ready")

    auth_data: Optional[Dict[str, object]] = None

    if not args.force_login:
        remembered_user = load_auto_login_user()
        if remembered_user is not None:
            auth_data = db.resume_user_session(remembered_user)
            if auth_data is None:
                clear_auto_login_user()

    if auth_data is None:
        _write_boot_trace("main:login_dialog_open")
        login = LoginDialog(db)
        if login.exec_() != QDialog.Accepted or not login.auth_result:
            _write_boot_trace("main:login_cancelled")
            db.close()
            return

        auth_data = login.auth_result
        _write_boot_trace("main:login_success")
        if login.remember_cb.isChecked():
            save_auto_login_user(int(auth_data.get("user_id", 0)))
        else:
            clear_auto_login_user()

    user_id = int(auth_data.get("user_id", 0))
    username = str(auth_data.get("username", "user"))

    _write_boot_trace("main:window_init_start")
    window = AssistantMainWindow(db=db, user_id=user_id, username=username)
    window.show()
    _write_boot_trace("main:window_shown")

    _write_boot_trace("main:event_loop_enter")
    exit_code = app.exec_()
    _write_boot_trace(f"main:event_loop_exit code={exit_code}")
    db.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
