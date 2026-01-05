import sys
import os
import threading
import pygame
import math
import time
import random
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QTextEdit, QLineEdit, QPushButton, QLabel, QFrame,
                             QDialog, QTabWidget, QFormLayout, QComboBox, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QMessageBox, QFileDialog)
from PyQt5.QtCore import pyqtSignal, Qt, QObject, QTimer

# --- LIVE2D & WINDOW LIBS ---
try:
    from live2d import v3 as live2d
    from pygame.locals import *
    import win32gui
    import win32con
except ImportError:
    print("[CRITICAL] Libraries missing. Ensure live2d, pygame, and pywin32 are installed.")

# --- PROJECT IMPORTS ---
# Ensure these files exist in the same folder
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(ROOT_DIR)

from action import ActionHandler
from index import get_marie_response_stream
from voice import MarieVoice
from database import MarieDB  # <--- NEW IMPORT
from voice_db import CHARACTERS # To populate dropdowns

# =========================================================================
# 1. LOGIN DIALOG (The "Gatekeeper")
# =========================================================================
class LoginDialog(QDialog):
    def __init__(self, db_instance):
        super().__init__()
        self.db = db_instance
        self.setWindowTitle("MARIE - User Login")
        self.setFixedSize(300, 200)
        self.user_id = None
        self.setStyleSheet("background-color: #252526; color: white;")

        layout = QVBoxLayout(self)
        
        # Inputs
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Username")
        self.user_input.setStyleSheet("padding: 5px; border: 1px solid #555;")
        
        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Password")
        self.pass_input.setEchoMode(QLineEdit.Password)
        self.pass_input.setStyleSheet("padding: 5px; border: 1px solid #555;")
        
        # Buttons
        self.login_btn = QPushButton("Login")
        self.login_btn.clicked.connect(self.handle_login)
        self.login_btn.setStyleSheet("background-color: #007acc; padding: 5px;")
        
        self.reg_btn = QPushButton("Register New Account")
        self.reg_btn.clicked.connect(self.handle_register)
        self.reg_btn.setStyleSheet("background-color: #333; padding: 5px;")

        layout.addWidget(QLabel("<h2>Welcome back</h2>"))
        layout.addWidget(self.user_input)
        layout.addWidget(self.pass_input)
        layout.addWidget(self.login_btn)
        layout.addWidget(self.reg_btn)

    def handle_login(self):
        username = self.user_input.text()
        password = self.pass_input.text()
        uid = self.db.login_user(username, password)
        
        if uid:
            self.user_id = uid
            self.accept() # Closes dialog and lets main window open
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

# =========================================================================
# 2. SETTINGS DASHBOARD (Tabs for Requirements 2-5)
# =========================================================================
class SettingsWindow(QDialog):
    def __init__(self, parent_window):
        super().__init__(parent_window)
        self.main_win = parent_window
        self.db = parent_window.db
        self.uid = parent_window.current_user_id
        
        self.setWindowTitle("Control Panel")
        self.resize(600, 400)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")
        
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("QTabBar::tab { background: #333; color: #888; padding: 8px; } QTabBar::tab:selected { background: #007acc; color: white; }")
        
        # --- TAB 1: PREFERENCES (Voice/Model) ---
        self.tab_prefs = QWidget()
        self.init_prefs_tab()
        self.tabs.addTab(self.tab_prefs, "Preferences")

        # --- TAB 2: CHAT LOGS ---
        self.tab_logs = QWidget()
        self.init_logs_tab()
        self.tabs.addTab(self.tab_logs, "Chat Logs")

        # --- TAB 3: RAD / MEMORY ---
        self.tab_rad = QWidget()
        self.init_rad_tab()
        self.tabs.addTab(self.tab_rad, "RAD Memory")

        layout.addWidget(self.tabs)

    def init_prefs_tab(self):
        layout = QFormLayout(self.tab_prefs)
        
        # Voice Selection
        self.voice_combo = QComboBox()
        self.voice_combo.addItems(list(CHARACTERS.keys()))
        self.voice_combo.setStyleSheet("background: #333; padding: 5px;")
        
        # Live2D Model Path
        self.model_path_input = QLineEdit()
        self.model_path_input.setText(self.main_win.model_path)
        self.model_path_input.setStyleSheet("background: #333; padding: 5px;")
        
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.browse_model)
        browse_btn.setStyleSheet("background: #444; padding: 5px;")
        
        save_btn = QPushButton("Save & Apply")
        save_btn.setStyleSheet("background: #007acc; padding: 8px; font-weight: bold;")
        save_btn.clicked.connect(self.save_preferences)

        layout.addRow("Voice Persona:", self.voice_combo)
        layout.addRow("Model Path:", self.model_path_input)
        layout.addRow("", browse_btn)
        layout.addRow("", save_btn)
        
        # Load current db values
        prefs = self.db.get_preference(self.uid)
        if prefs:
            voice_id, model_p = prefs
            if voice_id: self.voice_combo.setCurrentText(voice_id)
            if model_p: self.model_path_input.setText(model_p)

    def init_logs_tab(self):
        layout = QVBoxLayout(self.tab_logs)
        
        self.log_table = QTableWidget()
        self.log_table.setColumnCount(4)
        self.log_table.setHorizontalHeaderLabels(["Time", "Sender", "Message", "Emotion"])
        self.log_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.log_table.setStyleSheet("QHeaderView::section { background-color: #333; }")
        
        refresh_btn = QPushButton("Refresh Logs")
        refresh_btn.clicked.connect(self.load_logs)
        
        layout.addWidget(self.log_table)
        layout.addWidget(refresh_btn)
        self.load_logs()

    def init_rad_tab(self):
        layout = QVBoxLayout(self.tab_rad)
        
        # Form to add new data
        form_layout = QHBoxLayout()
        self.rad_key = QLineEdit()
        self.rad_key.setPlaceholderText("Key (e.g., 'birthday')")
        self.rad_val = QLineEdit()
        self.rad_val.setPlaceholderText("Value (e.g., 'Jan 6')")
        add_btn = QPushButton("Add Fact")
        add_btn.clicked.connect(self.add_rad_fact)
        
        form_layout.addWidget(self.rad_key)
        form_layout.addWidget(self.rad_val)
        form_layout.addWidget(add_btn)
        
        # Table to view data
        self.rad_table = QTableWidget()
        self.rad_table.setColumnCount(3)
        self.rad_table.setHorizontalHeaderLabels(["Category", "Key", "Value"])
        self.rad_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        
        layout.addLayout(form_layout)
        layout.addWidget(self.rad_table)
        self.load_rad_data()

    # --- ACTIONS ---
    def browse_model(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Select Model3 JSON", "", "Live2D Model (*.model3.json)")
        if fname:
            self.model_path_input.setText(fname)

    def save_preferences(self):
        voice = self.voice_combo.currentText()
        model = self.model_path_input.text()
        
        # Save to DB
        self.db.save_preference(self.uid, voice, model)
        
        # Apply to Running App
        self.main_win.voice.set_voice(voice)
        # Note: Changing Live2D model at runtime is complex; simpler to restart, 
        # but we update the variable so next restart picks it up.
        self.main_win.model_path = model 
        
        QMessageBox.information(self, "Saved", "Preferences saved. Restart to apply full visual changes.")

    def load_logs(self):
        self.db.cursor.execute("SELECT timestamp, message_type, content, emotion_tag FROM chat_logs WHERE user_id=? ORDER BY id DESC", (self.uid,))
        rows = self.db.cursor.fetchall()
        self.log_table.setRowCount(0)
        for row_idx, row_data in enumerate(rows):
            self.log_table.insertRow(row_idx)
            for col_idx, data in enumerate(row_data):
                self.log_table.setItem(row_idx, col_idx, QTableWidgetItem(str(data)))

    def add_rad_fact(self):
        key = self.rad_key.text()
        val = self.rad_val.text()
        if key and val:
            self.db.add_rad_data("user_fact", key, val)
            self.rad_key.clear()
            self.rad_val.clear()
            self.load_rad_data()

    def load_rad_data(self):
        self.db.cursor.execute("SELECT category, key_data, value_data FROM rad_memory")
        rows = self.db.cursor.fetchall()
        self.rad_table.setRowCount(0)
        for row_idx, row_data in enumerate(rows):
            self.rad_table.insertRow(row_idx)
            for col_idx, data in enumerate(row_data):
                self.rad_table.setItem(row_idx, col_idx, QTableWidgetItem(str(data)))

# =========================================================================
# 3. MAIN WINDOW (Updated)
# =========================================================================
class StreamSignals(QObject):
    new_token = pyqtSignal(str)
    finished = pyqtSignal(str)

class MainWindow(QMainWindow):
    def __init__(self, user_id, db_instance):
        super().__init__()
        self.current_user_id = user_id
        self.db = db_instance
        
        self.setWindowTitle("MARIE - Intelligent Environment")
        self.resize(1100, 700)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")

        # Engines
        self.voice = MarieVoice()
        self.actions = ActionHandler()
        self.signals = StreamSignals()
        
        # Default Model or Load from DB
        self.model_path = r"d:\pylearn\FYP\AiAssistant\models\kei\runtime\kei_vowels_pro.model3.json"
        prefs = self.db.get_preference(self.current_user_id)
        if prefs:
            saved_voice, saved_model = prefs
            if saved_voice: self.voice.set_voice(saved_voice)
            if saved_model: self.model_path = saved_model

        self.init_ui()
        
        # Connect Signals
        self.signals.new_token.connect(self.append_token)
        self.signals.finished.connect(self.finalize_response)

        QTimer.singleShot(100, self.init_live2d_embedding)

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # --- LEFT PANEL (Face) ---
        self.face_container = QFrame()
        self.face_container.setFixedSize(450, 600)
        self.face_container.setStyleSheet("background-color: #000; border: 2px solid #3e3e42; border-radius: 5px;")
        main_layout.addWidget(self.face_container)

        # --- RIGHT PANEL (Chat + Controls) ---
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        # TOP BAR (Settings Button)
        top_bar = QHBoxLayout()
        title_label = QLabel("SESSION ACTIVE")
        title_label.setStyleSheet("color: #4ec9b0; font-weight: bold;")
        
        settings_btn = QPushButton("⚙ Settings / DB")
        settings_btn.setFixedWidth(120)
        settings_btn.setStyleSheet("background-color: #444; padding: 5px;")
        settings_btn.clicked.connect(self.open_settings)
        
        top_bar.addWidget(title_label)
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

        send_btn = QPushButton("Send")
        send_btn.setStyleSheet("background-color: #007acc; padding: 10px; font-weight: bold; border-radius: 5px; color: white;")
        send_btn.clicked.connect(self.handle_send)

        right_layout.addLayout(top_bar)
        right_layout.addWidget(self.chat_history)
        right_layout.addWidget(self.input_field)
        right_layout.addWidget(send_btn)

        main_layout.addWidget(right_panel, stretch=1)

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
        
        if os.path.exists(self.model_path):
            os.chdir(os.path.dirname(self.model_path))
            self.model = live2d.LAppModel()
            self.model.LoadModelJson(self.model_path)
            self.model.Resize(450, 600)
            
        self.t_breath = 0.0
        self.last_blink = time.time()
        
        self.anim_timer = QTimer()
        self.anim_timer.timeout.connect(self.update_live2d_frame)
        self.anim_timer.start(16)

    def update_live2d_frame(self):
        for event in pygame.event.get(): pass

        self.t_breath += 0.05
        self.model.SetParameterValue("ParamBreath", (math.sin(self.t_breath) + 1) / 2)
        
        mouth_val = random.uniform(0.3, 1.0) if self.voice.is_speaking else 0.0
        self.model.SetParameterValue("ParamMouthOpenY", mouth_val)

        if time.time() > self.last_blink + 3:
            self.model.SetParameterValue("ParamEyeLOpen", 0.0)
            self.model.SetParameterValue("ParamEyeROpen", 0.0)
            if time.time() > self.last_blink + 3.2:
                self.last_blink = time.time()
        else:
            self.model.SetParameterValue("ParamEyeLOpen", 1.0)
            self.model.SetParameterValue("ParamEyeROpen", 1.0)

        self.model.Update()
        live2d.clearBuffer(0.1, 0.1, 0.1, 1.0)
        self.model.Draw()
        pygame.display.flip()

    def handle_send(self):
        text = self.input_field.text().strip()
        if not text: return

        # LOG TO DB
        self.db.log_chat(self.current_user_id, "user", text)

        self.chat_history.append(f"<b style='color: #4ec9b0'>YOU:</b> {text}")
        self.chat_history.append(f"<b style='color: #ce9178'>MARIE:</b> ")
        self.input_field.clear()
        
        threading.Thread(target=self.process_logic, args=(text,), daemon=True).start()

    def process_logic(self, text):
        full_response = ""
        sentence_buffer = ""
        for token in get_marie_response_stream(text):
            full_response += token
            sentence_buffer += token
            self.signals.new_token.emit(token)
            
            if any(punc in token for punc in [".", "!", "?", "\n"]):
                clean = sentence_buffer.strip()
                if clean:
                    self.voice.speak(clean)
                    sentence_buffer = ""

        if sentence_buffer.strip():
            self.voice.speak(sentence_buffer.strip())
        self.signals.finished.emit(full_response)

    def append_token(self, token):
        cursor = self.chat_history.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(token)
        self.chat_history.setTextCursor(cursor)

    def finalize_response(self, full_text):
        # LOG TO DB
        self.db.log_chat(self.current_user_id, "marie", full_text)
        
        self.chat_history.append("<hr style='background-color: #444; height: 1px; border: 0;'>")
        self.actions.execute(full_text)

    def closeEvent(self, event):
        self.db.logout_user(self.current_user_id)
        live2d.dispose()
        pygame.quit()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # 1. Init DB
    db = MarieDB()
    
    # 2. Show Login
    login = LoginDialog(db)
    if login.exec_() == QDialog.Accepted:
        # 3. If Login Success, Start Main Window
        window = MainWindow(login.user_id, db)
        window.show()
        sys.exit(app.exec_())
    else:
        sys.exit()