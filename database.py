import sqlite3
import hashlib
from datetime import datetime

DB_NAME = "marie_data.db"

class MarieDB:
    def __init__(self):
        self.conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.create_tables()

    def create_tables(self):
        """Initialize the 5 required structures (Tables)"""
        
        # 1. LOGIN/LOGOUT (User Accounts & Session Tracking)
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                logout_time TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')

        # 2. CHATLOG REPORT
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_type TEXT, -- 'user' or 'marie'
                content TEXT,
                emotion_tag TEXT, -- e.g. [happy]
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')

        # 3 & 4. CONFIGURATION (Voice & Character Settings)
        # We store the *paths* and *preferences*, not the files themselves.
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                preferred_voice_id TEXT DEFAULT 'tachyon',
                preferred_model_path TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')

        # 5. RAD / MEMORY (For future data analysis or RAG context)
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS rad_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT, -- e.g., 'fact', 'preference', 'task'
                key_data TEXT,
                value_data TEXT,
                confidence_score REAL DEFAULT 1.0
            )
        ''')
        self.conn.commit()

    # --- AUTHENTICATION METHODS ---
    def register_user(self, username, password):
        try:
            # Simple hashing for security
            pwd_hash = hashlib.sha256(password.encode()).hexdigest()
            self.cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", 
                                (username, pwd_hash))
            self.conn.commit()
            return True, "User registered successfully."
        except sqlite3.IntegrityError:
            return False, "Username already exists."

    def login_user(self, username, password):
        pwd_hash = hashlib.sha256(password.encode()).hexdigest()
        self.cursor.execute("SELECT id FROM users WHERE username=? AND password_hash=?", 
                            (username, pwd_hash))
        user = self.cursor.fetchone()
        
        if user:
            user_id = user[0]
            # Create a session entry
            self.cursor.execute("INSERT INTO sessions (user_id) VALUES (?)", (user_id,))
            self.conn.commit()
            return user_id
        return None

    def logout_user(self, user_id):
        # Update the logout time for the most recent session
        self.cursor.execute('''
            UPDATE sessions SET logout_time = CURRENT_TIMESTAMP 
            WHERE user_id = ? AND logout_time IS NULL
        ''', (user_id,))
        self.conn.commit()

    # --- LOGGING METHODS ---
    def log_chat(self, user_id, sender, text, emotion="neutral"):
        self.cursor.execute("INSERT INTO chat_logs (user_id, message_type, content, emotion_tag) VALUES (?, ?, ?, ?)",
                            (user_id, sender, text, emotion))
        self.conn.commit()

    # --- SETTINGS METHODS ---
    def save_preference(self, user_id, voice_id=None, model_path=None):
        # Check if settings exist, if not create them
        self.cursor.execute("SELECT user_id FROM user_settings WHERE user_id=?", (user_id,))
        if not self.cursor.fetchone():
            self.cursor.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
        
        if voice_id:
            self.cursor.execute("UPDATE user_settings SET preferred_voice_id=? WHERE user_id=?", (voice_id, user_id))
        if model_path:
            self.cursor.execute("UPDATE user_settings SET preferred_model_path=? WHERE user_id=?", (model_path, user_id))
        self.conn.commit()

    def get_preference(self, user_id):
        self.cursor.execute("SELECT preferred_voice_id, preferred_model_path FROM user_settings WHERE user_id=?", (user_id,))
        return self.cursor.fetchone()

    # --- RAD / DATA METHODS ---
    def add_rad_data(self, category, key, value):
        self.cursor.execute("INSERT INTO rad_memory (category, key_data, value_data) VALUES (?, ?, ?)",
                            (category, key, value))
        self.conn.commit()
    
    # --- DELETE METHODS (Add these to MarieDB class) ---
    def delete_chat_log(self, log_id):
        self.cursor.execute("DELETE FROM chat_logs WHERE id=?", (log_id,))
        self.conn.commit()

    def delete_rad_data(self, rad_id):
        self.cursor.execute("DELETE FROM rad_memory WHERE id=?", (rad_id,))
        self.conn.commit()
    
    def clear_all_chats(self, user_id):
        self.cursor.execute("DELETE FROM chat_logs WHERE user_id=?", (user_id,))
        self.conn.commit()
        
        