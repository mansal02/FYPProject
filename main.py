import argparse
import html
import json
import sys
import requests  
import os
import threading
import pygame
import math
import time
import random
import keyboard
from hear import VoiceWorker
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QTextEdit, QLineEdit, QPushButton, QLabel, QFrame,
                             QDialog, QTabWidget, QFormLayout, QComboBox, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QMessageBox, QFileDialog, QCheckBox)
from PyQt5.QtCore import pyqtSignal, Qt, QObject, QTimer

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

AUTO_LOGIN_FILE = os.path.join(ROOT_DIR, ".marie_autologin.json")


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

from action import ActionHandler
from database import MarieDB
from voice_db import CHARACTERS 

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
        self.transparent_face = transparent_face
        self.voice_input_enabled = app_mode in ("both", "voice")
        self.voice_output_enabled = app_mode in ("both", "voice")
        self.text_input_enabled = app_mode in ("both", "gui")
        
        self.setWindowTitle("MARIE - Intelligent Environment")
        self.resize(1100, 700)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")

        self.brain_url = "http://127.0.0.1:8000/chat"
        self.voice_url = "http://127.0.0.1:8001/speak"
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
        self.latest_user_text = ""
        self.latest_action_result = ""
        self.voice_thread = None
        self.hotkey_id = None
        
        self.model_path = r"d:\pylearn\FYP\AiAssistant\models\kei\runtime\kei_vowels_pro.model3.json"
        self.current_character = "tachyon" 

        prefs = self.db.get_preference(self.current_user_id)
        if prefs:
            saved_voice, saved_model = prefs
            if saved_voice: self.current_character = saved_voice
            if saved_model: self.model_path = saved_model

        self.init_ui()

        if self.text_input_enabled:
            self.input_field.setEnabled(True)
            self.send_btn.setEnabled(True)
        else:
            self.input_field.setEnabled(False)
            self.send_btn.setEnabled(False)
            self.input_field.setPlaceholderText("Voice-only mode is active.")
        
        # VOICE INTEGRATION
        if self.voice_input_enabled:
            self.voice_thread = VoiceWorker(wake_word="hey")
            self.voice_thread.text_received.connect(self.handle_voice_input)
            self.voice_thread.status_update.connect(self.update_voice_status)
            self.voice_thread.start()
            self.hotkey_id = keyboard.add_hotkey('F4', self.voice_thread.toggle_listening)

            if self.app_mode == "voice":
                self.voice_thread.is_active = True
                self.voice_label.setText("[Mic ON]")
                self.voice_label.setStyleSheet("color: #00ff00; margin-right: 10px;")
        else:
            self.voice_label.setText("[Mic disabled in GUI mode]")
            self.voice_label.setStyleSheet("color: #888; margin-right: 10px;")
        
        self.signals.new_token.connect(self.append_token)
        self.signals.finished.connect(self.finalize_response)
        self.signals.voice_hold.connect(self.schedule_voice_release)

        self.perform_startup_checks()
        QTimer.singleShot(100, self.init_live2d_embedding)

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
        self.voice_label = QLabel("Mic: OFF (F4)")
        self.voice_label.setStyleSheet("color: #888; margin-right: 10px;")
        
        self.mode_label = QLabel(f"SESSION ACTIVE [{self.app_mode.upper()}]")
        self.mode_label.setStyleSheet("color: #4ec9b0; font-weight: bold;")
        
        settings_btn = QPushButton("Settings / DB")
        settings_btn.setFixedWidth(120)
        settings_btn.setStyleSheet("background-color: #444; padding: 5px;")
        settings_btn.clicked.connect(self.open_settings)
        
        top_bar.addWidget(self.voice_label) 
        top_bar.addWidget(self.mode_label)
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

    def perform_startup_checks(self):
        warnings = []

        if not os.path.exists(self.model_path):
            warnings.append(f"Live2D model not found at: {self.model_path}")

        try:
            requests.get("http://127.0.0.1:8000/docs", timeout=1.2)
        except Exception:
            warnings.append("Reasoning server (8000) is unreachable.")

        if self.voice_output_enabled:
            try:
                requests.get("http://127.0.0.1:8001/docs", timeout=1.2)
            except Exception:
                warnings.append("Voice server (8001) is unreachable.")

        if warnings:
            self.chat_history.append("<span style='color:#d7ba7d'><b>System Check:</b></span>")
            for warning in warnings:
                self.chat_history.append(f"<span style='color:#d7ba7d'>- {warning}</span>")
        else:
            self.chat_history.append("<span style='color:#4ec9b0'><b>System Check:</b> all core services look ready.</span>")

    def handle_voice_input(self, text):
        if not text or not self.voice_input_enabled:
            return

        if self.is_processing_response:
            return

        self.input_field.setText(text)
        self.submit_user_text(text)

    def open_settings(self):
        dlg = SettingsWindow(self)
        dlg.exec_()

    def init_live2d_embedding(self):
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

    def update_live2d_frame(self):
        if not getattr(self, "model", None):
            return

        for event in pygame.event.get(): pass

        self.t_breath += 0.05
        self.model.SetParameterValue("ParamBreath", (math.sin(self.t_breath) + 1) / 2)
        
        mouth_val = random.uniform(0.3, 1.0) if self.is_speaking_remotely else 0.0
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

        if self.is_processing_response:
            self.chat_history.append("<span style='color:#d7ba7d'><i>[System] Processing previous request...</i></span>")
            return

        self.db.log_chat(self.current_user_id, "user", text, session_id=self.current_session_id)
        self.latest_user_text = text

        self.chat_history.append(f"<b style='color: #4ec9b0'>YOU:</b> {text}")
        self.chat_history.append(f"<b style='color: #ce9178'>MARIE:</b> ")
        self.input_field.clear()

        action_result = self.actions.execute_and_collect(text)
        self.latest_action_result = action_result
        if action_result:
            pretty = html.escape(action_result).replace("\n", "<br>")
            self.chat_history.append(f"<span style='color:#9cdcfe'><i>{pretty}</i></span>")

        self.is_processing_response = True
        if self.voice_thread:
            self.voice_thread.pause_for_processing()

        threading.Thread(target=self.process_logic, args=(text, action_result), daemon=True).start()

    def process_logic(self, text, action_result=""):
        ai_reply = ""
        voice_hold_ms = 0
        try:
            memory_context = self.db.build_memory_context(self.current_user_id, self.current_session_id)

            # 1. SEND TO BRAIN (Port 8000)
            payload = {
                "text": text,
                "user_id": self.current_user_id,
                "session_id": self.current_session_id,
                "memory_context": memory_context,
                "action_result": action_result,
            }
            
            response = requests.post(self.brain_url, json=payload, timeout=120)
            response.raise_for_status()
            data = response.json()
            ai_reply = data.get("response", "[Error: Brain Empty]")
            
            self.signals.new_token.emit(ai_reply)
            self.signals.finished.emit(ai_reply)

            # 2. SEND TO VOICE (Port 8001)
            if self.voice_output_enabled and ai_reply:
                self.is_speaking_remotely = True
                try:
                    voice_response = requests.post(
                        self.voice_url,
                        json={
                            "text": ai_reply,
                            "character": self.current_character,
                            "async_play": True,
                        },
                        timeout=120,
                    )
                    if voice_response.ok:
                        voice_data = voice_response.json()
                        voice_hold_ms = int(voice_data.get("duration_ms", 0))
                        if voice_hold_ms > 0:
                            self.waiting_for_voice_finish = True
                            self.signals.voice_hold.emit(voice_hold_ms)
                except Exception as e:
                    print(f"Voice Error: {e}")
                finally:
                    if voice_hold_ms <= 0:
                        self.stop_mouth()
            
        except Exception as e:
            print(f"Connection Error: {e}")
            fail_msg = "[System Error: Brain server is offline]"
            self.signals.new_token.emit(fail_msg)
            self.signals.finished.emit(fail_msg)
        finally:
            self.is_processing_response = False
            if self.voice_thread and not self.waiting_for_voice_finish:
                self.voice_thread.resume_after_processing()

    def schedule_voice_release(self, duration_ms):
        hold_ms = max(800, min(int(duration_ms) + 250, 180000))
        self.waiting_for_voice_finish = True
        self.is_speaking_remotely = True
        QTimer.singleShot(hold_ms, self.finish_voice_release)

    def finish_voice_release(self):
        self.waiting_for_voice_finish = False
        self.stop_mouth()
        if self.voice_thread and not self.is_processing_response:
            self.voice_thread.resume_after_processing()

    def stop_mouth(self):
        self.is_speaking_remotely = False

    def append_token(self, token):
        cursor = self.chat_history.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(token)
        self.chat_history.setTextCursor(cursor)

    def finalize_response(self, full_text):
        self.db.log_chat(self.current_user_id, "marie", full_text, session_id=self.current_session_id)
        self.db.log_conversation_turn(
            self.current_user_id,
            self.current_session_id,
            self.latest_user_text,
            full_text,
        )

        auto_saved = self.db.auto_store_important_conversation_data(self.latest_user_text, full_text)
        if auto_saved:
            preview_items = [f"{k}={v}" for k, v in auto_saved[:3]]
            preview = "; ".join(preview_items)
            self.chat_history.append(f"<span style='color:#b5cea8'><i>[RAD auto-saved] {preview}</i></span>")
        self.latest_user_text = ""
        self.latest_action_result = ""
        
        self.chat_history.append("<hr style='background-color: #444; height: 1px; border: 0;'>")
        threading.Thread(target=self.actions.execute, args=(full_text,), daemon=True).start()

    def closeEvent(self, event):
        self.db.logout_user(self.current_user_id, self.current_session_id)

        if self.voice_thread:
            self.voice_thread.running = False
            self.voice_thread.wait(1000)

        if self.hotkey_id is not None:
            try:
                keyboard.remove_hotkey(self.hotkey_id)
            except Exception:
                pass

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