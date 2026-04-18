import argparse
import html
import json
import sys
import requests  
import os
import re
import subprocess
import threading
import pygame
import math
import time
import random
import keyboard
from urllib.parse import urlparse
from aiassistant.infra.voice.hear import VoiceWorker
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QTextEdit, QLineEdit, QPushButton, QLabel, QFrame,
                             QDialog, QTabWidget, QFormLayout, QComboBox, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QMessageBox, QFileDialog, QCheckBox,
                             QSystemTrayIcon, QMenu, QAction, QStyle)
from PyQt5.QtCore import pyqtSignal, Qt, QObject, QTimer
from aiassistant.core.event_bus import EventBus, Events
from aiassistant.infra.config.app_config import CONFIG
from aiassistant.infra.vision.screen_vision import PYAUTOGUI_AVAILABLE, capture_screen_snapshot
from aiassistant.workers.reasoning_worker import ReasoningStreamWorker

# --- LIVE2D IMPORT ---
try:
    from live2d import v3 as live2d
    from pygame.locals import *
    import win32gui
    import win32con
except ImportError:
    print("[CRITICAL] Libraries missing. Ensure live2d, pygame, and pywin32 are installed.")

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(ROOT_DIR)

AUTO_LOGIN_FILE = CONFIG["paths"]["auto_login_file"]


def save_auto_login_user(user_id):
    try:
        with open(AUTO_LOGIN_FILE, "w", encoding="utf-8") as f:
            json.dump({"user_id": user_id}, f)
    except Exception as e:
        print(f"[LOGIN] Failed to save one-time login token: {e}")


def load_auto_login_user():
    if not os.path.exists(AUTO_LOGIN_FILE):
        return None

    try:
        with open(AUTO_LOGIN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        user_id = data.get("user_id")
        if isinstance(user_id, int):
            return user_id
    except Exception as e:
        print(f"[LOGIN] Failed to read one-time login token: {e}")

    return None


def clear_auto_login_user():
    if os.path.exists(AUTO_LOGIN_FILE):
        try:
            os.remove(AUTO_LOGIN_FILE)
        except Exception as e:
            print(f"[LOGIN] Failed to clear one-time login token: {e}")


def run_action_command_isolated(text, timeout_sec=50):
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

    if completed.returncode == 0:
        return True, output

    if not output:
        output = f"[ACTION] Isolated runner exited with code {completed.returncode}."
    return False, output


def _looks_like_high_risk_action_command(text):
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False

    if lowered.startswith(
        (
            "open ",
            "close ",
            "software ",
            "service ",
            "files open ",
            "search file ",
            "find file ",
            "locate file ",
        )
    ):
        return True

    if re.search(r"\b(?:open|search|find|locate|look\s+for)\b.*\b(?:file|files|folder|folders|document|documents|path)\b", lowered):
        return True

    if re.search(r"\b(?:open|close)\b\s+[a-z0-9]", lowered):
        return True

    return bool(re.search(r'"action"\s*:\s*"(open|close|open_file|search_file|semantic_search_file|deep_search|deep_search_paths)"', lowered))

from aiassistant.infra.db.database import MarieDB
from aiassistant.infra.voice.voice_db import CHARACTERS
from aiassistant.tools.action import ActionHandler

# 1. LOGIN DIALOG
class LoginDialog(QDialog):
    def __init__(self, db_instance):
        super().__init__()
        self.db = db_instance
        self.setWindowTitle("MARIE - User Login")
        self.setFixedSize(320, 230)
        self.user_id = None
        self.session_id = None
        self.setStyleSheet("background-color: #252526; color: white;")

        layout = QVBoxLayout(self)
        
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Username")
        self.user_input.setStyleSheet("padding: 5px; border: 1px solid #555;")
        
        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Password")
        self.pass_input.setEchoMode(QLineEdit.Password)
        self.pass_input.setStyleSheet("padding: 5px; border: 1px solid #555;")
        
        self.login_btn = QPushButton("Login")
        self.login_btn.clicked.connect(self.handle_login)
        self.login_btn.setStyleSheet("background-color: #007acc; padding: 5px;")
        
        self.reg_btn = QPushButton("Register New Account")
        self.reg_btn.clicked.connect(self.handle_register)
        self.reg_btn.setStyleSheet("background-color: #333; padding: 5px;")

        self.remember_cb = QCheckBox("One-time login (remember this account)")
        self.remember_cb.setChecked(True)
        self.remember_cb.setStyleSheet("color: #d4d4d4;")

        layout.addWidget(QLabel("<h2>Welcome back</h2>"))
        layout.addWidget(self.user_input)
        layout.addWidget(self.pass_input)
        layout.addWidget(self.remember_cb)
        layout.addWidget(self.login_btn)
        layout.addWidget(self.reg_btn)

    def handle_login(self):
        username = self.user_input.text()
        password = self.pass_input.text()
        auth_data = self.db.login_user(username, password)
        
        if auth_data:
            self.user_id, self.session_id = auth_data
            if self.remember_cb.isChecked():
                save_auto_login_user(self.user_id)
            else:
                clear_auto_login_user()
            self.accept()
        else:
            QMessageBox.warning(self, "Error", "Invalid username or password")

    def handle_register(self):
        username = self.user_input.text()
        password = self.pass_input.text()
        if not username or not password:
            QMessageBox.warning(self, "Error", "Fields cannot be empty")
            return
            
        success, msg = self.db.register_user(username, password)
        if success:
            QMessageBox.information(self, "Success", "Account created! You can now login.")
        else:
            QMessageBox.warning(self, "Error", msg)


# 2. SETTINGS DASHBOARD 

class SettingsWindow(QDialog):
    def __init__(self, parent_window):
        super().__init__(parent_window)
        self.main_win = parent_window
        self.db = parent_window.db
        self.uid = parent_window.current_user_id
        self.current_session_id = parent_window.current_session_id
        self.current_session_only = False
        
        self.setWindowTitle("Database & Settings Manager")
        self.resize(700, 500)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")
        
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("""
            QTabBar::tab { background: #333; color: #888; padding: 10px; min-width: 100px; } 
            QTabBar::tab:selected { background: #007acc; color: white; }
            QTableWidget { gridline-color: #444; }
        """)
        
        self.tab_prefs = QWidget()
        self.init_prefs_tab()
        self.tabs.addTab(self.tab_prefs, "Preferences")

        self.tab_logs = QWidget()
        self.init_logs_tab()
        self.tabs.addTab(self.tab_logs, "Chat Logs")

        self.tab_rad = QWidget()
        self.init_rad_tab()
        self.tabs.addTab(self.tab_rad, "RAD Memory")

        layout.addWidget(self.tabs)

    def init_prefs_tab(self):
        layout = QFormLayout(self.tab_prefs)
        
        self.voice_combo = QComboBox()
        self.voice_combo.addItems(list(CHARACTERS.keys()))
        self.voice_combo.setStyleSheet("background: #333; padding: 5px; color: white;")
        
        self.model_path_input = QLineEdit()
        self.model_path_input.setText(self.main_win.model_path)
        self.model_path_input.setStyleSheet("background: #333; padding: 5px; color: white;")
        
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.browse_model)
        browse_btn.setStyleSheet("background: #444; padding: 5px; color: white;")
        
        save_btn = QPushButton("Save & Apply")
        save_btn.setStyleSheet("background: #007acc; padding: 8px; font-weight: bold; color: white;")
        save_btn.clicked.connect(self.save_preferences)

        forget_login_btn = QPushButton("Forget One-Time Login")
        forget_login_btn.setStyleSheet("background: #6a1b1b; padding: 6px; color: white;")
        forget_login_btn.clicked.connect(self.forget_one_time_login)

        layout.addRow("Voice Persona:", self.voice_combo)
        layout.addRow("Model Path:", self.model_path_input)
        layout.addRow("", browse_btn)
        layout.addRow("", save_btn)
        layout.addRow("", forget_login_btn)
        
        prefs = self.db.get_preference(self.uid)
        if prefs:
            voice_id, model_p = prefs
            if voice_id: self.voice_combo.setCurrentText(voice_id)
            if model_p: self.model_path_input.setText(model_p)

    def init_logs_tab(self):
        layout = QVBoxLayout(self.tab_logs)
        
        self.log_table = QTableWidget()
        self.log_table.setColumnCount(6)
        self.log_table.setHorizontalHeaderLabels(["ID", "Session", "Time", "Sender", "Message", "Emotion"])
        self.log_table.hideColumn(0)
        self.log_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.log_table.setSelectionBehavior(QTableWidget.SelectRows)
        
        btn_layout = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.load_logs)
        refresh_btn.setStyleSheet("background: #444; padding: 5px; color: white;")

        delete_btn = QPushButton("Delete Selected Row")
        delete_btn.setStyleSheet("background: #cc3333; padding: 5px; font-weight: bold; color: white;")
        delete_btn.clicked.connect(self.delete_selected_log)

        self.filter_btn = QPushButton("Current Session Only: OFF")
        self.filter_btn.setStyleSheet("background: #505050; padding: 5px; color: white;")
        self.filter_btn.clicked.connect(self.toggle_log_filter)

        clear_btn = QPushButton("Clear ALL My Logs")
        clear_btn.setStyleSheet("background: #8b0000; padding: 5px; color: white;")
        clear_btn.clicked.connect(self.clear_all_logs)
        
        btn_layout.addWidget(refresh_btn)
        btn_layout.addWidget(delete_btn)
        btn_layout.addWidget(self.filter_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(clear_btn)
        
        layout.addWidget(self.log_table)
        layout.addLayout(btn_layout)
        self.load_logs()

    def init_rad_tab(self):
        layout = QVBoxLayout(self.tab_rad)
        
        form_layout = QHBoxLayout()
        self.rad_key = QLineEdit()
        self.rad_key.setPlaceholderText("Key (e.g., 'birthday')")
        self.rad_key.setStyleSheet("background: #333; padding: 5px; color: white;")
        self.rad_val = QLineEdit()
        self.rad_val.setPlaceholderText("Value (e.g., 'Jan 6')")
        self.rad_val.setStyleSheet("background: #333; padding: 5px; color: white;")
        
        add_btn = QPushButton("Add Fact")
        add_btn.setStyleSheet("background: #2d8a2d; padding: 5px; color: white;")
        add_btn.clicked.connect(self.add_rad_fact)
        
        form_layout.addWidget(self.rad_key)
        form_layout.addWidget(self.rad_val)
        form_layout.addWidget(add_btn)
        
        self.rad_table = QTableWidget()
        self.rad_table.setColumnCount(4)
        self.rad_table.setHorizontalHeaderLabels(["ID", "Category", "Key", "Value"])
        self.rad_table.hideColumn(0)
        self.rad_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.rad_table.setSelectionBehavior(QTableWidget.SelectRows)

        del_layout = QHBoxLayout()
        del_btn = QPushButton("Delete Selected Fact")
        del_btn.setStyleSheet("background: #cc3333; padding: 5px; color: white;")
        del_btn.clicked.connect(self.delete_selected_rad)
        del_layout.addStretch()
        del_layout.addWidget(del_btn)
        
        layout.addLayout(form_layout)
        layout.addWidget(self.rad_table)
        layout.addLayout(del_layout)
        self.load_rad_data()

# Logic for browsing model file
    def browse_model(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Select Model3 JSON", "", "Live2D Model (*.model3.json)")
        if fname: self.model_path_input.setText(fname)

    def save_preferences(self):
        voice = self.voice_combo.currentText()
        model = self.model_path_input.text()
        self.db.save_preference(self.uid, voice, model)
        
        self.main_win.current_character = voice
        self.main_win.model_path = model 
        
        QMessageBox.information(self, "Saved", "Preferences saved. (Restart may be needed for Model change)")

    def forget_one_time_login(self):
        clear_auto_login_user()
        QMessageBox.information(self, "Done", "One-time login was cleared.")

    def toggle_log_filter(self):
        self.current_session_only = not self.current_session_only
        state = "ON" if self.current_session_only else "OFF"
        self.filter_btn.setText(f"Current Session Only: {state}")
        self.load_logs()

    def load_logs(self):
        selected_session = self.current_session_id if self.current_session_only else None
        rows = self.db.get_chat_logs(self.uid, selected_session)
        self.log_table.setRowCount(0)
        for row_idx, row_data in enumerate(rows):
            self.log_table.insertRow(row_idx)
            for col_idx, data in enumerate(row_data):
                self.log_table.setItem(row_idx, col_idx, QTableWidgetItem(str(data)))

    def delete_selected_log(self):
        row = self.log_table.currentRow()
        if row >= 0:
            log_id = self.log_table.item(row, 0).text()
            self.db.delete_chat_log(log_id)
            self.log_table.removeRow(row)

    def clear_all_logs(self):
        if self.current_session_only:
            confirm_text = "Delete logs from this session only? This cannot be undone."
        else:
            confirm_text = "Delete ALL your chat history? This cannot be undone."

        confirm = QMessageBox.question(self, "Confirm", confirm_text, QMessageBox.Yes | QMessageBox.No)
        if confirm == QMessageBox.Yes:
            selected_session = self.current_session_id if self.current_session_only else None
            self.db.clear_all_chats(self.uid, selected_session)
            self.load_logs()

    def add_rad_fact(self):
        key = self.rad_key.text()
        val = self.rad_val.text()
        if key and val:
            self.db.add_rad_data("user_fact", key, val)
            self.rad_key.clear()
            self.rad_val.clear()
            self.load_rad_data()

    def load_rad_data(self):
        self.db.cursor.execute("SELECT id, category, key_data, value_data FROM rad_memory")
        rows = self.db.cursor.fetchall()
        self.rad_table.setRowCount(0)
        for row_idx, row_data in enumerate(rows):
            self.rad_table.insertRow(row_idx)
            for col_idx, data in enumerate(row_data):
                self.rad_table.setItem(row_idx, col_idx, QTableWidgetItem(str(data)))

    def delete_selected_rad(self):
        row = self.rad_table.currentRow()
        if row >= 0:
            rad_id = self.rad_table.item(row, 0).text()
            self.db.delete_rad_data(rad_id)
            self.rad_table.removeRow(row)

# 3. MAIN APPLICATION WINDOW
class StreamSignals(QObject):
    new_token = pyqtSignal(str)
    finished = pyqtSignal(str)
    voice_hold = pyqtSignal(int)

class MainWindow(QMainWindow):
    def __init__(self, user_id, session_id, db_instance, app_mode="both", transparent_face=False):
        super().__init__()
        self.current_user_id = user_id
        self.current_session_id = session_id
        self.db = db_instance
        self.app_mode = app_mode
        self.transparent_face = bool(transparent_face or CONFIG.get("ui", {}).get("transparent_face", False))
        self.voice_input_enabled = app_mode in ("both", "voice")
        self.voice_output_enabled = app_mode in ("both", "voice")
        self.text_input_enabled = app_mode in ("both", "gui")
        self.ui_theme = str(CONFIG.get("ui", {}).get("theme", "dark")).lower()
        self.screen_share_enabled = bool(CONFIG.get("vision", {}).get("screen_share_enabled", False))
        self.screen_capture_warned = False
        
        self.setWindowTitle("MARIE - Intelligent Environment")
        self.resize(1100, 700)
        self._apply_theme()

        self.brain_url = CONFIG["servers"]["reasoning_url"]
        self.brain_stream_url = CONFIG["servers"]["reasoning_stream_url"]
        self.brain_stop_url = CONFIG["servers"]["reasoning_stop_url"]
        self.voice_url = CONFIG["servers"]["voice_url"]
        self.voice_stop_url = CONFIG["servers"]["voice_stop_url"]
        self.mic_hotkey = CONFIG["voice"].get("microphone_hotkey", "F4")
        self.summon_hotkey = CONFIG["voice"].get("summon_hotkey", "ctrl+space")

        self.event_bus = EventBus()
        self.actions = ActionHandler(
            db=self.db,
            context_provider=lambda: {
                "user_id": self.current_user_id,
                "session_id": self.current_session_id,
            },
        )
        self.signals = StreamSignals()
        
        self.is_speaking_remotely = False
        self.is_processing_response = False
        self.waiting_for_voice_finish = False
        self.voice_release_deadline = 0.0
        self.voice_release_token = 0
        self.viseme_timeline = []
        self.viseme_lock = threading.RLock()
        self.current_viseme_value = 0.0
        self.latest_user_text = ""
        self.latest_action_result = ""
        self.voice_thread = None
        self.reasoning_worker = None
        self.current_request_id = None
        self.hotkey_id = None
        self.summon_hotkey_id = None
        self.tray_icon = None
        
        self.model_path = CONFIG["paths"]["default_live2d_model"]
        self.current_character = CONFIG["voice"].get("default_character", "tachyon")

        prefs = self.db.get_preference(self.current_user_id)
        if prefs:
            saved_voice, saved_model = prefs
            if saved_voice: self.current_character = saved_voice
            if saved_model: self.model_path = saved_model

        self._bind_events()

        self.init_ui()
        self.init_system_tray()

        if self.text_input_enabled:
            self.input_field.setEnabled(True)
            self.send_btn.setEnabled(True)
        else:
            self.input_field.setEnabled(False)
            self.send_btn.setEnabled(False)
            self.input_field.setPlaceholderText("Voice-only mode is active.")
        
        # VOICE INTEGRATION
        if self.voice_input_enabled:
            self.voice_thread = VoiceWorker(wake_word=CONFIG["voice"].get("wake_word", "hey marie"))
            self.voice_thread.keyword_mode = True
            self.voice_thread.text_received.connect(self.handle_voice_input)
            self.voice_thread.status_update.connect(self.update_voice_status)
            self.voice_thread.speech_detected.connect(self.handle_speech_activity)
            self.voice_thread.start()
            try:
                self.hotkey_id = keyboard.add_hotkey(self.mic_hotkey, self.voice_thread.toggle_listening)
            except Exception as e:
                print(f"[HOTKEY] Failed to register mic hotkey '{self.mic_hotkey}': {e}")
            try:
                self.summon_hotkey_id = keyboard.add_hotkey(
                    self.summon_hotkey,
                    lambda: QTimer.singleShot(0, self.toggle_window_visibility),
                )
            except Exception as e:
                print(f"[HOTKEY] Failed to register summon hotkey '{self.summon_hotkey}': {e}")

            if self.app_mode == "voice":
                self.voice_thread.is_active = False
                self.voice_thread.keyword_mode = True
                self.voice_label.setText("[Keyword Mode]")
                self.voice_label.setStyleSheet("color: #4ec9b0; margin-right: 10px;")
        else:
            self.voice_label.setText("[Mic disabled in GUI mode]")
            self.voice_label.setStyleSheet("color: #888; margin-right: 10px;")

        if self.summon_hotkey_id is None:
            try:
                self.summon_hotkey_id = keyboard.add_hotkey(
                    self.summon_hotkey,
                    lambda: QTimer.singleShot(0, self.toggle_window_visibility),
                )
            except Exception as e:
                print(f"[HOTKEY] Failed to register summon hotkey '{self.summon_hotkey}': {e}")
        
        self.signals.new_token.connect(self.append_token)
        self.signals.finished.connect(self.finalize_response)
        self.signals.voice_hold.connect(self.schedule_voice_release)

        self.perform_startup_checks()
        QTimer.singleShot(100, self.init_live2d_embedding)

    def _bind_events(self):
        self.event_bus.subscribe(Events.AI_SENTENCE_READY, self._on_ai_sentence_ready)
        self.event_bus.subscribe(Events.BARGE_IN, self._on_barge_in_event)

    def _apply_theme(self):
        if self.ui_theme == "light":
            self.setStyleSheet("background-color: #f4f4f4; color: #202020;")
            return
        if self.ui_theme == "forest":
            self.setStyleSheet("background-color: #102018; color: #e6f2ea;")
            return
        self.setStyleSheet("background-color: #1e1e1e; color: white;")

    def init_system_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        self.tray_icon.setToolTip("MARIE Assistant")

        tray_menu = QMenu(self)
        toggle_action = QAction("Show / Hide", self)
        toggle_action.triggered.connect(self.toggle_window_visibility)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)

        tray_menu.addAction(toggle_action)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.toggle_window_visibility()

    def toggle_window_visibility(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        self.face_container = QFrame()
        self.face_container.setFixedSize(450, 600)
        if self.transparent_face:
            self.face_container.setStyleSheet("background-color: rgba(0, 0, 0, 0); border: 2px solid #3e3e42; border-radius: 5px;")
        else:
            self.face_container.setStyleSheet("background-color: #000; border: 2px solid #3e3e42; border-radius: 5px;")
        main_layout.addWidget(self.face_container)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        top_bar = QHBoxLayout()
        self.voice_label = QLabel(f"Mic: OFF ({self.mic_hotkey})")
        self.voice_label.setStyleSheet("color: #888; margin-right: 10px;")
        
        self.mode_label = QLabel(f"SESSION ACTIVE [{self.app_mode.upper()}]")
        self.mode_label.setStyleSheet("color: #4ec9b0; font-weight: bold;")

        self.processing_label = QLabel("Status: Idle")
        self.processing_label.setStyleSheet("color: #888; margin-right: 10px;")

        self.screen_toggle_btn = QPushButton()
        self.screen_toggle_btn.setFixedWidth(105)
        self.screen_toggle_btn.clicked.connect(self.toggle_screen_share)
        self._refresh_screen_toggle_button()
        
        settings_btn = QPushButton("Settings / DB")
        settings_btn.setFixedWidth(120)
        settings_btn.setStyleSheet("background-color: #444; padding: 5px;")
        settings_btn.clicked.connect(self.open_settings)
        
        top_bar.addWidget(self.voice_label) 
        top_bar.addWidget(self.mode_label)
        top_bar.addWidget(self.processing_label)
        top_bar.addWidget(self.screen_toggle_btn)
        top_bar.addStretch()
        top_bar.addWidget(settings_btn)

        self.chat_history = QTextEdit()
        self.chat_history.setReadOnly(True)
        self.chat_history.setStyleSheet("""
            background-color: #252526; 
            font-size: 14px; 
            padding: 10px; 
            border: 1px solid #3e3e42;
            color: #d4d4d4;
        """)
        
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Type your command here...")
        self.input_field.setStyleSheet("background-color: #333; padding: 10px; border-radius: 5px; color: white;")
        self.input_field.returnPressed.connect(self.handle_send)

        self.send_btn = QPushButton("Send")
        self.send_btn.setStyleSheet("background-color: #007acc; padding: 10px; font-weight: bold; border-radius: 5px; color: white;")
        self.send_btn.clicked.connect(self.handle_send)

        right_layout.addLayout(top_bar)
        right_layout.addWidget(self.chat_history)
        right_layout.addWidget(self.input_field)
        right_layout.addWidget(self.send_btn)

        main_layout.addWidget(right_panel, stretch=1)
        
        
    def update_voice_status(self, status):
        color = "#888"
        if "Listening" in status:
            color = "#4ec9b0"
        elif "ON" in status:
            color = "#00ff00"
        elif "Auto-paused" in status:
            color = "#d7ba7d"
        self.voice_label.setStyleSheet(f"color: {color}; margin-right: 10px;")
        self.voice_label.setText(f"[{status}]")

    def _docs_url_from_api(self, api_url):
        parsed = urlparse(api_url)
        return f"{parsed.scheme}://{parsed.netloc}/docs"

    def perform_startup_checks(self):
        warnings = []

        if not os.path.exists(self.model_path):
            warnings.append(f"Live2D model not found at: {self.model_path}")

        try:
            requests.get(self._docs_url_from_api(self.brain_url), timeout=1.2)
        except Exception:
            warnings.append("Reasoning server is unreachable.")

        if self.voice_output_enabled:
            try:
                requests.get(self._docs_url_from_api(self.voice_url), timeout=1.2)
            except Exception:
                warnings.append("Voice server is unreachable.")

        if self.screen_share_enabled and not PYAUTOGUI_AVAILABLE:
            warnings.append("Screen share is enabled but PyAutoGUI is unavailable.")

        if self.screen_share_enabled and not str(CONFIG.get("vision", {}).get("vision_model", "")).strip():
            warnings.append("Screen share is ON without a vision model. Set vision.vision_model for screenshot analysis.")

        if warnings:
            self.chat_history.append("<span style='color:#d7ba7d'><b>System Check:</b></span>")
            for warning in warnings:
                self.chat_history.append(f"<span style='color:#d7ba7d'>- {warning}</span>")
        else:
            self.chat_history.append("<span style='color:#4ec9b0'><b>System Check:</b> all core services look ready.</span>")

    def handle_voice_input(self, text):
        if not text or not self.voice_input_enabled:
            return

        if self.is_processing_response or self.waiting_for_voice_finish:
            self.event_bus.emit(Events.BARGE_IN, {"source": "voice_input"})

        self.input_field.setText(text)
        self.submit_user_text(text)

    def handle_speech_activity(self):
        if self.is_processing_response or self.waiting_for_voice_finish:
            self.event_bus.emit(Events.BARGE_IN, {"source": "speech_activity"})

    def _on_barge_in_event(self, payload=None):
        _ = payload or {}
        self.chat_history.append("<span style='color:#d7ba7d'><i>[System] Barge-in detected. Interrupting current output...</i></span>")
        self.interrupt_current_output()

    def interrupt_current_output(self):
        if self.reasoning_worker and self.reasoning_worker.isRunning():
            self.reasoning_worker.cancel()

        if self.current_request_id:
            try:
                requests.post(self.brain_stop_url, json={"request_id": self.current_request_id}, timeout=2)
            except Exception:
                pass

        try:
            requests.post(self.voice_stop_url, json={}, timeout=2)
        except Exception:
            pass

        self.current_request_id = None
        self.is_processing_response = False
        self.waiting_for_voice_finish = False
        self.voice_release_token += 1
        self.voice_release_deadline = 0.0
        self.stop_mouth()
        self.set_processing_status("Status: Interrupted", "#d7ba7d")

        if self.voice_thread:
            self.voice_thread.resume_after_processing()

    def set_processing_status(self, text, color="#888"):
        self.processing_label.setText(text)
        self.processing_label.setStyleSheet(f"color: {color}; margin-right: 10px;")

    def _refresh_screen_toggle_button(self):
        if not hasattr(self, "screen_toggle_btn"):
            return

        if self.screen_share_enabled:
            self.screen_toggle_btn.setText("Screen: ON")
            self.screen_toggle_btn.setStyleSheet("background-color: #2d8a2d; padding: 5px; color: white;")
        else:
            self.screen_toggle_btn.setText("Screen: OFF")
            self.screen_toggle_btn.setStyleSheet("background-color: #555; padding: 5px; color: white;")

    def toggle_screen_share(self):
        self.screen_share_enabled = not self.screen_share_enabled
        self._refresh_screen_toggle_button()

        if self.screen_share_enabled:
            self.chat_history.append(
                "<span style='color:#4ec9b0'><i>[Screen Share] Enabled. A screenshot will be attached to each prompt.</i></span>"
            )
            if not PYAUTOGUI_AVAILABLE:
                self.chat_history.append(
                    "<span style='color:#f48771'><i>[Screen Share] PyAutoGUI is unavailable, so capture will fail.</i></span>"
                )
        else:
            self.chat_history.append("<span style='color:#888'><i>[Screen Share] Disabled.</i></span>")

    def _collect_screen_payload(self):
        if not self.screen_share_enabled:
            return {}

        capture_data = capture_screen_snapshot()
        capture_error = capture_data.get("error")
        if capture_error:
            if not self.screen_capture_warned:
                safe_err = html.escape(str(capture_error))
                self.chat_history.append(
                    f"<span style='color:#f48771'><i>[Screen Share] {safe_err}</i></span>"
                )
            self.screen_capture_warned = True
            return {}

        self.screen_capture_warned = False

        payload = {}
        image_path = capture_data.get("image_path")
        if image_path:
            payload["screen_image_path"] = image_path

        window_title = capture_data.get("window_title")
        if window_title:
            payload["screen_window_title"] = window_title

        return payload

    def open_settings(self):
        dlg = SettingsWindow(self)
        dlg.exec_()

    def init_live2d_embedding(self):
        if "live2d" not in globals():
            self.chat_history.append("<span style='color:#d7ba7d'><i>[System] Live2D runtime not available.</i></span>")
            return
        if os.name != "nt" or "win32gui" not in globals() or "win32con" not in globals():
            self.chat_history.append("<span style='color:#d7ba7d'><i>[System] Live2D embedding currently supports Windows in this build.</i></span>")
            return

        pygame.init()
        os.environ['SDL_VIDEO_WINDOW_POS'] = "%d,%d" % (-1000, -1000)
        self.screen = pygame.display.set_mode((450, 600), DOUBLEBUF | OPENGL | NOFRAME)
        
        pygame_hwnd = pygame.display.get_wm_info()['window']
        parent_hwnd = int(self.face_container.winId())

        win32gui.SetParent(pygame_hwnd, parent_hwnd)
        win32gui.SetWindowPos(pygame_hwnd, win32con.HWND_TOP, 0, 0, 450, 600, win32con.SWP_SHOWWINDOW)

        live2d.init()
        live2d.glInit()
        self.model = None
        
        if os.path.exists(self.model_path):
            os.chdir(os.path.dirname(self.model_path))
            self.model = live2d.LAppModel()
            self.model.LoadModelJson(self.model_path)
            self.model.Resize(450, 600)
        else:
            print(f"[LIVE2D] Model file not found: {self.model_path}")
            return
            
        self.t_breath = 0.0
        self.last_blink = time.time()
        
        self.anim_timer = QTimer()
        self.anim_timer.timeout.connect(self.update_live2d_frame)
        self.anim_timer.start(16)

    def _char_to_viseme(self, ch):
        ch = ch.lower()
        if ch in "ae":
            return 0.95
        if ch in "iou":
            return 0.85
        if ch in "fvm":
            return 0.55
        if ch in "wq":
            return 0.45
        if ch in "lrn":
            return 0.35
        if ch in "bp":
            return 0.15
        return 0.25

    def _enqueue_viseme_timeline(self, text, duration_ms):
        letters = [c for c in text if c.isalpha()]
        if not letters or duration_ms <= 0:
            return

        start_ts = time.time()
        step = max(0.035, float(duration_ms) / 1000.0 / max(1, len(letters)))

        with self.viseme_lock:
            ts = start_ts
            for letter in letters:
                self.viseme_timeline.append((ts, self._char_to_viseme(letter)))
                ts += step

            # Ensure a gentle closure after the sentence.
            self.viseme_timeline.append((ts + 0.06, 0.0))

    def _next_viseme_value(self):
        now = time.time()
        with self.viseme_lock:
            while self.viseme_timeline and self.viseme_timeline[0][0] <= now:
                _, value = self.viseme_timeline.pop(0)
                self.current_viseme_value = value
                if not self.viseme_timeline or self.viseme_timeline[0][0] > now:
                    return self.current_viseme_value
        return self.current_viseme_value

    def update_live2d_frame(self):
        if not getattr(self, "model", None):
            return

        for event in pygame.event.get(): pass

        self.t_breath += 0.05
        self.model.SetParameterValue("ParamBreath", (math.sin(self.t_breath) + 1) / 2)
        
        mouth_val = self._next_viseme_value() if self.is_speaking_remotely else 0.0
        self.model.SetParameterValue("ParamMouthOpenY", mouth_val)
        
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
            live2d.clearBuffer(0.1, 0.1, 0.1, 1.0)
        self.model.Draw()
        pygame.display.flip()

    def handle_send(self):
        if not self.text_input_enabled:
            return

        text = self.input_field.text().strip()
        self.submit_user_text(text)

    def submit_user_text(self, text):
        if not text:
            return

        if self.is_processing_response or self.waiting_for_voice_finish:
            self.interrupt_current_output()

        self.db.log_chat(self.current_user_id, "user", text, session_id=self.current_session_id)
        self.latest_user_text = text
        self.event_bus.emit(Events.USER_SPOKE, {"text": text})

        self.chat_history.append(f"<b style='color: #4ec9b0'>YOU:</b> {text}")
        self.chat_history.append(f"<b style='color: #ce9178'>MARIE:</b> ")
        self.input_field.clear()

        if _looks_like_high_risk_action_command(text):
            avatar_was_running = bool(
                hasattr(self, "anim_timer") and self.anim_timer is not None and self.anim_timer.isActive()
            )
            if avatar_was_running:
                self.anim_timer.stop()
            try:
                _, action_result = run_action_command_isolated(text)
            finally:
                if avatar_was_running:
                    self.anim_timer.start(16)
        else:
            action_result = self.actions.execute_and_collect(text)
        self.latest_action_result = action_result
        if action_result:
            pretty = html.escape(action_result).replace("\n", "<br>")
            self.chat_history.append(f"<span style='color:#9cdcfe'><i>{pretty}</i></span>")

        self.is_processing_response = True
        self.set_processing_status("Status: Processing...", "#d7ba7d")
        if self.voice_thread:
            self.voice_thread.pause_for_processing()

        self.start_reasoning_stream(text, action_result)

    def start_reasoning_stream(self, text, action_result=""):
        memory_context = self.db.build_memory_context(self.current_user_id, self.current_session_id)
        screen_payload = self._collect_screen_payload()
        payload = {
            "text": text,
            "user_id": self.current_user_id,
            "session_id": self.current_session_id,
            "memory_context": memory_context,
            "action_result": action_result,
        }
        if screen_payload:
            payload.update(screen_payload)

        if self.reasoning_worker and self.reasoning_worker.isRunning():
            self.reasoning_worker.cancel()
            self.reasoning_worker.wait(800)

        self.reasoning_worker = ReasoningStreamWorker(
            stream_url=self.brain_stream_url,
            stop_url=self.brain_stop_url,
            payload=payload,
            timeout_sec=140,
        )
        self.reasoning_worker.stream_started.connect(self._on_stream_started)
        self.reasoning_worker.token_received.connect(self._on_stream_token)
        self.reasoning_worker.sentence_ready.connect(self._on_stream_sentence)
        self.reasoning_worker.completed.connect(self._on_stream_completed)
        self.reasoning_worker.failed.connect(self._on_stream_failed)
        self.reasoning_worker.cancelled.connect(self._on_stream_cancelled)
        self.reasoning_worker.start()

    def _on_stream_started(self, request_id):
        self.current_request_id = request_id

    def _on_stream_token(self, token):
        self.event_bus.emit(Events.AI_TOKEN, {"token": token})
        self.append_token(token)

    def _on_stream_sentence(self, sentence):
        self.event_bus.emit(Events.AI_SENTENCE_READY, {"sentence": sentence})

    def _on_ai_sentence_ready(self, payload):
        sentence = (payload or {}).get("sentence", "")
        if not sentence or not self.voice_output_enabled:
            return
        threading.Thread(target=self._speak_sentence_remote, args=(sentence,), daemon=True).start()

    def _speak_sentence_remote(self, sentence):
        self.is_speaking_remotely = True
        try:
            voice_response = requests.post(
                self.voice_url,
                json={
                    "text": sentence,
                    "character": self.current_character,
                    "async_play": True,
                },
                timeout=35,
            )
            if voice_response.ok:
                voice_data = voice_response.json()
                duration_ms = int(voice_data.get("duration_ms", 0))
                if duration_ms > 0:
                    self._enqueue_viseme_timeline(sentence, duration_ms)
                    self.event_bus.emit(Events.AUDIO_READY, {"duration_ms": duration_ms, "sentence": sentence})
                    self.signals.voice_hold.emit(duration_ms)
        except Exception as e:
            print(f"Voice sentence error: {e}")

    def _on_stream_completed(self, full_text):
        self.current_request_id = None
        self.is_processing_response = False
        self.set_processing_status("Status: Idle", "#888")
        self.signals.finished.emit(full_text)
        self.event_bus.emit(Events.AI_COMPLETED, {"text": full_text})

        if self.voice_thread and not self.waiting_for_voice_finish:
            self.voice_thread.resume_after_processing()

    def _on_stream_failed(self, error_text):
        self.current_request_id = None
        self.is_processing_response = False
        self.set_processing_status("Status: Error", "#f48771")

        fail_msg = f"[System Error: {error_text}]"
        self.append_token(fail_msg)
        self.signals.finished.emit(fail_msg)
        self.event_bus.emit(Events.ERROR, {"error": error_text})
        self._play_error_voice_line()

        if self.voice_thread and not self.waiting_for_voice_finish:
            self.voice_thread.resume_after_processing()

    def _on_stream_cancelled(self):
        self.current_request_id = None
        self.is_processing_response = False
        self.set_processing_status("Status: Interrupted", "#d7ba7d")
        if self.voice_thread and not self.waiting_for_voice_finish:
            self.voice_thread.resume_after_processing()

    def schedule_voice_release(self, duration_ms):
        hold_ms = max(300, min(int(duration_ms) + 180, 20000))
        now = time.time()
        if self.voice_release_deadline < now:
            self.voice_release_deadline = now
        self.voice_release_deadline += hold_ms / 1000.0

        self.waiting_for_voice_finish = True
        self.is_speaking_remotely = True
        self.voice_release_token += 1
        token = self.voice_release_token
        delay_ms = max(350, int((self.voice_release_deadline - now) * 1000) + 120)
        QTimer.singleShot(delay_ms, lambda t=token: self.finish_voice_release(t))

    def finish_voice_release(self, token=None):
        if token is not None and token != self.voice_release_token:
            return
        self.waiting_for_voice_finish = False
        self.voice_release_deadline = 0.0
        self.stop_mouth()
        if self.voice_thread and not self.is_processing_response:
            self.voice_thread.resume_after_processing()

    def stop_mouth(self):
        self.is_speaking_remotely = False
        with self.viseme_lock:
            self.viseme_timeline.clear()
        self.current_viseme_value = 0.0

    def _play_error_voice_line(self):
        if not self.voice_output_enabled:
            return
        try:
            requests.post(
                self.voice_url,
                json={
                    "text": "[concerned] Sorry, I ran into an error. Please try again.",
                    "character": self.current_character,
                    "async_play": True,
                },
                timeout=10,
            )
        except Exception:
            pass

    def append_token(self, token):
        cursor = self.chat_history.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(token)
        self.chat_history.setTextCursor(cursor)

    def finalize_response(self, full_text):
        if not full_text:
            full_text = "[No response]"

        self.db.log_chat(self.current_user_id, "marie", full_text, session_id=self.current_session_id)
        self.db.log_conversation_turn(
            self.current_user_id,
            self.current_session_id,
            self.latest_user_text,
            full_text,
        )

        auto_saved = self.db.auto_store_important_conversation_data(
            self.latest_user_text,
            full_text,
            user_id=self.current_user_id,
        )
        if auto_saved:
            preview_items = [f"{k}={v}" for k, v in auto_saved[:3]]
            preview = "; ".join(preview_items)
            self.chat_history.append(f"<span style='color:#b5cea8'><i>[RAD auto-saved] {preview}</i></span>")
        self.latest_user_text = ""
        self.latest_action_result = ""
        
        self.chat_history.append("<hr style='background-color: #444; height: 1px; border: 0;'>")
        threading.Thread(target=self._execute_assistant_actions_background, args=(full_text,), daemon=True).start()

    def _execute_assistant_actions_background(self, full_text):
        if _looks_like_high_risk_action_command(full_text):
            ok, output = run_action_command_isolated(full_text, timeout_sec=35)
            if output:
                prefix = "[ACTION][ASSISTANT]" if ok else "[ACTION][ASSISTANT][ERROR]"
                print(f"{prefix} {output}")
            return

        try:
            self.actions.execute_from_assistant(full_text)
        except Exception as exc:
            print(f"[ACTION][ASSISTANT][ERROR] {exc}")

    def closeEvent(self, event):
        self.db.logout_user(self.current_user_id, self.current_session_id)

        if self.reasoning_worker and self.reasoning_worker.isRunning():
            self.reasoning_worker.cancel()
            self.reasoning_worker.wait(1200)

        if self.voice_thread:
            self.voice_thread.running = False
            self.voice_thread.wait(1000)

        if self.hotkey_id is not None:
            try:
                keyboard.remove_hotkey(self.hotkey_id)
            except Exception:
                pass

        if self.summon_hotkey_id is not None:
            try:
                keyboard.remove_hotkey(self.summon_hotkey_id)
            except Exception:
                pass

        if self.tray_icon:
            self.tray_icon.hide()

        try:
            live2d.dispose()
        except Exception:
            pass

        pygame.quit()
        event.accept()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MARIE Launcher")
    parser.add_argument("--mode", choices=["both", "gui", "voice"], default="both", help="Input/output mode")
    parser.add_argument("--force-login", action="store_true", help="Ignore saved one-time login token")
    parser.add_argument("--reset-login", action="store_true", help="Clear one-time login token and exit")
    parser.add_argument("--transparent-face", action="store_true", help="Use transparent background behind Live2D canvas")
    args = parser.parse_args()

    if args.reset_login:
        clear_auto_login_user()
        print("[LOGIN] One-time login token cleared.")
        sys.exit(0)

    app = QApplication(sys.argv)
    
    db = MarieDB()

    if not args.force_login:
        remembered_user_id = load_auto_login_user()
        if remembered_user_id and db.user_exists(remembered_user_id):
            resumed_session_id = db.resume_user_session(remembered_user_id)
            if resumed_session_id:
                window = MainWindow(
                    remembered_user_id,
                    resumed_session_id,
                    db,
                    app_mode=args.mode,
                    transparent_face=args.transparent_face,
                )
                window.show()
                sys.exit(app.exec_())
        elif remembered_user_id:
            clear_auto_login_user()
    
    login = LoginDialog(db)
    if login.exec_() == QDialog.Accepted:
        window = MainWindow(
            login.user_id,
            login.session_id,
            db,
            app_mode=args.mode,
            transparent_face=args.transparent_face,
        )
        window.show()
        sys.exit(app.exec_())
    else:       
        sys.exit()