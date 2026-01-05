import sys
import os
import threading
import pygame
import math
import time
import random
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QTextEdit, QLineEdit, QPushButton, QLabel, QFrame)
from PyQt5.QtCore import pyqtSignal, Qt, QObject, QTimer
from PyQt5.QtGui import QWindow

# --- LIVE2D IMPORT ---
try:
    from live2d import v3 as live2d
    from pygame.locals import *
except ImportError:
    print("[CRITICAL] 'live2d' or 'pygame' library not found.")

# --- WINDOW EMBEDDING LIBS ---
try:
    import win32gui
    import win32con
except ImportError:
    print("[CRITICAL] pywin32 not found. Run: pip install pywin32")

# --- SAFE PATH SETUP ---
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(ROOT_DIR)

from action import ActionHandler
from index import get_marie_response_stream
from voice import MarieVoice

class StreamSignals(QObject):
    new_token = pyqtSignal(str)
    finished = pyqtSignal(str)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MARIE - Intelligent Environment")
        self.resize(1100, 700)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")

        # Engines
        self.voice = MarieVoice()
        self.actions = ActionHandler()
        self.signals = StreamSignals()
        
        # Live2D Config
        self.model_path = r"d:\pylearn\FYP\AiAssistant\models\kei\runtime\kei_vowels_pro.model3.json"
        
        # UI Setup
        self.init_ui()
        
        # Connect Signals
        self.signals.new_token.connect(self.append_token)
        self.signals.finished.connect(self.finalize_response)

        # Initialize Live2D (After UI is shown, to embed correctly)
        QTimer.singleShot(100, self.init_live2d_embedding)

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # --- LEFT PANEL (Face Container) ---
        self.face_container = QFrame()
        self.face_container.setFixedSize(450, 600)
        self.face_container.setStyleSheet("background-color: #000; border: 2px solid #3e3e42; border-radius: 5px;")
        
        # We need the window handle (HWND) of this widget to embed Pygame inside it
        main_layout.addWidget(self.face_container)

        # --- RIGHT PANEL (Chat) ---
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

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
        self.input_field.setStyleSheet("""
            background-color: #333; 
            padding: 10px; 
            border-radius: 5px; 
            border: 1px solid #3e3e42; 
            color: white;
        """)
        self.input_field.returnPressed.connect(self.handle_send)

        self.send_btn = QPushButton("Send")
        self.send_btn.setStyleSheet("""
            background-color: #007acc; 
            padding: 10px; 
            font-weight: bold; 
            border-radius: 5px;
            color: white;
        """)
        self.send_btn.clicked.connect(self.handle_send)

        right_layout.addWidget(self.chat_history)
        right_layout.addWidget(self.input_field)
        right_layout.addWidget(self.send_btn)

        main_layout.addWidget(right_panel, stretch=1)

    def init_live2d_embedding(self):
        """Initializes Pygame and embeds its window inside the PyQt Frame."""
        # 1. Init Pygame (NOFRAME removes the title bar)
        pygame.init()
        os.environ['SDL_VIDEO_WINDOW_POS'] = "%d,%d" % (-1000, -1000) # Start off-screen momentarily
        self.screen = pygame.display.set_mode((450, 600), DOUBLEBUF | OPENGL | NOFRAME)
        
        # 2. Get Window Handles
        pygame_hwnd = pygame.display.get_wm_info()['window']
        parent_hwnd = int(self.face_container.winId())

        # 3. Embed Pygame into PyQt Widget using Win32 API
        win32gui.SetParent(pygame_hwnd, parent_hwnd)
        
        # 4. Adjust Position (0,0 relative to the parent widget)
        win32gui.SetWindowPos(pygame_hwnd, win32con.HWND_TOP, 0, 0, 450, 600, win32con.SWP_SHOWWINDOW)

        # 5. Load Model
        live2d.init()
        live2d.glInit() # Important for OpenGL context
        
        if os.path.exists(self.model_path):
            os.chdir(os.path.dirname(self.model_path))
            self.model = live2d.LAppModel()
            self.model.LoadModelJson(self.model_path)
            self.model.Resize(450, 600)
            
        self.t_breath = 0.0
        self.last_blink = time.time()
        
        # 6. Start Rendering Loop via Timer (60 FPS)
        self.anim_timer = QTimer()
        self.anim_timer.timeout.connect(self.update_live2d_frame)
        self.anim_timer.start(16)

    def update_live2d_frame(self):
        # Handle Pygame Events (Required to keep window responsive)
        for event in pygame.event.get():
            pass # Just flush events

        # Animation Physics
        self.t_breath += 0.05
        self.model.SetParameterValue("ParamBreath", (math.sin(self.t_breath) + 1) / 2)
        
        # Sync Mouth with Voice
        mouth_val = random.uniform(0.3, 1.0) if self.voice.is_speaking else 0.0
        self.model.SetParameterValue("ParamMouthOpenY", mouth_val)

        # Blinking
        if time.time() > self.last_blink + 3:
            self.model.SetParameterValue("ParamEyeLOpen", 0.0)
            self.model.SetParameterValue("ParamEyeROpen", 0.0)
            if time.time() > self.last_blink + 3.2:
                self.last_blink = time.time()
        else:
            self.model.SetParameterValue("ParamEyeLOpen", 1.0)
            self.model.SetParameterValue("ParamEyeROpen", 1.0)

        # Draw
        self.model.Update()
        live2d.clearBuffer(0.1, 0.1, 0.1, 1.0) # Grey background matches GUI
        self.model.Draw()
        pygame.display.flip()

    def handle_send(self):
        text = self.input_field.text().strip()
        if not text: return

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
        self.chat_history.append("<hr style='background-color: #444; height: 1px; border: 0;'>")
        self.actions.execute(full_text)

    # Ensure clean exit
    def closeEvent(self, event):
        live2d.dispose()
        pygame.quit()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())