"""
Main PyQt5 GUI for the offline desktop assistant.

Optimized to run seamlessly on mid-tier hardware with multi-threaded isolated 
runners to prevent UI freezing.
"""

from __future__ import annotations

# ==========================================
# 1. IMPORTS
# ==========================================
import argparse
import contextlib
import docx
import faulthandler
import html
import io
import json
import math
import mimetypes
import os
import pptx
import pyautogui
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import openpyxl
from PyQt5.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QFont, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget
)

from aiassistant.core.llm_core import AgentConfig, OfflineAgentCore
from aiassistant.infra.config.app_config import CONFIG, ROOT_DIR
from aiassistant.infra.db.database_manager import DatabaseManager
from aiassistant.infra.doc_indexer import build_default_indexer
from aiassistant.tools.tools_os import ActionHandler, convert_file_to_json

# ==========================================
# 2. ENVIRONMENT & GLOBALS
# ==========================================
def _bootstrap_env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None: return bool(default)
    lowered = str(value).strip().lower()
    return True if lowered in {"1", "true", "yes", "on"} else False if lowered in {"0", "false", "no", "off"} else bool(default)

def _bootstrap_env_int(name: str, default: int = 0) -> int:
    value = os.environ.get(name)
    if value is None: return int(default)
    try: return int(str(value).strip())
    except Exception: return int(default)

LAST_EVENT_POLL_TIME = 0.0
_EVENT_POLL_INTERVAL_SEC = 0.05
_BOOT_STABILITY_MODE_LEVEL = _bootstrap_env_int("MARIE_STABILITY_MODE_LEVEL", 0)
_BOOT_DISABLE_VOICE_INPUT = _bootstrap_env_bool("MARIE_DISABLE_VOICE_INPUT", False)
_BOOT_SAFE_MINIMAL = _bootstrap_env_bool("MARIE_SAFE_MINIMAL", False)

AUTO_LOGIN_FILE = str(CONFIG.get("paths", {}).get("auto_login_file", "./.marie_autologin.json"))
DEFAULT_REASONING_MODEL = str(CONFIG.get("ollama", {}).get("model", "qwen2.5-coder:7b"))
DEFAULT_LIVE2D_MODEL = str(CONFIG.get("paths", {}).get("default_live2d_model", "./models/Knight/knight.model3.json"))
DEFAULT_TTS_SPEED = float(CONFIG.get("voice", {}).get("speaking_speed", 1.0))

_DEFAULT_BOOT_LOG = str(
    (Path(str(CONFIG.get("paths", {}).get("db_path", "./cache/assistant_sessions.db"))).resolve().parent
    / "main_gui_boot.log").resolve()
)
_BOOT_TRACE_PATH = str(os.environ.get("MARIE_GUI_BOOT_LOG", _DEFAULT_BOOT_LOG if _BOOT_STABILITY_MODE_LEVEL > 0 else "")).strip()

def _write_boot_trace(step: str) -> None:
    if not _BOOT_TRACE_PATH: return
    try:
        trace_path = Path(_BOOT_TRACE_PATH)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} pid={os.getpid()} step={step}\n")
    except Exception: pass

_write_boot_trace(f"module:loaded stability={_BOOT_STABILITY_MODE_LEVEL} safe_minimal={int(_BOOT_SAFE_MINIMAL)}")

# ==========================================
# 3. EXTERNAL MODULES (Voice / Live2D)
# ==========================================
class _NullTextToSpeechEngine:
    def __init__(self, mode: str = "silent", speaking_speed: float = 1.0) -> None:
        self.speaking_speed = float(speaking_speed)
    @staticmethod
    def list_system_voices() -> List[Dict[str, str]]: return []
    def is_available(self) -> bool: return False
    def get_active_mode(self) -> str: return "silent"
    def set_speaking_speed(self, speaking_speed: float) -> None: self.speaking_speed = float(speaking_speed)
    def start(self) -> None: pass
    def stop(self) -> None: pass
    def interrupt(self) -> None: pass
    def speak(self, text: str) -> None: pass

PYAUTOGUI_AVAILABLE = False
TextToSpeechEngine = _NullTextToSpeechEngine

try:
    from aiassistant.infra.voice.voice_db import CHARACTERS, get_character_data
    _write_boot_trace("module:voice_db_loaded")
except Exception:
    CHARACTERS = {}
    _write_boot_trace("module:voice_db_failed")

pygame = live2d = win32con = win32gui = None
DOUBLEBUF = NOFRAME = OPENGL = 0
LIVE2D_AVAILABLE = True

if not _BOOT_SAFE_MINIMAL:
    try:
        import pygame
        from live2d import v3 as live2d
        from pygame.locals import DOUBLEBUF, NOFRAME, OPENGL
        import win32con
        import win32gui
        LIVE2D_AVAILABLE = True
        _write_boot_trace("module:live2d_stack_loaded")
    except Exception:
        LIVE2D_AVAILABLE = False
        _write_boot_trace("module:live2d_stack_failed")

# ==========================================
# 4. UTILITIES (Storage & Parsing)
# ==========================================
def save_auto_login_user(user_id: int) -> None:
    try:
        auto_path = Path(AUTO_LOGIN_FILE)
        auto_path.parent.mkdir(parents=True, exist_ok=True)
        with auto_path.open("w", encoding="utf-8") as handle:
            json.dump({"user_id": int(user_id)}, handle)
    except Exception: pass

def load_auto_login_user() -> Optional[int]:
    auto_path = Path(AUTO_LOGIN_FILE)
    if not auto_path.exists(): return None
    try:
        with auto_path.open("r", encoding="utf-8") as handle:
            return json.load(handle).get("user_id")
    except Exception: return None

def clear_auto_login_user() -> None:
    try:
        auto_path = Path(AUTO_LOGIN_FILE)
        if auto_path.exists(): auto_path.unlink()
    except Exception: pass

def _sanitize_action_output_for_chat(output: str) -> str:
    raw = str(output or "").replace("\r\n", "\n")
    if not raw: return ""
    cleaned_lines = [
        line.strip() for line in raw.split("\n")
        if line.strip() and not line.strip().startswith(("[ACTION][TOOLS] Data:", "[ACTION][TOOLS][ERROR] Data:", "[ACTION][TOOLS] Found 0"))
    ]
    deduped = []
    for line in cleaned_lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return "\n".join(deduped).strip()

def run_action_command_isolated(text: str, timeout_sec: int = 300) -> tuple[bool, str]:
    clean_text = str(text or "").strip()
    if not clean_text: return True, ""

    command = [sys.executable, "-m", "aiassistant.tools.tools_os", "--text", clean_text]
    try:
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(ROOT_DIR)
        completed = subprocess.run(
            command, capture_output=True, text=True, 
            timeout=max(5, int(timeout_sec)), check=False, cwd=str(ROOT_DIR), env=child_env
        )
    except subprocess.TimeoutExpired:
        return False, "[ACTION] Command timed out in isolated runner."
    except Exception as exc:
        return False, f"[ACTION] Isolated runner failed to start: {exc}"

    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
    output = _sanitize_action_output_for_chat(output)
    return completed.returncode == 0, output or (f"[ACTION] Runner exited with code {completed.returncode}." if completed.returncode != 0 else "")

def _looks_like_legacy_action_command(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered: return False
    return bool(re.search(r"\b(open|launch|start|close|quit|exit|search|find|locate|volume|type|note)\b", lowered))

# ==========================================
# 5. ASYNC WORKERS (Threads)
# ==========================================
class FastNativeActionWorker(QThread):
    """Executes actions directly in memory using a pre-warmed ActionHandler. Zero latency."""
    finished_action = pyqtSignal(bool, str)
    
    def __init__(self, raw_text=None, json_cmd=None):
        super().__init__()
        self.raw_text = raw_text
        self.json_cmd = json_cmd
        self.handler = ActionHandler() 
        
    def run(self):
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                if self.json_cmd:
                    self.handler._execute_json_action(self.json_cmd)
                elif self.raw_text:
                    self.handler.execute(self.raw_text)
            
            output = buffer.getvalue().strip()
            self.finished_action.emit(True, output or "[ACTION] Executed instantly.")
        except Exception as e:
            self.finished_action.emit(False, f"[ACTION ERROR] {e}")

class JsonActionWorker(QThread):
    """Asynchronous runner to execute JSON-based shell utility tools."""
    finished_action = pyqtSignal(bool, str)
    error_occurred = pyqtSignal(str)

    def __init__(self, action_cmd: dict, timeout_sec: int = 55) -> None:
        super().__init__()
        self.action_cmd = action_cmd
        self.timeout_sec = timeout_sec
        self._is_cancelled = False
        self._process = None
        
    def cancel(self) -> None:
        self._is_cancelled = True
        if self._process:
            try: self._process.kill()
            except Exception: pass

    def run(self) -> None:
        command = [sys.executable, "-m", "aiassistant.tools.tools_os", "--action-json", json.dumps(self.action_cmd)]
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(ROOT_DIR)

        try:
            self._process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                text=True, cwd=str(ROOT_DIR), env=child_env
            )

            try:
                stdout, stderr = self._process.communicate(timeout=self.timeout_sec)
            except subprocess.TimeoutExpired:
                self._process.kill()
                stdout, stderr = self._process.communicate()
                self.finished_action.emit(False, "[ACTION] Command timed out in isolated runner.")
                return

            if self._is_cancelled:
                self.finished_action.emit(False, "[ACTION] Execution cancelled by user.")
                return

            stdout_text = (stdout or "").strip()
            stderr_text = (stderr or "").strip()

            for line in reversed(stdout_text.splitlines()):
                try:
                    result = json.loads(line)
                    if isinstance(result, dict) and "success" in result:
                        msg = result.get("message", "")
                        data = result.get("data", "")
                        err = result.get("error", "")
                        if result["success"]:
                            self.finished_action.emit(True, f"{msg} {data}".strip())
                        else:
                            self.finished_action.emit(False, f"{msg} {err}".strip())
                        return
                except json.JSONDecodeError: continue

            output = "\n".join(part for part in [stdout_text, stderr_text] if part).strip()
            ok = self._process.returncode == 0
            fallback_msg = output or (f"[ACTION] Exited with code {self._process.returncode}." if not ok else "")
            self.finished_action.emit(ok, fallback_msg)

        except Exception as exc:
            self.error_occurred.emit(f"[ACTION] Isolated runner failed to start: {exc}")
        finally:
            self._process = None

class ActionWorker(QThread):
    """Legacy async runner for shell utility tools without triggering GUI thread locks."""
    finished_action = pyqtSignal(bool, str)

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def run(self) -> None:
        ok, result = run_action_command_isolated(self.message)
        self.finished_action.emit(ok, result)

class AgentWorker(QThread):
    token_received = pyqtSignal(str)   
    finished_text = pyqtSignal(str)
    failed_text = pyqtSignal(str)

    def __init__(self, agent: OfflineAgentCore, message: str) -> None:
        super().__init__()
        self.agent = agent
        self.message = message

    def run(self) -> None:
        try:
            intent = "general"
            manager = getattr(self.agent, 'manager', None)
            if manager:
                intent = manager.classify(self.message, None)

            if self.agent.fast_orchestrator and intent == "general" and not self.agent.stop_event.is_set():
                self.agent.db.log_interaction(self.agent.session_id, role="user", message=self.message, category="chat")
                full_reply, token_buffer = "", ""
                last_emit_time = time.time()
                
                for stream_chunk in self.agent.fast_orchestrator.stream_response(self.message, "general"):
                    if self.agent.stop_event.is_set(): break
                    chunk_text = stream_chunk.get("chunk", "")
                    if chunk_text:
                        full_reply += chunk_text
                        self.token_received.emit(chunk_text)
                        
                        if time.time() - last_emit_time > 0.05:
                            self.token_received.emit(token_buffer)
                            token_buffer = ""
                            last_emit_time = time.time()
                
                if token_buffer: self.token_received.emit(token_buffer)
                
                cleaned = self.agent.clean_output_for_ui(full_reply) or full_reply
                self.agent.db.log_interaction(self.agent.session_id, role="assistant", message=cleaned, category="chat")
                self.finished_text.emit(f"[STREAMED]{cleaned}")
            else:
                reply = self.agent.process_user_message(self.message)
                self.finished_text.emit(reply)
        except Exception as exc:
            self.failed_text.emit(f"Assistant error: {exc}")

# ==========================================
# 6. UI COMPONENTS
# ==========================================
class DropLineEdit(QLineEdit):
    """Custom QLineEdit that accepts drag-and-drop files."""
    file_dropped = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            self.file_dropped.emit(file_path)
            self.setText(f'[File Attached: {os.path.basename(file_path)}] ')
            event.acceptProposedAction()

class LoginDialog(QDialog):
    def __init__(self, db: DatabaseManager) -> None:
        super().__init__()
        self.db = db
        self.auth_result: Optional[Dict[str, object]] = None
        self.setWindowTitle("MARIE Login")
        self.setFixedSize(340, 255)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Welcome back</h2>"))

        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Username")
        layout.addWidget(self.user_input)

        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Password")
        self.pass_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.pass_input)

        self.remember_cb = QCheckBox("Remember this account")
        self.remember_cb.setChecked(True)
        layout.addWidget(self.remember_cb)

        row = QHBoxLayout()
        login_btn = QPushButton("Login")
        login_btn.clicked.connect(self.handle_login)
        row.addWidget(login_btn)

        register_btn = QPushButton("Register")
        register_btn.clicked.connect(self.handle_register)
        row.addWidget(register_btn)
        layout.addLayout(row)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    def handle_login(self) -> None:
        auth_data = self.db.login_user(self.user_input.text().strip(), self.pass_input.text().strip())
        if not auth_data:
            self.status_label.setText("Invalid credentials.")
            return
        self.auth_result = auth_data
        self.accept()

    def handle_register(self) -> None:
        ok, message = self.db.register_user(self.user_input.text().strip(), self.pass_input.text().strip())
        if ok:
            QMessageBox.information(self, "Account", message)
        else:
            QMessageBox.warning(self, "Account", message)

# ==========================================
# 7. MAIN WINDOW GUI
# ==========================================
class AssistantMainWindow(QMainWindow):
    indexer_status_signal = pyqtSignal(str)

    def __init__(self, db: DatabaseManager, user_id: int, username: str) -> None:
        super().__init__()
        _write_boot_trace("window:init:start")

        # Core state
        self.db = db
        self.current_user_id = int(user_id)
        self.current_username = str(username)
        self.preferences = self.db.get_user_preference(self.current_user_id)
        self.logout_requested = False
        self._status_core = "Ready"
        self._latest_user_text = ""
        self.doc_indexer = None
        self.idle_index_controller = None
        self._idle_training_status = "Idle training: paused"
        self.response_only_mode = False
        
        # Configs
        self.reasoning_model = DEFAULT_REASONING_MODEL
        self.model_path = str(self.preferences.get("preferred_live2d_model") or DEFAULT_LIVE2D_MODEL)
        self.voice_profile = str(self.preferences.get("preferred_voice") or "system_default")
        self.speaking_speed = float(self.preferences.get("speaking_speed", DEFAULT_TTS_SPEED))
        self.online_mode = "offline"

        # Thread Refs
        self.anim_timer: Optional[QTimer] = None
        self.worker: Optional[AgentWorker] = None
        self.action_worker: Optional[ActionWorker] = None
        self.fast_worker: Optional[FastNativeActionWorker] = None
        self.json_action_worker: Optional[JsonActionWorker] = None

        # Agent Setup
        self.agent = OfflineAgentCore(
            db=self.db,
            config=AgentConfig(
                reasoning_model=self.reasoning_model,
                rag_enabled=bool(self.preferences.get("rag_enabled", True)),
                hybrid_mode=False,
                online_mode="offline",
            ),
            user_id=self.current_user_id or 1,
        )
        self.agent.set_system_prompt_behavior("default", "")
        self.allow_legacy_text_actions = bool(CONFIG.get("actions", {}).get("allow_legacy_text_commands", True))

        # Avatar / Visual Setup
        self.tts = _NullTextToSpeechEngine(speaking_speed=self.speaking_speed)
        self.live2d_enabled = bool(CONFIG.get("ui", {}).get("enable_live2d", True))
        self.model = None
        self.screen = None
        self.t_breath = 0.0
        self.last_blink = time.time()

        # Window Config
        self.setWindowTitle(f" Desktop Assistant | {self.current_username}")
        self.resize(1180, 780)

        # Core Initializations
        self.indexer_status_signal.connect(self._set_status_core)
        self._build_ui()
        self._apply_styles()
        self._load_preferences_into_ui()

        # Background processes
        QTimer.singleShot(300, self._init_idle_training)
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_status_strip)
        self.status_timer.start(1000)

        if self.live2d_enabled:
            QTimer.singleShot(150, self.init_live2d_embedding)

    # --- Setup Methods ---
    def _init_idle_training(self) -> None:
        def status_callback(msg: str):
            if "indexing documents" in msg:
                self.indexer_status_signal.emit("Scanning searchable mirror...")
            elif "paused" in msg:
                self.indexer_status_signal.emit("Ready")
            else:
                self.indexer_status_signal.emit(msg)

        self.doc_indexer, self.idle_index_controller = build_default_indexer(
            db=self.db, 
            status_cb=status_callback
        )
        if self.idle_index_controller:
            self.idle_index_controller.start()

    def _apply_styles(self) -> None:
        self.setStyleSheet("""
            QMainWindow { background: #0b0f16; }
            QWidget { color: #7fb0ff; font-family: Segoe UI, Bahnschrift; font-size: 14px; }
            QTabWidget::pane { border: 1px solid #1d2a3d; background: #101a2b; border-radius: 8px; }
            QTextEdit, QLineEdit, QComboBox, QDoubleSpinBox, QTableWidget { background: #0f1726; border: 1px solid #23324a; padding: 6px; border-radius: 6px; }
            QPushButton { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00b0ff, stop:1 #5ee7ff); color: #0b0f16; font-weight: bold; border-radius: 6px; padding: 8px; }
            QPushButton:hover { background: #7cf1ff; }
        """)

    # --- UI Builders ---
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs)

        self.chat_tab, self.history_tab, self.settings_tab, self.rad_tab, self.help_tab = QWidget(), QWidget(), QWidget(), QWidget(), QWidget()

        self.tabs.addTab(self.chat_tab, "Assistant")
        self.tabs.addTab(self.history_tab, "History")
        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.addTab(self.rad_tab, "RAG")
        self.tabs.addTab(self.help_tab, "Help")

        self._build_chat_tab()
        self._build_history_tab()
        self._build_settings_tab()
        self._build_rad_tab()
        self._build_help_tab()

    def _build_chat_tab(self) -> None:
        layout = QHBoxLayout(self.chat_tab)

        avatar_control_bar = QHBoxLayout()
        avatar_control_bar.addStretch(1)

        self.minimize_avatar_btn = QPushButton("−")
        self.minimize_avatar_btn.setFixedSize(28, 28)
        self.minimize_avatar_btn.setStyleSheet("""
            QPushButton { background: #ff4d6d; color: #0b0f16; border: 1px solid #6f1d2c; border-radius: 6px; font-weight: bold; font-size: 14px; padding: 0px; }
            QPushButton:hover { background: #ff7a8f; }
        """)
        self.minimize_avatar_btn.clicked.connect(self.on_minimize_avatar_clicked)

        avatar_control_bar.addWidget(self.minimize_avatar_btn)
        layout.addLayout(avatar_control_bar) 

        self.face_container = QFrame()
        self.face_container.setObjectName("avatarFrame")
        self.face_container.setFixedSize(470, 620)
        self.face_container.setStyleSheet("background-color: #0f1520; border: 2px solid #c8d5e5; border-radius: 10px;")

        face_layout = QVBoxLayout(self.face_container)
        self.face_placeholder = QLabel("Avatar starting...")
        self.face_placeholder.setAlignment(Qt.AlignCenter)
        self.face_placeholder.setStyleSheet("color: #aab8c8; font-size: 13px;")
        face_layout.addWidget(self.face_placeholder)

        layout.addWidget(self.face_container)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        title_layout = QHBoxLayout()
        title = QLabel(f"Local Desktop Assistant | user: {self.current_username}")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        title_layout.addWidget(title)
        
        self.restore_avatar_btn = QPushButton("Show Avatar Panel")
        self.restore_avatar_btn.setVisible(False)
        self.restore_avatar_btn.clicked.connect(self.on_restore_avatar_clicked)
        title_layout.addWidget(self.restore_avatar_btn)
        right_layout.addLayout(title_layout)

        self.chat_box = QTextEdit()
        self.chat_box.setObjectName("chatBox")
        self.chat_box.setReadOnly(True)
        self.chat_box.setPlaceholderText("Chat history appears here...")
        right_layout.addWidget(self.chat_box, stretch=1)

        controls_row = QHBoxLayout()
        self.input_line = DropLineEdit()
        self.input_line.setPlaceholderText("Type a command or drop a file here...")
        self.input_line.returnPressed.connect(self.on_send_clicked)
        self.attached_file_path = None
        self.input_line.file_dropped.connect(self._on_file_dropped)
        
        controls_row.addWidget(self.input_line, stretch=1)

        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.on_send_clicked)
        controls_row.addWidget(self.send_btn)
        right_layout.addLayout(controls_row)

        safety_row = QHBoxLayout()
        self.stop_btn = QPushButton("STOP PROCESS")
        self.stop_btn.setMinimumHeight(45)
        self.stop_btn.clicked.connect(self.on_stop_clicked)
        safety_row.addWidget(self.stop_btn)

        self.resume_btn = QPushButton("Resume")
        self.resume_btn.setMinimumHeight(45)
        self.resume_btn.clicked.connect(self.on_resume_clicked)
        safety_row.addWidget(self.resume_btn)
        right_layout.addLayout(safety_row)

        toggles_row = QHBoxLayout()
        self.rag_toggle = QCheckBox("Use local RAG memory")
        self.rag_toggle.setChecked(bool(self.preferences.get("rag_enabled", True)))
        self.rag_toggle.stateChanged.connect(self.on_rag_toggle_changed)
        toggles_row.addWidget(self.rag_toggle)
        
        self.tts_toggle = QCheckBox("Speak assistant replies")
        self.tts_toggle.stateChanged.connect(self.on_tts_toggle_changed)
        toggles_row.addWidget(self.tts_toggle)
        right_layout.addLayout(toggles_row)

        self.status_label = QLabel("Status: Ready")
        right_layout.addWidget(self.status_label)

        layout.addWidget(right_panel, stretch=1)
        self._start_auto_scanner()
        
    def _start_auto_scanner(self):
        def status_callback(msg: str):
            clean_msg = msg.replace("Idle training: indexing documents...", "🔍 Scanning searchable mirror...")
            clean_msg = clean_msg.replace("Idle training: paused", "✅ Mirror scan paused (Idle)")
            self.indexer_status_signal.emit(clean_msg)

        self.doc_indexer, self.indexer_controller = build_default_indexer(self.db, status_cb=status_callback)
        self.indexer_thread = threading.Thread(target=self.indexer_controller._run, daemon=True)
        self.indexer_thread.start()    
        
    def _on_file_dropped(self, file_path: str):
        self.attached_file_path = file_path    

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

        delete_btn = QPushButton("Delete Session")
        delete_btn.clicked.connect(self.on_delete_selected_history_session)
        top_row.addWidget(delete_btn)
        layout.addLayout(top_row)

        self.history_box = QTextEdit()
        self.history_box.setReadOnly(True)
        layout.addWidget(self.history_box, stretch=1)

    def _build_settings_tab(self) -> None:
        layout = QVBoxLayout(self.settings_tab)
        form = QFormLayout()

        self.reasoning_model_input = QLineEdit(self.reasoning_model)
        self.reasoning_model_input.setReadOnly(True)
        form.addRow("Reasoning core", self.reasoning_model_input)

        self.online_mode_combo = QComboBox()
        self.online_mode_combo.addItem("Offline only", "offline")
        form.addRow("Network setting", self.online_mode_combo)

        self.pref_rag_cb = QCheckBox("Enable local RAG memory")
        self.pref_tts_cb = QCheckBox("Enable text to speech conversions")
        
        form.addRow("Active Toggles", self.pref_rag_cb)
        form.addRow("", self.pref_tts_cb)
        layout.addLayout(form)

        button_row = QHBoxLayout()
        save_btn = QPushButton("Apply Parameters")
        save_btn.clicked.connect(self.on_save_preferences_clicked)
        button_row.addWidget(save_btn)

        forget_btn = QPushButton("Clear Cache Login")
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
        self.rad_category_combo.addItems(["user_fact", "preference", "task", "Searchable Mirror"])
        self.rad_category_combo.currentIndexChanged.connect(self._load_rad_table) 
        form_row.addWidget(self.rad_category_combo)

        self.rad_key_input = QLineEdit()
        self.rad_key_input.setPlaceholderText("Key descriptor")
        form_row.addWidget(self.rad_key_input)

        self.rad_value_input = QLineEdit()
        self.rad_value_input.setPlaceholderText("Value payload")
        form_row.addWidget(self.rad_value_input)

        add_btn = QPushButton("Inject Fact")
        add_btn.clicked.connect(self.on_add_rad_clicked)
        form_row.addWidget(add_btn)
        layout.addLayout(form_row)

        self.rad_table = QTableWidget()
        self.rad_table.setColumnCount(2) 
        self.rad_table.setHorizontalHeaderLabels(["File Path", "Snippet"])
        self.rad_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.rad_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(self.rad_table, stretch=1)

        row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh Mirror Data")
        refresh_btn.clicked.connect(self._load_rad_table) 
        row.addWidget(refresh_btn)
        layout.addLayout(row)

    def _build_help_tab(self):
        layout = QVBoxLayout(self.help_tab)
        help_text = QTextEdit()
        help_text.setReadOnly(True)
        help_text.setHtml("""
        <h2>MARIE System Guide & Commands</h2>
        <p>Welcome to your personal AI assistant! Here is a cheat sheet on how to interact with the system.</p>
        <h3>📅 Calendar & Meetings</h3>
    <ul>
        <li><b>Schedule meeting:</b> <code>Schedule a [duration] [subject] meeting with [email] on [date/time] in room [location]</code></li>
        <li><b>Send invite:</b> <code>Send an invite to [email] for a [subject] appointment on [date/time] for [duration] minutes</code></li>
    </ul>

    <h3>✉️ Email & Messaging</h3>
    <ul>
        <li><b>Draft email:</b> <code>Email draft to [email] subject [subject] body [message] attach [file]</code></li>
        <li><b>Send email:</b> <code>Email send to [email] subject [subject] body [message] provider [gmail/outlook]</code></li>
        <li><b>Telegram:</b> <code>Telegram send to [chat_id] token [bot_token] message [message]</code></li>
        <li><b>WhatsApp:</b> <code>Whatsapp send to [phone_number] message [message]</code></li>
    </ul>

    <h3>📁 File & Folder Operations</h3>
    <ul>
        <li><b>Construction:</b> <code>Files create file [path] content [text]</code> or <code>Files create folder [path]</code></li>
        <li><b>Manipulation:</b> <code>Files move [source] -> [dest]</code> or <code>Files copy [source] -> [dest]</code></li>
        <li><b>Removal:</b> <code>Files delete [path]</code></li>
        <li><b>Search:</b> <code>Files deep search [query] in [root]</code></li>
    </ul>

    <h3>📊 Office & Document Automation</h3>
    <ul>
        <li><b>Excel:</b> <code>Excel random table [rows]x[cols] with graph in [file].xlsx</code></li>
        <li><b>Excel Math:</b> <code>Excel sum [range] in [file].xlsx to [cell]</code> or <code>Excel formula [cell] = [formula] in [file].xlsx</code></li>
        <li><b>Word:</b> <code>Word add heading [text] level [1-6] in [file].docx</code></li>
        <li><b>PowerPoint:</b> <code>Powerpoint add slide title [title] content [text] in [file].pptx</code></li>
    </ul>

    <h3>💻 System & Media Controls</h3>
    <ul>
        <li><b>Web Research:</b> <code>Web research [topic]</code></li>
        <li><b>Navigation:</b> <code>Open website [url]</code> or <code>Browse [url]</code></li>
        <li><b>Media:</b> <code>Play [topic]</code></li>
        <li><b>App Control:</b> <code>Open [app name]</code> or <code>Close [app name]</code></li>
        <li><b>Note taking:</b> <code>Take a note [text]</code></li>
        <li><b>Clipboard Save:</b> <code>Save clipboard to rad as [key name]</code></li>
    </ul>
    <h3>📥 The File Drop Guide</h3>
        <p>When dragging and dropping files into the input box, follow these rules to ensure the reasoning core reliably reads and processes the data:</p>
        
        <ul>
            <li><strong>The "One-Shot" Rule:</strong> Always drop the file and type your specific instructions in the <em>exact same message</em>. The agent reads the parsed data attached to the current prompt.
                <br><em>Example:</em> <code>[Drop File] Extract the top 3 error codes from this log and summarize them.</code>
            </li>
            <li><strong>The Generation Rule (Avoiding the Fast-Path):</strong> When you want to turn dropped data into a new file, avoid simple command words like "create" or "make". Force the LLM to use its internal reasoning tools.
                <br><em>Example:</em> <code>[Drop File] Analyze this JSON data and use your tools to generate an Excel report detailing the findings.</code>
            </li>
            <li><strong>The Heavy Document Alternative:</strong> If a file is too massive (e.g., overflowing the context window memory limits), provide the explicit file path so the system can use the document analysis tool instead of direct embedding.
                <br><em>Example:</em> <code>Read the document at C:/Documents/large_report.pdf and extract the key financial metrics.</code>
            </li>
        </ul>
        """)
        layout.addWidget(help_text)

    # --- Live2D Integration ---
    def init_live2d_embedding(self) -> None:
        if not LIVE2D_AVAILABLE or os.name != "nt":
            self._set_avatar_status("Avatar rendering uninitialized.")
            return
        model_file = Path(self.model_path).expanduser().resolve()
        if not model_file.exists():
            self._set_avatar_status("Model matching file unresolvable.")
            return
        try:
            pygame.init()
            self.screen = pygame.display.set_mode((450, 600), DOUBLEBUF | OPENGL | NOFRAME)
            pygame_hwnd = pygame.display.get_wm_info().get("window")
            win32gui.SetParent(pygame_hwnd, int(self.face_container.winId()))
            win32gui.SetWindowPos(pygame_hwnd, win32con.HWND_TOP, 0, 30, 450, 570, win32con.SWP_SHOWWINDOW)

            live2d.init()
            live2d.glInit()
            self.model = live2d.LAppModel()
            self.model.LoadModelJson(str(model_file))
            self.model.Resize(450, 600)

            self.face_placeholder.hide()
            self.anim_timer = QTimer(self)
            self.anim_timer.timeout.connect(self.update_live2d_frame)
            self._set_live2d_rendering_enabled(True)
        except Exception as exc:
            self._set_avatar_status(f"Error packing profile frame mapping bindings: {exc}")

    def update_live2d_frame(self) -> None:
        if not self.model: return
        global LAST_EVENT_POLL_TIME
        current_time = time.time() 
        if (current_time - LAST_EVENT_POLL_TIME) > _EVENT_POLL_INTERVAL_SEC: 
            pygame.event.get()
        LAST_EVENT_POLL_TIME = current_time
        
        self.t_breath += 0.05
        self.model.SetParameterValue("ParamBreath", (math.sin(self.t_breath) + 1) / 2)
        self.model.Update()
        live2d.clearBuffer(0.08, 0.1, 0.14, 1.0)
        self.model.Draw()
        pygame.display.flip()

    def _set_avatar_status(self, message: str) -> None:
        self.face_placeholder.setText(message)

    def _set_live2d_rendering_enabled(self, enabled: bool) -> None:
        if self.anim_timer and self.model:
            self.anim_timer.start(16) if enabled else self.anim_timer.stop()

    def on_minimize_avatar_clicked(self) -> None:
        self.face_container.setVisible(False)
        self.restore_avatar_btn.setVisible(True)
        self._set_live2d_rendering_enabled(False)

    def on_restore_avatar_clicked(self) -> None:
        self.face_container.setVisible(True)
        self.restore_avatar_btn.setVisible(False)
        self._set_live2d_rendering_enabled(True)

    # --- Chat Handlers ---
    def on_send_clicked(self) -> None:
        message = self.input_line.text().strip()
        if not message: return
        
        if (getattr(self, 'worker', None) and self.worker.isRunning()) or \
           (getattr(self, 'fast_worker', None) and self.fast_worker.isRunning()) or \
           (getattr(self, 'action_worker', None) and self.action_worker.isRunning()):
            self._set_status_core("Please wait, task in progress...")
            return
        
        self.input_line.clear()
        self._append_chat("You", message)
        
        temp_handler = ActionHandler()
        if temp_handler._looks_like_action_command(message) and not self.response_only_mode:
            self._set_status_core("Executing instantly...")
            self.fast_worker = FastNativeActionWorker(raw_text=message)
            self.fast_worker.finished_action.connect(self._on_action_completed)
            self.fast_worker.start()
            return
            
        self._set_status_core("Thinking...")
        
        if hasattr(self, 'attached_file_path') and self.attached_file_path:
            self._set_status_core("Converting file to JSON...")
            file_json_result = convert_file_to_json(self.attached_file_path)
            
            if file_json_result.get("success"):
                json_string = json.dumps(file_json_result["data"])
                message = f"{message}\n\n[USER UPLOADED FILE DATA (JSON)]:\n{json_string}"
            else:
                self._append_chat("System", f"Failed to read file: {file_json_result.get('error')}")
            self.attached_file_path = None
        
        clean_ui_message = message.split("\n\n[USER UPLOADED FILE DATA")[0]
        self.agent.reset_stop()
        self._latest_user_text = message

        if _looks_like_legacy_action_command(message) and self.allow_legacy_text_actions:
            self._set_status_core("Running Command Thread")
            self.action_worker = ActionWorker(message)
            self.action_worker.finished_action.connect(self._on_action_completed)
            self.action_worker.start()
            return

        self.worker = AgentWorker(self.agent, message)
        self.chat_box.append("<b>Assistant:</b> ")
        self.worker.token_received.connect(self.on_token_streamed)
        self.worker.finished_text.connect(self._on_agent_reply)
        self.worker.failed_text.connect(lambda err: self._append_chat("System", err))
        self.worker.finished.connect(lambda: self._set_status_core("Ready"))
        
        self.worker.start()
        self._set_status_core("Thinking")

    def on_token_streamed(self, token: str) -> None:
        cursor = self.chat_box.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(token)
        self.chat_box.setTextCursor(cursor)

    def _on_agent_reply(self, text: str) -> None:
        # Check if the text was already streamed to the screen
        is_streamed = text.startswith("[STREAMED]")
        if is_streamed:
            text = text.replace("[STREAMED]", "", 1)

        cmd = self._extract_json_command(text) 
        if cmd:          
            self._run_tool_bridge_action(cmd)
            text = re.sub(r'\{.*\}', '', text)
        
        safe_text = self.agent.clean_output_for_ui(text)
        if safe_text:
            # Only append to the chat box if it wasn't already streamed
            if not is_streamed:
                self._append_chat("Assistant", safe_text)
            
            # --- OFFICIAL VOICE PATCH ---
            if self.tts_toggle.isChecked():
                import requests
                import threading
                
                def speak_text():
                    try:
                        voice_url = "http://127.0.0.1:8001/speak"
                        requests.post(voice_url, json={"text": safe_text}, timeout=2)
                    except Exception as e:
                        print(f"[VOICE ERROR] {e}")
                        
                threading.Thread(target=speak_text, daemon=True).start()
            # ----------------------------

        self.db.auto_store_important_conversation_data(self.current_user_id, self._latest_user_text, safe_text)
        self._load_rad_table()

    def _on_action_completed(self, success: bool, output: str) -> None:
        tag = "Action" if success else "Action Error"
        if output:
            self._append_chat(tag, output)
        else:
            self._append_chat("Action", "Sure.") if success else self._append_chat("Action Error", "There is some problem with my system.")
        self._set_status_core("Ready")

    def on_stop_clicked(self) -> None:
        self.agent.stop()
        self._set_status_core("Cancellation Requested...")
        self.chat_box.append("<p style='color: #ff4d6d;'><b>System:</b> Halting active inference tasks cooperatively...</p>")
        QTimer.singleShot(800, lambda: self._set_status_core("Stopped"))
        if self.json_action_worker: self.json_action_worker.cancel()

    def on_resume_clicked(self) -> None:
        self.agent.reset_stop()
        self._set_status_core("Ready")

    # --- Data & Preferences ---
    def on_rag_toggle_changed(self, state: int) -> None:
        self.agent.set_rag_enabled(state == Qt.Checked)

    def on_tts_toggle_changed(self, state: int) -> None:
        pass

    def on_forget_autologin_clicked(self) -> None:
        clear_auto_login_user()
        QMessageBox.information(self, "Auto-login", "Remembered login has been cleared.")

    def on_save_preferences_clicked(self) -> None:
        rag_enabled = self.pref_rag_cb.isChecked()
        tts_enabled = self.pref_tts_cb.isChecked()
        self.rag_toggle.setChecked(rag_enabled)
        self.tts_toggle.setChecked(tts_enabled)
        self.agent.set_rag_enabled(rag_enabled)
        
        payload = {
            "rag_enabled": rag_enabled,
            "tts_enabled": tts_enabled,
            "preferred_reasoning_model": self.reasoning_model,
            "preferred_live2d_model": "./models/Knight/knight.model3.json",
            "speaking_speed": self.speaking_speed,
            "preferred_voice": self.voice_profile
        }

        try:
            self.db.save_user_preference(self.current_user_id, payload)
            self._load_rad_table()
            self._append_chat("System", "Configuration preferences written and applied successfully.")
            QMessageBox.information(self, "Preferences", "Parameters written successfully.")
        except Exception as exc:
            self._append_chat("System", f"Failed to write configuration context: {exc}")
            QMessageBox.critical(self, "Database Error", f"Could not update options: {exc}")

    def on_logout_clicked(self) -> None:
        confirm = QMessageBox.question(self, "Log Out", "Log out now? The app window will close.", QMessageBox.Yes | QMessageBox.No)
        if confirm == QMessageBox.Yes:
            clear_auto_login_user()
            self.logout_requested = True
            self.close()

    def _refresh_history_sessions(self) -> None:
        self.history_session_combo.clear()
        for session in self.db.list_sessions(limit=40):
            self.history_session_combo.addItem(f"{session['started_at']} | {session['session_id']}", session["session_id"])

    def _load_selected_session_history(self) -> None:
        session_id = self.history_session_combo.currentData()
        if not session_id: return
        parts = [f"<p><b>{row.get('role')}</b>: {html.escape(row.get('message'))}</p>" for row in self.db.get_all_session_messages(session_id)]
        self.history_box.setHtml("".join(parts))

    def on_delete_selected_history_session(self) -> None:
        session_id = self.history_session_combo.currentData()
        if session_id and session_id != self.agent.session_id and self.db.delete_session_history(session_id):
            self._refresh_history_sessions()

    def _load_preferences_into_ui(self) -> None:
        self.rag_toggle.setChecked(bool(self.preferences.get("rag_enabled", True)))
        self.tts_toggle.setChecked(bool(self.preferences.get("tts_enabled", False)))
        self.pref_rag_cb.setChecked(self.rag_toggle.isChecked())
        self.pref_tts_cb.setChecked(self.tts_toggle.isChecked())
        self._refresh_history_sessions()
        
        self._load_rad_table()

    def _load_rad_table(self) -> None:
        selected_category = self.rad_category_combo.currentText()
        self.rad_table.setRowCount(0)
        
        if selected_category == "Searchable Mirror":
            self.rad_table.setColumnCount(2)
            self.rad_table.setHorizontalHeaderLabels(["File Path", "Snippet"])
            results = self.db.list_all_searchable_mirror(limit=50)
            
            for idx, row in enumerate(results):
                self.rad_table.insertRow(idx)
                self.rad_table.setItem(idx, 0, QTableWidgetItem(str(row.get("file_path", ""))))
                self.rad_table.setItem(idx, 1, QTableWidgetItem(str(row.get("snippet", ""))))
        else:
            self.rad_table.setColumnCount(6)
            self.rad_table.setHorizontalHeaderLabels(["ID", "Category", "Key", "Value", "Confidence", "Created"])
            rows = self.db.get_rad_data(self.current_user_id, limit=50)
            filtered_rows = [r for r in rows if r.get("category") == selected_category]
            
            for idx, row in enumerate(filtered_rows):
                self.rad_table.insertRow(idx)
                self.rad_table.setItem(idx, 0, QTableWidgetItem(str(row.get("id", ""))))
                self.rad_table.setItem(idx, 1, QTableWidgetItem(str(row.get("category", ""))))
                self.rad_table.setItem(idx, 2, QTableWidgetItem(str(row.get("key_data", ""))))
                self.rad_table.setItem(idx, 3, QTableWidgetItem(str(row.get("value_data", ""))))
                self.rad_table.setItem(idx, 4, QTableWidgetItem(str(row.get("confidence_score", ""))))
                self.rad_table.setItem(idx, 5, QTableWidgetItem(str(row.get("created_at", ""))))
        
    def on_add_rad_clicked(self) -> None:
        k, v = self.rad_key_input.text().strip(), self.rad_value_input.text().strip()
        if k and v:
            self.db.add_rad_data(self.current_user_id, self.rad_category_combo.currentText(), k, v)
            self.rad_key_input.clear()
            self.rad_value_input.clear()
            self._load_rad_table()

    # --- Internal Utilities ---
    def _append_chat(self, speaker: str, text: str) -> None:
        self.chat_box.append(f"<b>{html.escape(speaker)}:</b> {html.escape(text)}")

    def _set_status_core(self, status: str) -> None:
        self._status_core = status
        self._update_status_strip()

    def _update_status_strip(self) -> None:
        self.status_label.setText(f"Status: {self._status_core} | Memory Sync Mode: Connected")

    def _extract_json_command(self, text: str) -> Optional[Dict]:
        if not text: return None
        
        tool_match = re.search(r"<tool>(.*?)</tool>", text, flags=re.IGNORECASE | re.DOTALL)
        if tool_match:
            candidate = tool_match.group(1).strip()
            candidate = re.sub(r"^```json\s*", "", candidate, flags=re.IGNORECASE)
            candidate = re.sub(r"^```\s*", "", candidate)
            candidate = re.sub(r"\s*```$", "", candidate).strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and "action" in parsed: return parsed
            except json.JSONDecodeError: pass

        fenced = re.findall(r"```json\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
        for candidate in fenced:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and "action" in parsed: return parsed
            except json.JSONDecodeError: continue

        loose = re.findall(r"(\{\s*\"action\"[\s\S]*?\})", text)
        for candidate in loose:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and "action" in parsed: return parsed
            except json.JSONDecodeError: continue
                
        return None

    def _run_tool_bridge_action(self, cmd: Dict) -> None:
        self._set_status_core("Executing Tool...")
        self.json_action_worker = JsonActionWorker(cmd)
        self.json_action_worker.finished_action.connect(self._on_action_completed)
        self.json_action_worker.start()
        
    def closeEvent(self, event):
        self._set_status_core("Shutting down...")
        
        if getattr(self, 'worker', None) and self.worker.isRunning():
            self.agent.stop()
            self.worker.quit()
            self.worker.wait()
            
        if getattr(self, 'fast_worker', None) and self.fast_worker.isRunning():
            self.fast_worker.quit()
            self.fast_worker.wait()
            
        if getattr(self, 'action_worker', None) and self.action_worker.isRunning():
            self.action_worker.quit()
            self.action_worker.wait()
            
        if getattr(self, 'json_action_worker', None) and self.json_action_worker.isRunning():
            self.json_action_worker.cancel()
            self.json_action_worker.quit()
            self.json_action_worker.wait()
            
        super().closeEvent(event)

# ==========================================
# 8. ENTRY POINT
# ==========================================
def main() -> None:
    app = QApplication(sys.argv)
    db = DatabaseManager(db_path=str(CONFIG.get("paths", {}).get("db_path", "cache/assistant_sessions.db")))
    auth_data = None

    remembered_user = load_auto_login_user()
    if remembered_user is not None:
        auth_data = db.resume_user_session(remembered_user)

    if auth_data is None:
        login = LoginDialog(db)
        if login.exec_() != QDialog.Accepted or not login.auth_result:
            db.close()
            return
        auth_data = login.auth_result
        if login.remember_cb.isChecked():
            save_auto_login_user(int(auth_data.get("user_id", 0)))

    window = AssistantMainWindow(
        db=db, 
        user_id=int(auth_data.get("user_id", 0)), 
        username=str(auth_data.get("username", "user"))
    )
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()