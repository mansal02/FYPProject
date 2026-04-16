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
import html
import json
import math
import os
import re
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
from aiassistant.infra.vision.screen_vision import PYAUTOGUI_AVAILABLE, capture_screen_snapshot
from aiassistant.infra.vision.vision_audio import CameraTracker, SpeechListener, TextToSpeechEngine
from aiassistant.tools.action import ActionHandler

try:
    from aiassistant.infra.voice.voice_db import CHARACTERS, get_character_data
except Exception:
    CHARACTERS = {}

    def get_character_data(_char_id):
        raise RuntimeError("voice_db is unavailable")


try:
    import pygame
    from pygame.locals import DOUBLEBUF, NOFRAME, OPENGL
    from live2d import v3 as live2d
    import win32con
    import win32gui

    LIVE2D_AVAILABLE = True
except Exception:
    pygame = None
    live2d = None
    win32con = None
    win32gui = None
    DOUBLEBUF = NOFRAME = OPENGL = 0
    LIVE2D_AVAILABLE = False


AUTO_LOGIN_FILE = str(CONFIG.get("paths", {}).get("auto_login_file", "./.marie_autologin.json"))
DEFAULT_REASONING_MODEL = str(CONFIG.get("ollama", {}).get("model", "llama3.2:3b"))
DEFAULT_VISION_MODEL = str(CONFIG.get("vision", {}).get("vision_model", "moondream") or "moondream")
DEFAULT_LIVE2D_MODEL = str(CONFIG.get("paths", {}).get("default_live2d_model", ""))
DEFAULT_TTS_SPEED = float(CONFIG.get("voice", {}).get("speaking_speed", 1.0))


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

        self.db = db
        self.current_user_id = int(user_id)
        self.current_username = str(username)
        self.preferences = self.db.get_user_preference(self.current_user_id)
        self.logout_requested = False
        self._status_core = "Ready"
        self._tts_unavailable_notified = False

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
        self.allow_legacy_text_actions = bool(CONFIG.get("actions", {}).get("allow_legacy_text_commands", True))
        self.actions: Optional[ActionHandler] = None

        self.camera = CameraTracker(camera_index=0)
        self.speech = SpeechListener()
        self.voice_allow_online_fallback = bool(CONFIG.get("voice", {}).get("allow_online_fallback", True))
        self.voice_mode = "Silent-Command"
        self.speech_bridge = SpeechBridge()
        self.speech_bridge.transcribed.connect(self._on_speech_transcribed)
        self.wake_words = self._build_wake_words()

        self.tts = self._create_tts_engine(self.voice_profile, self.speaking_speed)
        self.tts.start()

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

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_status_strip)
        self.status_timer.start(1000)

        QTimer.singleShot(150, self.init_live2d_embedding)

        if not self.tts.is_available():
            self._append_chat(
                "System",
                "Voice output is unavailable. Install pyttsx3 or add piper.exe to the piper folder.",
            )

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
        form.addRow("Voice profile", self.voice_profile_combo)

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
        help_text.setHtml(
            """
            <h2>How to Use</h2>
            <p><b>1) Login:</b> You can create an account and optionally enable remembered auto-login.</p>
            <p><b>2) Assistant tab:</b> Type commands, use voice command listening, and enable screen input when needed.</p>
            <p><b>3) Settings tab:</b> Change voice, reasoning model, vision model, avatar model, and log out.</p>
            <p><b>4) RAD tab:</b> Check and edit your stored rapid-access facts.</p>
            <p><b>5) Screen options:</b> Turn on live screen as model input and optionally view live preview.</p>
            <p><b>6) STOP PROCESS:</b> Immediate safety interrupt for active requests.</p>
            <hr>
            <p><b>Note:</b> Voice output uses either pyttsx3 (system voice) or Piper character profiles if piper.exe is available.</p>
            """
        )
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

        self.agent.set_screen_capture_enabled(self.screen_toggle.isChecked())
        self.agent.set_rag_enabled(self.rag_toggle.isChecked())

        self._set_voice_input_enabled(self.voice_toggle.isChecked(), announce=False)
        self._set_screen_preview_enabled(self.screen_preview_toggle.isChecked(), announce=False)
        self._set_desktop_mate_enabled(self.desktop_mate_toggle.isChecked(), announce=False)
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

    def _create_tts_engine(self, profile: str, speaking_speed: float) -> TextToSpeechEngine:
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
            display_name = str(data.get("name") or char_id)
            self.voice_profile_combo.addItem(
                f"Character: {display_name} ({char_id})",
                f"character:{char_id}",
            )

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

    def init_live2d_embedding(self) -> None:
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
            self.anim_timer.start(16)

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

    # --- chat actions ---
    def on_send_clicked(self) -> None:
        if self.worker and self.worker.isRunning():
            self._set_status_core("Busy processing current request")
            return

        message = self.input_line.text().strip()
        if not message:
            return

        self.agent.reset_stop()
        self.input_line.clear()
        self._append_chat("You", message)

        if self.allow_legacy_text_actions:
            try:
                if self.actions is None:
                    self.actions = ActionHandler()
                action_result = self.actions.execute_and_collect(message)
                if action_result:
                    self._append_chat("Action", action_result)
            except Exception as exc:
                self._append_chat("System", f"Action module error: {exc}")

        self.worker = AgentWorker(self.agent, message)
        self.worker.finished_text.connect(self._on_agent_reply)
        self.worker.failed_text.connect(self._on_agent_error)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

        self._set_status_core("Thinking")

    def on_stop_clicked(self) -> None:
        self.agent.stop()
        self._set_status_core("STOP signal sent")
        self._append_chat("System", "Stop signal delivered. Current process will halt safely.")

    def on_resume_clicked(self) -> None:
        self.agent.reset_stop()
        self._set_status_core("Ready")
        self._append_chat("System", "Processing resumed.")

    # --- toggle handlers ---
    def _set_screen_input_enabled(self, enabled: bool, announce: bool = True) -> None:
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

    def _set_voice_input_enabled(self, enabled: bool, announce: bool = True) -> None:
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

        # Ensure listener backends are not duplicated.
        self.speech.stop_background_listening()
        self.speech.stop_wake_word_listener()

        wake_backend = "none"
        if enabled:
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
                )

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
        if self.worker and self.worker.isRunning():
            return

        heard = self.speech.listen_once(
            timeout=3.0,
            phrase_time_limit=8.0,
            wake_words=None,
            allow_online_fallback=self.voice_allow_online_fallback,
        )
        if heard:
            self.speech_bridge.transcribed.emit(heard)

    def on_voice_toggle_changed(self, state: int) -> None:
        self._set_voice_input_enabled(state == Qt.Checked)

    def _set_rag_enabled(self, enabled: bool, announce: bool = True) -> None:
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
        enabled = bool(enabled)
        self._set_checkbox_checked(self.tts_toggle, enabled)
        self._sync_preference_checkbox_states()
        self._persist_runtime_preferences()
        if announce:
            self._append_chat("System", f"Voice output {'enabled' if enabled else 'disabled'}.")

    def on_tts_toggle_changed(self, state: int) -> None:
        self._set_tts_enabled(state == Qt.Checked)

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
        safe_text = self.agent.clean_output_for_ui(text)
        self._append_chat("Assistant", safe_text)

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
        self._append_chat("System", err)

    def _on_worker_finished(self) -> None:
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

        self._append_chat("Mic", cleaned)
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
        self.tts.speak("Voice test successful. Settings were applied.")

    def on_reload_avatar_clicked(self) -> None:
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
            if hasattr(self, "face_container"):
                self.face_container.hide()
        else:
            flags = Qt.Window
            self.setWindowFlags(flags)
            self.setAttribute(Qt.WA_TranslucentBackground, False)
            self.setWindowOpacity(1.0)
            self.resize(1180, 780)
            if hasattr(self, "face_container"):
                self.face_container.show()

        self.show()

    # --- lifecycle ---
    def closeEvent(self, event) -> None:  # type: ignore[override]
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
    args = _parse_args()

    if args.reset_login:
        clear_auto_login_user()
        print("[LOGIN] Remembered auto-login cleared.")
        return

    app = QApplication(sys.argv)
    db = DatabaseManager(db_path=str(CONFIG.get("paths", {}).get("db_path", "cache/assistant_sessions.db")))

    auth_data: Optional[Dict[str, object]] = None

    if not args.force_login:
        remembered_user = load_auto_login_user()
        if remembered_user is not None:
            auth_data = db.resume_user_session(remembered_user)
            if auth_data is None:
                clear_auto_login_user()

    if auth_data is None:
        login = LoginDialog(db)
        if login.exec_() != QDialog.Accepted or not login.auth_result:
            db.close()
            return

        auth_data = login.auth_result
        if login.remember_cb.isChecked():
            save_auto_login_user(int(auth_data.get("user_id", 0)))
        else:
            clear_auto_login_user()

    user_id = int(auth_data.get("user_id", 0))
    username = str(auth_data.get("username", "user"))

    window = AssistantMainWindow(db=db, user_id=user_id, username=username)
    window.show()

    exit_code = app.exec_()
    db.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
