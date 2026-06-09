"""
Thread-safe SQLite session and interaction manager.

This module keeps assistant state local and offline. It stores sessions and
categorized interactions, then provides a small recent-history window for
faster prompts.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aiassistant.infra.config.app_config import CONFIG


class DatabaseManager:
    """Manages local SQLite persistence for assistant sessions and chat logs."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        configured_path = db_path or str(
            CONFIG.get("paths", {}).get("db_path", "cache/assistant_sessions.db")
        )
        self.db_path = Path(configured_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        
        # Use an RLock for safe, re-entrant thread locking across the instance
        self._lock = threading.RLock()
        self._rad_has_user_id = True

        try:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA busy_timeout=5000;")  
        except sqlite3.Error as e:
            print(f"[Database Warning] Failed to initialize WAL safety layers: {e}")

        self._create_tables()

    # --- Utility Helpers ---

    @staticmethod
    def _now() -> str:
        """Returns the current timestamp in ISO format."""
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _clean_str(val: any, default: str = "") -> str:
        """Safely cleans and strips a string."""
        return str(val or default).strip()

    @staticmethod
    def _hash_password(password: str) -> str:
        return hashlib.sha256(password.encode("utf-8", errors="ignore")).hexdigest()

    def safe_write_query(self, query: str, params: tuple = ()) -> bool:
        """Ensures thread-safe writes preventing SQLite locks."""
        with self._lock:
            try:
                self.conn.execute(query, params)
                self.conn.commit()
                return True
            except sqlite3.OperationalError as e:
                print(f"[DB ERROR] Locked or failed write: {e}")
                self.conn.rollback()
                return False

    def _execute_read_all(self, query: str, params: tuple = ()) -> List[sqlite3.Row]:
        """Helper to execute read queries safely."""
        with self._lock:
            return self.conn.execute(query, params).fetchall()

    def _execute_read_one(self, query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """Helper to execute single read queries safely."""
        with self._lock:
            return self.conn.execute(query, params).fetchone()

    # --- Schema Setup ---

    def _create_tables(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS assistant_sessions (
            session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, label TEXT
        );
        CREATE TABLE IF NOT EXISTS assistant_interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL, role TEXT NOT NULL, message TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'chat',
            FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id)
        );
        CREATE INDEX IF NOT EXISTS idx_assistant_interactions_session_id ON assistant_interactions(session_id);
        CREATE INDEX IF NOT EXISTS idx_assistant_interactions_timestamp ON assistant_interactions(timestamp);

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            login_time TEXT NOT NULL, logout_time TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id ON auth_sessions(user_id);

        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER PRIMARY KEY, preferred_voice TEXT DEFAULT 'system_default',
            preferred_reasoning_model TEXT, preferred_live2d_model TEXT,
            tts_enabled INTEGER DEFAULT 1, voice_input_enabled INTEGER DEFAULT 0,
            rag_enabled INTEGER DEFAULT 1, speaking_speed REAL DEFAULT 1.0,
            system_prompt_behavior TEXT DEFAULT 'default', system_prompt_custom TEXT DEFAULT '',
            updated_at TEXT, FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS rad_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            category TEXT NOT NULL, key_data TEXT NOT NULL, value_data TEXT NOT NULL,
            confidence_score REAL DEFAULT 1.0, created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS searchable_mirror (
            id INTEGER PRIMARY KEY AUTOINCREMENT, file_path TEXT UNIQUE NOT NULL,
            raw_text_or_data TEXT, file_hash TEXT, updated_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS searchable_mirror_fts USING fts5(
            file_path, raw_text_or_data, content='searchable_mirror', content_rowid='id'
        );
        CREATE INDEX IF NOT EXISTS idx_searchable_mirror_path ON searchable_mirror(file_path);

        CREATE TABLE IF NOT EXISTS style_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT UNIQUE NOT NULL,
            profile_json TEXT, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_style_profiles_scope ON style_profiles(scope);
        """
        with self._lock:
            self.conn.executescript(schema)
            self._ensure_schema_migrations()
            self.conn.commit()

    def _ensure_schema_migrations(self) -> None:
        row = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rad_memory'").fetchone()
        if not row:
            self._rad_has_user_id = True
            return

        columns = {col["name"] for col in self.conn.execute("PRAGMA table_info(rad_memory)").fetchall()}

        if "created_at" not in columns:
            self.conn.execute("ALTER TABLE rad_memory ADD COLUMN created_at TEXT")
            self.conn.execute("UPDATE rad_memory SET created_at = COALESCE(created_at, ?) WHERE created_at IS NULL OR created_at = ''", (self._now(),))

        self._rad_has_user_id = "user_id" in columns
        if self._rad_has_user_id:
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_rad_memory_user_id ON rad_memory(user_id)")

        pref_row = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_preferences'").fetchone()
        if pref_row:
            pref_columns = {col["name"] for col in self.conn.execute("PRAGMA table_info(user_preferences)").fetchall()}
            if "system_prompt_behavior" not in pref_columns:
                self.conn.execute("ALTER TABLE user_preferences ADD COLUMN system_prompt_behavior TEXT DEFAULT 'default'")
            if "system_prompt_custom" not in pref_columns:
                self.conn.execute("ALTER TABLE user_preferences ADD COLUMN system_prompt_custom TEXT DEFAULT ''")

    # --- Session & Interactions ---

    def create_session(self, label: Optional[str] = None) -> str:
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.safe_write_query(
            "INSERT INTO assistant_sessions (session_id, started_at, label) VALUES (?, ?, ?)",
            (session_id, self._now(), label),
        )
        return session_id

    def log_interaction(self, session_id: str, role: str, message: str, category: str = "chat") -> None:
        self.safe_write_query(
            "INSERT INTO assistant_interactions (session_id, timestamp, role, message, category) VALUES (?, ?, ?, ?, ?)",
            (session_id, self._now(), role, message, category)
        )

    def get_recent_turns(self, session_id: str, turn_limit: int = 3) -> List[Dict[str, str]]:
        row_limit = max(1, turn_limit) * 2
        rows = self._execute_read_all(
            """SELECT role, message, timestamp FROM assistant_interactions 
               WHERE session_id = ? AND role IN ('user', 'assistant') ORDER BY id DESC LIMIT ?""",
            (session_id, row_limit)
        )
        return [{"role": r["role"], "message": r["message"], "timestamp": r["timestamp"]} for r in reversed(rows)]

    def get_all_session_messages(self, session_id: str) -> List[Dict[str, str]]:
        rows = self._execute_read_all(
            "SELECT role, message, category, timestamp FROM assistant_interactions WHERE session_id = ? ORDER BY id ASC",
            (session_id,)
        )
        return [dict(r) for r in rows]

    def list_sessions(self, limit: int = 40) -> List[Dict[str, str]]:
        rows = self._execute_read_all(
            """SELECT s.session_id, s.started_at, COALESCE(s.label, '') AS label, COUNT(i.id) AS message_count
               FROM assistant_sessions s LEFT JOIN assistant_interactions i ON i.session_id = s.session_id
               GROUP BY s.session_id, s.started_at, s.label ORDER BY s.started_at DESC LIMIT ?""",
            (max(1, limit),)
        )
        return [{"session_id": r["session_id"], "started_at": r["started_at"], "label": r["label"], "message_count": str(r["message_count"])} for r in rows]

    def delete_session_history(self, session_id: str) -> bool:
        clean_id = self._clean_str(session_id)
        if not clean_id: return False

        with self._lock:
            self.conn.execute("DELETE FROM assistant_interactions WHERE session_id = ?", (clean_id,))
            cursor = self.conn.execute("DELETE FROM assistant_sessions WHERE session_id = ?", (clean_id,))
            self.conn.commit()
            return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # --- Searchable Mirror ---

    def save_searchable_mirror(self, file_path: str, raw_text: str, file_hash: str) -> None:
        if not file_path or not raw_text: return
        self.safe_write_query(
            """INSERT INTO searchable_mirror (file_path, raw_text_or_data, file_hash, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET raw_text_or_data=excluded.raw_text_or_data, file_hash=excluded.file_hash, updated_at=excluded.updated_at""",
            (str(file_path), str(raw_text), str(file_hash or ""), self._now())
        )

    def get_searchable_mirror_hash(self, file_path: str) -> Optional[str]:
        clean_path = self._clean_str(file_path)
        if not clean_path: return None
        row = self._execute_read_one("SELECT file_hash FROM searchable_mirror WHERE file_path = ?", (clean_path,))
        return str(row["file_hash"]) if row else None

    def search_searchable_mirror(self, query: str, limit: int = 8) -> List[Dict[str, str]]:
        clean_query = self._clean_str(query)
        if not clean_query: return []

        rows = self._execute_read_all(
            "SELECT file_path, raw_text_or_data FROM searchable_mirror_fts WHERE searchable_mirror_fts MATCH ? ORDER BY rank LIMIT ?",
            (f'"{clean_query}"', max(1, limit))
        )

        results = []
        for row in rows:
            raw = str(row["raw_text_or_data"] or "")
            idx = raw.lower().find(clean_query.lower())
            snippet = raw[max(0, idx - 120):min(len(raw), idx + 240)].strip() if idx >= 0 else ""
            results.append({"file_path": str(row["file_path"]), "snippet": snippet})
        return results

    # --- Style Profiles ---

    def save_style_profile(self, scope: str, profile_json: Dict[str, object]) -> None:
        clean_scope = self._clean_str(scope, "default") or "default"
        self.safe_write_query(
            """INSERT INTO style_profiles (scope, profile_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(scope) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at""",
            (clean_scope, json.dumps(profile_json or {}, ensure_ascii=True), self._now())
        )

    def get_style_profile(self, scope: str = "default") -> Optional[Dict[str, object]]:
        clean_scope = self._clean_str(scope, "default") or "default"
        row = self._execute_read_one("SELECT profile_json FROM style_profiles WHERE scope = ?", (clean_scope,))
        if row and row["profile_json"]:
            try:
                return json.loads(row["profile_json"])
            except json.JSONDecodeError:
                pass
        return None

    # --- User Auth & Preferences ---

    def register_user(self, username: str, password: str) -> Tuple[bool, str]:
        user, pwd = self._clean_str(username), self._clean_str(password)
        if not user or not pwd: return False, "Username and password cannot be empty."

        try:
            self.safe_write_query(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (user, self._hash_password(pwd), self._now())
            )
            return True, "Account created successfully."
        except sqlite3.IntegrityError:
            return False, "Username already exists."

    def _create_auth_session(self, user_id: int, username: str) -> Dict[str, object]:
        """Helper to create an auth session and return unified response."""
        self.safe_write_query(
            "INSERT INTO auth_sessions (user_id, login_time) VALUES (?, ?)", 
            (user_id, self._now())
        )
        return {"user_id": user_id, "username": username}

    def login_user(self, username: str, password: str) -> Optional[Dict[str, object]]:
        user, pwd = self._clean_str(username), self._clean_str(password)
        if not user or not pwd: return None

        row = self._execute_read_one(
            "SELECT id, username FROM users WHERE username = ? AND password_hash = ?",
            (user, self._hash_password(pwd))
        )
        return self._create_auth_session(row["id"], row["username"]) if row else None

    def resume_user_session(self, user_id: int) -> Optional[Dict[str, object]]:
        row = self._execute_read_one("SELECT username FROM users WHERE id = ?", (int(user_id),))
        return self._create_auth_session(user_id, row["username"]) if row else None

    def logout_user(self, user_id: int) -> None:
        self.safe_write_query(
            "UPDATE auth_sessions SET logout_time = ? WHERE user_id = ? AND logout_time IS NULL",
            (self._now(), int(user_id))
        )

    def user_exists(self, user_id: int) -> bool:
        return bool(self._execute_read_one("SELECT id FROM users WHERE id = ?", (int(user_id),)))

    def get_username(self, user_id: int) -> Optional[str]:
        row = self._execute_read_one("SELECT username FROM users WHERE id = ?", (int(user_id),))
        return str(row["username"]) if row else None

    def save_user_preference(self, user_id: int, preference: Dict[str, object]) -> None:
        if not preference: return

        bool_keys = {"tts_enabled", "voice_input_enabled", "rag_enabled"}
        updates, values = [], []

        for key, value in preference.items():
            if value is None: continue

            if key in bool_keys:
                parsed_val = 1 if bool(value) else 0
            elif key == "speaking_speed":
                try: parsed_val = max(0.4, min(float(value), 2.5))
                except (TypeError, ValueError): continue
            else:
                parsed_val = str(value).strip()

            updates.append(f"{key} = ?")
            values.append(parsed_val)

        if not updates: return
        
        updated_at = self._now()
        with self._lock:
            self.conn.execute("INSERT OR IGNORE INTO user_preferences (user_id, updated_at) VALUES (?, ?)", (int(user_id), updated_at))
            values.extend([updated_at, int(user_id)])
            self.conn.execute(f"UPDATE user_preferences SET {', '.join(updates)}, updated_at = ? WHERE user_id = ?", tuple(values))
            self.conn.commit()

    def get_user_preference(self, user_id: int) -> Dict[str, object]:
        defaults = {
            "preferred_voice": "system_default",
            "preferred_reasoning_model": str(CONFIG.get("ollama", {}).get("model", "qwen2.5-coder:7b")),
            "preferred_live2d_model": str(CONFIG.get("paths", {}).get("default_live2d_model", "")),
            "tts_enabled": True, "voice_input_enabled": False, "rag_enabled": True,
            "speaking_speed": float(CONFIG.get("voice", {}).get("speaking_speed", 1.0)),
            "system_prompt_behavior": "default", "system_prompt_custom": "",
        }

        row = self._execute_read_one("SELECT * FROM user_preferences WHERE user_id = ?", (int(user_id),))
        if not row: return defaults

        res = dict(defaults)
        res["preferred_voice"] = str(row["preferred_voice"] or defaults["preferred_voice"])
        res["preferred_reasoning_model"] = str(row["preferred_reasoning_model"] or defaults["preferred_reasoning_model"])
        res["preferred_live2d_model"] = str(row["preferred_live2d_model"] or defaults["preferred_live2d_model"])
        
        res["tts_enabled"] = bool(row["tts_enabled"]) if row["tts_enabled"] is not None else defaults["tts_enabled"]
        res["voice_input_enabled"] = bool(row["voice_input_enabled"]) if row["voice_input_enabled"] is not None else defaults["voice_input_enabled"]
        res["rag_enabled"] = bool(row["rag_enabled"]) if row["rag_enabled"] is not None else defaults["rag_enabled"]
        
        try: res["speaking_speed"] = max(0.4, min(float(row["speaking_speed"]), 2.5))
        except (TypeError, ValueError): pass

        res["system_prompt_behavior"] = str(row["system_prompt_behavior"] or defaults["system_prompt_behavior"]).strip() or "default"
        res["system_prompt_custom"] = str(row["system_prompt_custom"] or defaults["system_prompt_custom"])
        return res

    # --- RAD Memory ---

    def add_rad_data(self, user_id: int, category: str, key_data: str, value_data: str, confidence_score: float = 1.0) -> None:
        cat = self._clean_str(category, "user_fact") or "user_fact"
        kd, vd = self._clean_str(key_data), self._clean_str(value_data)
        
        if self._rad_has_user_id:
            self.safe_write_query(
                "INSERT INTO rad_memory (user_id, category, key_data, value_data, confidence_score, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (int(user_id), cat, kd, vd, float(confidence_score), self._now())
            )
        else:
            self.safe_write_query(
                "INSERT INTO rad_memory (category, key_data, value_data, confidence_score, created_at) VALUES (?, ?, ?, ?, ?)",
                (cat, kd, vd, float(confidence_score), self._now())
            )

    def add_rad_data_if_new(self, user_id: int, category: str, key_data: str, value_data: str, confidence_score: float = 1.0) -> bool:
        cat = self._clean_str(category, "user_fact") or "user_fact"
        kd, vd = self._clean_str(key_data), self._clean_str(value_data)
        if not kd or not vd: return False

        query = "SELECT id FROM rad_memory WHERE lower(category) = lower(?) AND lower(key_data) = lower(?) AND lower(value_data) = lower(?)"
        params = [cat, kd, vd]

        if self._rad_has_user_id:
            query += " AND user_id = ?"
            params.append(int(user_id))

        if self._execute_read_one(query + " LIMIT 1", tuple(params)):
            return False

        self.add_rad_data(user_id, cat, kd, vd, confidence_score)
        return True

    @staticmethod
    def _extract_important_facts(text: str) -> List[Tuple[str, str, float]]:
        clean = str(text or "").strip()
        if not clean: return []

        facts = []
        lowered = clean.lower()

        for prefix in ["remember that", "remember", "note that", "important", "store this"]:
            if lowered.startswith(prefix):
                if note := clean[len(prefix):].strip(" .,:;"):
                    facts.append(("remembered_note", note, 0.96))
                break

        patterns = [
            (r"\bmy name is\s+([a-zA-Z][a-zA-Z\s\.'-]{1,40})", "name", 0.98),
            (r"\bi am\s+(\d{1,3})\s+years old\b", "age", 0.97),
            (r"\bmy birthday is\s+([a-zA-Z0-9\s,/-]{2,40})", "birthday", 0.96),
            (r"\bi live in\s+([a-zA-Z\s\.'-]{2,50})", "location", 0.93),
            (r"\bi work as\s+([a-zA-Z\s\.'-]{2,50})", "job", 0.90),
            (r"\bi prefer\s+([a-zA-Z0-9\s\.,'-]{2,70})", "preference", 0.88),
        ]

        for pattern, key, conf in patterns:
            if match := re.search(pattern, clean, flags=re.IGNORECASE):
                if val := match.group(1).strip(" ."): facts.append((key, val, conf))

        if match := re.search(r"\bmy favorite\s+([a-zA-Z\s]{2,24})\s+is\s+([a-zA-Z0-9\s\.,'-]{1,60})", clean, flags=re.IGNORECASE):
            topic = re.sub(r"\s+", "_", match.group(1).strip().lower())
            if val := match.group(2).strip(" ."): facts.append((f"favorite_{topic}", val, 0.91))

        return facts

    @staticmethod
    def _extract_assistant_points(assistant_text: str) -> List[Tuple[str, str, float]]:
        clean = str(assistant_text or "").strip()
        if not clean: return []

        points = []
        for line in filter(None, (ln.strip() for ln in clean.splitlines())):
            if ":" not in (collapsed := line.strip("-*• ")): continue
            k, v = collapsed.split(":", 1)
            key = re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_")
            val = v.strip(" .")

            if 0 < len(key) <= 36 and len(val) >= 6:
                points.append((f"assistant_{key}", val, 0.72))
                if len(points) >= 5: break
        return points

    def auto_store_important_conversation_data(self, user_id: int, user_text: str, assistant_text: str = "") -> List[Dict[str, str]]:
        if not user_id: return []

        candidates = self._extract_important_facts(user_text) + self._extract_assistant_points(assistant_text)
        stored, seen = [], set()

        for key, value, conf in candidates:
            if (key, value) in seen: continue
            seen.add((key, value))

            if self.add_rad_data_if_new(int(user_id), "auto_fact", key, value, conf):
                stored.append({"key": key, "value": value})
        return stored

    def get_rad_data(self, user_id: int, limit: int = 300) -> List[Dict[str, object]]:
        query = "SELECT id, category, key_data, value_data, confidence_score, created_at FROM rad_memory"
        params = []
        
        if self._rad_has_user_id:
            query += " WHERE user_id = ?"
            params.append(int(user_id))
            
        rows = self._execute_read_all(query + " ORDER BY id DESC LIMIT ?", tuple(params + [max(1, limit)]))
        return [dict(r) for r in rows]

    def delete_rad_data(self, user_id: int, rad_id: int) -> bool:
        query = "DELETE FROM rad_memory WHERE id = ?"
        params = [int(rad_id)]
        
        if self._rad_has_user_id:
            query += " AND user_id = ?"
            params.append(int(user_id))
            
        with self._lock:
            cursor = self.conn.execute(query, tuple(params))
            self.conn.commit()
            return cursor.rowcount > 0

    def build_memory_context(self, user_id: int, session_id: Optional[str] = None) -> str:
        rad_limit = int(CONFIG.get("memory", {}).get("rad_limit", 220))
        turn_limit = int(CONFIG.get("memory", {}).get("recent_turn_limit", 10))
        max_context_chars = int(CONFIG.get("memory", {}).get("max_context_chars", 9000))

        rad_context = "\n".join(f"{r['key_data']}: {r['value_data']}" for r in reversed(self.get_rad_data(user_id, rad_limit)))
        turn_context = "\n".join(f"{t['role'].title()}: {t['message']}" for t in self.get_recent_turns(session_id, turn_limit)) if session_id else ""

        chunks = []
        if rad_context: chunks.append(f"Known memory:\n{rad_context}")
        if turn_context: chunks.append(f"Recent conversation:\n{turn_context}")

        context = "\n\n".join(chunks)
        return context if len(context) <= max_context_chars else context[-max_context_chars:]