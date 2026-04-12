import sqlite3
import hashlib
import re

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
                session_id INTEGER,
                message_type TEXT, -- 'user' or 'marie'
                content TEXT,
                emotion_tag TEXT, -- e.g. [happy]
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                user_text TEXT,
                assistant_text TEXT,
                important INTEGER DEFAULT 0,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(session_id) REFERENCES sessions(id)
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

        self._ensure_schema_migrations()

        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_logs_user_session ON chat_logs(user_id, session_id)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_turns_user_session ON conversation_turns(user_id, session_id)")
        self.conn.commit()

    def _ensure_schema_migrations(self):
        """Applies safe migrations for existing databases."""
        self.cursor.execute("PRAGMA table_info(chat_logs)")
        columns = {row[1] for row in self.cursor.fetchall()}
        if "session_id" not in columns:
            self.cursor.execute("ALTER TABLE chat_logs ADD COLUMN session_id INTEGER")

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
            session_id = self.create_session(user_id)
            return user_id, session_id
        return None

    def create_session(self, user_id):
        self.cursor.execute("INSERT INTO sessions (user_id) VALUES (?)", (user_id,))
        self.conn.commit()
        return self.cursor.lastrowid

    def user_exists(self, user_id):
        self.cursor.execute("SELECT id FROM users WHERE id=?", (user_id,))
        return self.cursor.fetchone() is not None

    def resume_user_session(self, user_id):
        if not self.user_exists(user_id):
            return None
        return self.create_session(user_id)

    def logout_user(self, user_id, session_id=None):
        if session_id:
            self.cursor.execute('''
                UPDATE sessions SET logout_time = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ? AND logout_time IS NULL
            ''', (session_id, user_id))
        else:
            self.cursor.execute('''
                UPDATE sessions SET logout_time = CURRENT_TIMESTAMP
                WHERE user_id = ? AND logout_time IS NULL
            ''', (user_id,))
        self.conn.commit()

    # --- LOGGING METHODS ---
    def log_chat(self, user_id, sender, text, emotion="neutral", session_id=None):
        active_session_id = session_id
        if active_session_id is None and user_id is not None:
            self.cursor.execute(
                "SELECT id FROM sessions WHERE user_id=? AND logout_time IS NULL ORDER BY id DESC LIMIT 1",
                (user_id,)
            )
            row = self.cursor.fetchone()
            if row:
                active_session_id = row[0]

        self.cursor.execute(
            "INSERT INTO chat_logs (user_id, session_id, message_type, content, emotion_tag) VALUES (?, ?, ?, ?, ?)",
            (user_id, active_session_id, sender, text, emotion)
        )
        self.conn.commit()

    def get_chat_logs(self, user_id, session_id=None):
        if session_id is None:
            self.cursor.execute(
                "SELECT id, session_id, timestamp, message_type, content, emotion_tag FROM chat_logs WHERE user_id=? ORDER BY id DESC",
                (user_id,)
            )
        else:
            self.cursor.execute(
                "SELECT id, session_id, timestamp, message_type, content, emotion_tag FROM chat_logs WHERE user_id=? AND session_id=? ORDER BY id DESC",
                (user_id, session_id)
            )
        return self.cursor.fetchall()

    def log_conversation_turn(self, user_id, session_id, user_text, assistant_text, important=0):
        if user_id is None or session_id is None:
            return
        self.cursor.execute(
            "INSERT INTO conversation_turns (user_id, session_id, user_text, assistant_text, important) VALUES (?, ?, ?, ?, ?)",
            (user_id, session_id, user_text, assistant_text, int(bool(important))),
        )
        self.conn.commit()

    def get_conversation_turns(self, user_id, session_id=None, limit=200):
        if session_id is None:
            self.cursor.execute(
                "SELECT id, session_id, timestamp, user_text, assistant_text, important FROM conversation_turns WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            )
        else:
            self.cursor.execute(
                "SELECT id, session_id, timestamp, user_text, assistant_text, important FROM conversation_turns WHERE user_id=? AND session_id=? ORDER BY id DESC LIMIT ?",
                (user_id, session_id, limit),
            )
        return self.cursor.fetchall()

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
    def add_rad_data(self, category, key, value, confidence=1.0):
        self.cursor.execute(
            "INSERT INTO rad_memory (category, key_data, value_data, confidence_score) VALUES (?, ?, ?, ?)",
            (category, key, value, confidence),
        )
        self.conn.commit()

    def add_rad_data_if_new(self, category, key, value, confidence=1.0):
        self.cursor.execute(
            "SELECT id FROM rad_memory WHERE lower(category)=lower(?) AND lower(key_data)=lower(?) AND lower(value_data)=lower(?) LIMIT 1",
            (category, key, value),
        )
        if self.cursor.fetchone():
            return False

        self.cursor.execute(
            "INSERT INTO rad_memory (category, key_data, value_data, confidence_score) VALUES (?, ?, ?, ?)",
            (category, key, value, confidence),
        )
        self.conn.commit()
        return True

    def _extract_important_facts(self, text):
        if not text:
            return []

        text = text.strip()
        lowered = text.lower()
        facts = []

        explicit_prefixes = ["remember that", "remember", "note that", "important", "store this"]
        for prefix in explicit_prefixes:
            if lowered.startswith(prefix):
                note = text[len(prefix):].strip(" .,:;")
                if note:
                    facts.append(("remembered_note", note, 0.96))
                break

        pattern_rules = [
            (r"\bmy name is\s+([a-zA-Z][a-zA-Z\s\.'-]{1,40})", "name", 0.98),
            (r"\bi am\s+(\d{1,3})\s+years old\b", "age", 0.97),
            (r"\bmy birthday is\s+([a-zA-Z0-9\s,/-]{2,40})", "birthday", 0.96),
            (r"\bi live in\s+([a-zA-Z\s\.'-]{2,50})", "location", 0.93),
            (r"\bi work as\s+([a-zA-Z\s\.'-]{2,50})", "job", 0.90),
            (r"\bi prefer\s+([a-zA-Z0-9\s\.,'-]{2,70})", "preference", 0.88),
        ]

        for pattern, key, confidence in pattern_rules:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                value = match.group(1).strip(" .")
                if value:
                    facts.append((key, value, confidence))

        favorite_match = re.search(
            r"\bmy favorite\s+([a-zA-Z\s]{2,24})\s+is\s+([a-zA-Z0-9\s\.,'-]{1,60})",
            text,
            flags=re.IGNORECASE,
        )
        if favorite_match:
            topic = re.sub(r"\s+", "_", favorite_match.group(1).strip().lower())
            value = favorite_match.group(2).strip(" .")
            if topic and value:
                facts.append((f"favorite_{topic}", value, 0.91))

        return facts

    def _extract_assistant_points(self, assistant_text):
        if not assistant_text:
            return []

        points = []
        lines = [line.strip() for line in assistant_text.splitlines() if line.strip()]
        for line in lines:
            cleaned = line.strip("-*• ")
            if ":" not in cleaned:
                continue

            key_part, value_part = cleaned.split(":", 1)
            key = re.sub(r"[^a-z0-9]+", "_", key_part.lower()).strip("_")
            value = value_part.strip(" .")
            if not key or not value:
                continue
            if len(key) > 36 or len(value) < 6:
                continue

            points.append((f"assistant_{key}", value, 0.72))
            if len(points) >= 5:
                break

        return points

    def auto_store_important_conversation_data(self, user_text, assistant_text=""):
        stored = []
        candidate_facts = self._extract_important_facts(user_text)
        candidate_facts.extend(self._extract_assistant_points(assistant_text))

        for key, value, confidence in candidate_facts:
            if self.add_rad_data_if_new("auto_fact", key, value, confidence):
                stored.append((key, value))
        return stored

    def get_all_rad_data(self, limit=300):
        """Fetches stored facts to use as context for the AI."""
        self.cursor.execute(
            "SELECT key_data, value_data FROM rad_memory ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = self.cursor.fetchall()
        
        if not rows:
            return ""
            
        # Format: "key: value"
        context_list = [f"{row[0]}: {row[1]}" for row in reversed(rows)]
        return "\n".join(context_list)

    def get_recent_turn_context(self, user_id, session_id=None, limit=10):
        if user_id is None:
            return ""

        if session_id is None:
            self.cursor.execute(
                "SELECT user_text, assistant_text FROM conversation_turns WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            )
        else:
            self.cursor.execute(
                "SELECT user_text, assistant_text FROM conversation_turns WHERE user_id=? AND session_id=? ORDER BY id DESC LIMIT ?",
                (user_id, session_id, limit),
            )

        rows = self.cursor.fetchall()
        if not rows:
            return ""

        lines = []
        for user_text, assistant_text in reversed(rows):
            if user_text:
                lines.append(f"User: {user_text}")
            if assistant_text:
                lines.append(f"Marie: {assistant_text}")

        return "\n".join(lines)

    def build_memory_context(self, user_id, session_id=None):
        """Combines RAD memory + prior conversation turns for continuity."""
        rad_context = self.get_all_rad_data(limit=220)
        turn_context = self.get_recent_turn_context(user_id, session_id=session_id, limit=10)

        chunks = []
        if rad_context:
            chunks.append("Known memory:\n" + rad_context)
        if turn_context:
            chunks.append("Recent conversation:\n" + turn_context)

        return "\n\n".join(chunks)
    
    # --- DELETE METHODS ---
    def delete_chat_log(self, log_id):
        self.cursor.execute("DELETE FROM chat_logs WHERE id=?", (log_id,))
        self.conn.commit()

    def delete_rad_data(self, rad_id):
        self.cursor.execute("DELETE FROM rad_memory WHERE id=?", (rad_id,))
        self.conn.commit()
    
    def clear_all_chats(self, user_id, session_id=None):
        if session_id is None:
            self.cursor.execute("DELETE FROM chat_logs WHERE user_id=?", (user_id,))
            self.cursor.execute("DELETE FROM conversation_turns WHERE user_id=?", (user_id,))
        else:
            self.cursor.execute("DELETE FROM chat_logs WHERE user_id=? AND session_id=?", (user_id, session_id))
            self.cursor.execute("DELETE FROM conversation_turns WHERE user_id=? AND session_id=?", (user_id, session_id))
        self.conn.commit()
