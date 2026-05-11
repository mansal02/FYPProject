"""
Thread-safe SQLite session and interaction manager.

This module keeps assistant state local and offline. It stores sessions and
categorized interactions, then provides a small recent-history window for
faster prompts.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from aiassistant.infra.config.app_config import CONFIG


class DatabaseManager:
    """Manages local SQLite persistence for assistant sessions and chat logs."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        configured_path = db_path or str(
            CONFIG.get("paths", {}).get("db_path", "cache/assistant_sessions.db")
        )
        self.db_path = Path(configured_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False allows usage from GUI worker threads.
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._rad_has_user_id = True

        self._create_tables()

    def _create_tables(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS assistant_sessions (
            session_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            label TEXT
        );

        CREATE TABLE IF NOT EXISTS assistant_interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'chat',
            FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_assistant_interactions_session_id
            ON assistant_interactions(session_id);

        CREATE INDEX IF NOT EXISTS idx_assistant_interactions_timestamp
            ON assistant_interactions(timestamp);

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS auth_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            login_time TEXT NOT NULL,
            logout_time TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id
            ON auth_sessions(user_id);

        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER PRIMARY KEY,
            preferred_voice TEXT DEFAULT 'system_default',
            preferred_reasoning_model TEXT,
            preferred_vision_model TEXT,
            preferred_live2d_model TEXT,
            tts_enabled INTEGER DEFAULT 1,
            voice_input_enabled INTEGER DEFAULT 0,
            screen_capture_enabled INTEGER DEFAULT 0,
            screen_preview_enabled INTEGER DEFAULT 0,
            rag_enabled INTEGER DEFAULT 1,
            desktop_mate_enabled INTEGER DEFAULT 0,
            speaking_speed REAL DEFAULT 1.0,
            system_prompt_behavior TEXT DEFAULT 'default',
            system_prompt_custom TEXT DEFAULT '',
            updated_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS rad_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            key_data TEXT NOT NULL,
            value_data TEXT NOT NULL,
            confidence_score REAL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS searchable_mirror (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE NOT NULL,
            raw_text_or_data TEXT,
            file_hash TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS style_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT UNIQUE NOT NULL,
            profile_json TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_searchable_mirror_path
            ON searchable_mirror(file_path);
        CREATE INDEX IF NOT EXISTS idx_style_profiles_scope
            ON style_profiles(scope);
        """
        with self._lock:
            self.conn.executescript(schema)
            self._ensure_schema_migrations()
            self.conn.commit()

    def _ensure_schema_migrations(self) -> None:
        # Legacy databases may have an earlier rad_memory schema without user_id/created_at.
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='rad_memory'"
        ).fetchone()
        if not row:
            self._rad_has_user_id = True
            return

        columns = {
            col["name"]
            for col in self.conn.execute("PRAGMA table_info(rad_memory)").fetchall()
        }

        if "created_at" not in columns:
            self.conn.execute("ALTER TABLE rad_memory ADD COLUMN created_at TEXT")

        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE rad_memory SET created_at = COALESCE(created_at, ?) WHERE created_at IS NULL OR created_at = ''",
            (now,),
        )

        self._rad_has_user_id = "user_id" in columns
        if self._rad_has_user_id:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rad_memory_user_id ON rad_memory(user_id)"
            )

        pref_row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_preferences'"
        ).fetchone()
        if pref_row:
            pref_columns = {
                col["name"]
                for col in self.conn.execute("PRAGMA table_info(user_preferences)").fetchall()
            }
            if "system_prompt_behavior" not in pref_columns:
                self.conn.execute(
                    "ALTER TABLE user_preferences ADD COLUMN system_prompt_behavior TEXT DEFAULT 'default'"
                )
            if "system_prompt_custom" not in pref_columns:
                self.conn.execute(
                    "ALTER TABLE user_preferences ADD COLUMN system_prompt_custom TEXT DEFAULT ''"
                )

    @staticmethod
    def _hash_password(password: str) -> str:
        return hashlib.sha256(password.encode("utf-8", errors="ignore")).hexdigest()

    def create_session(self, label: Optional[str] = None) -> str:
        now = datetime.now()
        session_id = now.strftime("%Y%m%d_%H%M%S_%f")
        started_at = now.isoformat(timespec="seconds")

        with self._lock:
            self.conn.execute(
                "INSERT INTO assistant_sessions (session_id, started_at, label) VALUES (?, ?, ?)",
                (session_id, started_at, label),
            )
            self.conn.commit()

        return session_id

    def log_interaction(
        self,
        session_id: str,
        role: str,
        message: str,
        category: str = "chat",
    ) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO assistant_interactions (session_id, timestamp, role, message, category)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, timestamp, role, message, category),
            )
            self.conn.commit()

    def get_recent_turns(self, session_id: str, turn_limit: int = 3) -> List[Dict[str, str]]:
        """
        Returns the most recent user/assistant messages for prompt context.

        turn_limit=3 means up to 3 user turns + 3 assistant turns for speed.
        """
        row_limit = max(1, turn_limit) * 2

        with self._lock:
            rows = self.conn.execute(
                """
                SELECT role, message, timestamp
                                FROM assistant_interactions
                WHERE session_id = ?
                  AND role IN ('user', 'assistant')
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, row_limit),
            ).fetchall()

        # Reverse so prompts are oldest -> newest.
        ordered_rows = list(reversed(rows))
        return [
            {
                "role": row["role"],
                "message": row["message"],
                "timestamp": row["timestamp"],
            }
            for row in ordered_rows
        ]

    def get_all_session_messages(self, session_id: str) -> List[Dict[str, str]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT role, message, category, timestamp
                FROM assistant_interactions
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()

        return [
            {
                "role": row["role"],
                "message": row["message"],
                "category": row["category"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]

    def list_sessions(self, limit: int = 40) -> List[Dict[str, str]]:
        """Returns recent sessions with quick interaction counts for UI history browsing."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT
                    s.session_id,
                    s.started_at,
                    COALESCE(s.label, '') AS label,
                    COUNT(i.id) AS message_count
                FROM assistant_sessions s
                LEFT JOIN assistant_interactions i ON i.session_id = s.session_id
                GROUP BY s.session_id, s.started_at, s.label
                ORDER BY s.started_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()

        return [
            {
                "session_id": row["session_id"],
                "started_at": row["started_at"],
                "label": row["label"],
                "message_count": str(row["message_count"]),
            }
            for row in rows
        ]

    def delete_session_history(self, session_id: str) -> bool:
        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            return False

        with self._lock:
            self.conn.execute(
                "DELETE FROM assistant_interactions WHERE session_id = ?",
                (clean_session_id,),
            )
            cursor = self.conn.execute(
                "DELETE FROM assistant_sessions WHERE session_id = ?",
                (clean_session_id,),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # --- Searchable mirror ---
    def save_searchable_mirror(self, file_path: str, raw_text: str, file_hash: str) -> None:
        if not file_path or not raw_text:
            return
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO searchable_mirror (file_path, raw_text_or_data, file_hash, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    raw_text_or_data=excluded.raw_text_or_data,
                    file_hash=excluded.file_hash,
                    updated_at=excluded.updated_at
                """,
                (str(file_path), str(raw_text), str(file_hash or ""), now),
            )
            self.conn.commit()

    def get_searchable_mirror_hash(self, file_path: str) -> str | None:
        clean = str(file_path or "").strip()
        if not clean:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT file_hash FROM searchable_mirror WHERE file_path = ?",
                (clean,),
            ).fetchone()
        if not row:
            return None
        return str(row["file_hash"] or "")

    def search_searchable_mirror(self, query: str, limit: int = 8) -> List[Dict[str, str]]:
        clean = str(query or "").strip()
        if not clean:
            return []
        wildcard = f"%{clean}%"
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT file_path, raw_text_or_data
                FROM searchable_mirror
                WHERE raw_text_or_data LIKE ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (wildcard, max(1, int(limit))),
            ).fetchall()

        results: List[Dict[str, str]] = []
        for row in rows:
            raw = str(row["raw_text_or_data"] or "")
            idx = raw.lower().find(clean.lower())
            snippet = ""
            if idx >= 0:
                start = max(0, idx - 120)
                end = min(len(raw), idx + 240)
                snippet = raw[start:end].strip()
            results.append({"file_path": str(row["file_path"]), "snippet": snippet})
        return results

    # --- Style profile ---
    def save_style_profile(self, scope: str, profile_json: Dict[str, object]) -> None:
        clean_scope = str(scope or "default").strip() or "default"
        now = datetime.now().isoformat(timespec="seconds")
        payload = json.dumps(profile_json or {}, ensure_ascii=True)
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO style_profiles (scope, profile_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    profile_json=excluded.profile_json,
                    updated_at=excluded.updated_at
                """,
                (clean_scope, payload, now),
            )
            self.conn.commit()

    def get_style_profile(self, scope: str = "default") -> Dict[str, object] | None:
        clean_scope = str(scope or "default").strip() or "default"
        with self._lock:
            row = self.conn.execute(
                "SELECT profile_json FROM style_profiles WHERE scope = ?",
                (clean_scope,),
            ).fetchone()
        if not row or not row["profile_json"]:
            return None
        try:
            return json.loads(row["profile_json"])
        except Exception:
            return None

    # --- User auth + preferences ---
    def register_user(self, username: str, password: str) -> tuple[bool, str]:
        clean_username = (username or "").strip()
        clean_password = (password or "").strip()

        if not clean_username or not clean_password:
            return False, "Username and password cannot be empty."

        now = datetime.now().isoformat(timespec="seconds")
        pwd_hash = self._hash_password(clean_password)

        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (clean_username, pwd_hash, now),
                )
                self.conn.commit()
            return True, "Account created successfully."
        except sqlite3.IntegrityError:
            return False, "Username already exists."

    def login_user(self, username: str, password: str) -> Optional[Dict[str, object]]:
        clean_username = (username or "").strip()
        clean_password = (password or "").strip()
        if not clean_username or not clean_password:
            return None

        pwd_hash = self._hash_password(clean_password)
        login_time = datetime.now().isoformat(timespec="seconds")

        with self._lock:
            row = self.conn.execute(
                "SELECT id, username FROM users WHERE username = ? AND password_hash = ?",
                (clean_username, pwd_hash),
            ).fetchone()
            if not row:
                return None

            user_id = int(row["id"])
            self.conn.execute(
                "INSERT INTO auth_sessions (user_id, login_time) VALUES (?, ?)",
                (user_id, login_time),
            )
            self.conn.commit()

        return {"user_id": user_id, "username": str(row["username"]) }

    def resume_user_session(self, user_id: int) -> Optional[Dict[str, object]]:
        if not self.user_exists(user_id):
            return None

        login_time = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            row = self.conn.execute(
                "SELECT username FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return None

            self.conn.execute(
                "INSERT INTO auth_sessions (user_id, login_time) VALUES (?, ?)",
                (user_id, login_time),
            )
            self.conn.commit()

        return {"user_id": int(user_id), "username": str(row["username"]) }

    def logout_user(self, user_id: int) -> None:
        logout_time = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self.conn.execute(
                """
                UPDATE auth_sessions
                SET logout_time = ?
                WHERE user_id = ? AND logout_time IS NULL
                """,
                (logout_time, int(user_id)),
            )
            self.conn.commit()

    def user_exists(self, user_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT id FROM users WHERE id = ?",
                (int(user_id),),
            ).fetchone()
        return bool(row)

    def get_username(self, user_id: int) -> Optional[str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT username FROM users WHERE id = ?",
                (int(user_id),),
            ).fetchone()
        if not row:
            return None
        return str(row["username"])

    def save_user_preference(self, user_id: int, preference: Dict[str, object]) -> None:
        if not preference:
            return

        column_map = {
            "preferred_voice": "preferred_voice",
            "preferred_reasoning_model": "preferred_reasoning_model",
            "preferred_vision_model": "preferred_vision_model",
            "preferred_live2d_model": "preferred_live2d_model",
            "tts_enabled": "tts_enabled",
            "voice_input_enabled": "voice_input_enabled",
            "screen_capture_enabled": "screen_capture_enabled",
            "screen_preview_enabled": "screen_preview_enabled",
            "rag_enabled": "rag_enabled",
            "desktop_mate_enabled": "desktop_mate_enabled",
            "speaking_speed": "speaking_speed",
            "system_prompt_behavior": "system_prompt_behavior",
            "system_prompt_custom": "system_prompt_custom",
        }
        bool_keys = {
            "tts_enabled",
            "voice_input_enabled",
            "screen_capture_enabled",
            "screen_preview_enabled",
            "rag_enabled",
            "desktop_mate_enabled",
        }

        updates: List[str] = []
        values: List[object] = []

        for key, column in column_map.items():
            if key not in preference:
                continue

            raw_value = preference.get(key)
            if raw_value is None:
                continue

            if key in bool_keys:
                value: object = 1 if bool(raw_value) else 0
            elif key == "speaking_speed":
                try:
                    value = max(0.4, min(float(raw_value), 2.5))
                except (TypeError, ValueError):
                    continue
            else:
                value = str(raw_value).strip()

            updates.append(f"{column} = ?")
            values.append(value)

        if not updates:
            return

        updated_at = datetime.now().isoformat(timespec="seconds")

        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO user_preferences (user_id, updated_at) VALUES (?, ?)",
                (int(user_id), updated_at),
            )
            updates.append("updated_at = ?")
            values.append(updated_at)
            values.append(int(user_id))
            self.conn.execute(
                f"UPDATE user_preferences SET {', '.join(updates)} WHERE user_id = ?",
                tuple(values),
            )
            self.conn.commit()

    def get_user_preference(self, user_id: int) -> Dict[str, object]:
        defaults = {
            "preferred_voice": "system_default",
            "preferred_reasoning_model": str(CONFIG.get("ollama", {}).get("model", "qwen2.5-coder:7b")),
            "preferred_vision_model": str(CONFIG.get("vision", {}).get("vision_model", "qwen2.5vl:7b")),
            "preferred_live2d_model": str(CONFIG.get("paths", {}).get("default_live2d_model", "")),
            "tts_enabled": True,
            "voice_input_enabled": False,
            "screen_capture_enabled": bool(CONFIG.get("vision", {}).get("screen_share_enabled", False)),
            "screen_preview_enabled": False,
            "rag_enabled": True,
            "desktop_mate_enabled": False,
            "speaking_speed": float(CONFIG.get("voice", {}).get("speaking_speed", 1.0)),
            "system_prompt_behavior": "default",
            "system_prompt_custom": "",
        }

        with self._lock:
            row = self.conn.execute(
                """
                SELECT
                    preferred_voice,
                    preferred_reasoning_model,
                    preferred_vision_model,
                    preferred_live2d_model,
                    tts_enabled,
                    voice_input_enabled,
                    screen_capture_enabled,
                    screen_preview_enabled,
                    rag_enabled,
                    desktop_mate_enabled,
                    speaking_speed,
                    system_prompt_behavior,
                    system_prompt_custom
                FROM user_preferences
                WHERE user_id = ?
                """,
                (int(user_id),),
            ).fetchone()

        if not row:
            return defaults

        def _to_bool(value: object, fallback: bool) -> bool:
            if value is None:
                return fallback
            return bool(int(value))

        result = dict(defaults)
        result["preferred_voice"] = str(row["preferred_voice"] or defaults["preferred_voice"])
        result["preferred_reasoning_model"] = str(
            row["preferred_reasoning_model"] or defaults["preferred_reasoning_model"]
        )
        result["preferred_vision_model"] = str(
            row["preferred_vision_model"] or defaults["preferred_vision_model"]
        )
        result["preferred_live2d_model"] = str(
            row["preferred_live2d_model"] or defaults["preferred_live2d_model"]
        )
        result["tts_enabled"] = _to_bool(row["tts_enabled"], bool(defaults["tts_enabled"]))
        result["voice_input_enabled"] = _to_bool(
            row["voice_input_enabled"],
            bool(defaults["voice_input_enabled"]),
        )
        result["screen_capture_enabled"] = _to_bool(
            row["screen_capture_enabled"],
            bool(defaults["screen_capture_enabled"]),
        )
        result["screen_preview_enabled"] = _to_bool(
            row["screen_preview_enabled"],
            bool(defaults["screen_preview_enabled"]),
        )
        result["rag_enabled"] = _to_bool(row["rag_enabled"], bool(defaults["rag_enabled"]))
        result["desktop_mate_enabled"] = _to_bool(
            row["desktop_mate_enabled"],
            bool(defaults["desktop_mate_enabled"]),
        )

        try:
            result["speaking_speed"] = max(0.4, min(float(row["speaking_speed"]), 2.5))
        except (TypeError, ValueError):
            result["speaking_speed"] = defaults["speaking_speed"]

        result["system_prompt_behavior"] = str(
            row["system_prompt_behavior"] or defaults["system_prompt_behavior"]
        ).strip() or "default"
        result["system_prompt_custom"] = str(
            row["system_prompt_custom"] or defaults["system_prompt_custom"]
        )

        return result

    # --- RAD memory ---
    def add_rad_data(
        self,
        user_id: int,
        category: str,
        key_data: str,
        value_data: str,
        confidence_score: float = 1.0,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            if self._rad_has_user_id:
                self.conn.execute(
                    """
                    INSERT INTO rad_memory (user_id, category, key_data, value_data, confidence_score, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(user_id),
                        (category or "user_fact").strip() or "user_fact",
                        (key_data or "").strip(),
                        (value_data or "").strip(),
                        float(confidence_score),
                        now,
                    ),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO rad_memory (category, key_data, value_data, confidence_score, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (category or "user_fact").strip() or "user_fact",
                        (key_data or "").strip(),
                        (value_data or "").strip(),
                        float(confidence_score),
                        now,
                    ),
                )
            self.conn.commit()

    def add_rad_data_if_new(
        self,
        user_id: int,
        category: str,
        key_data: str,
        value_data: str,
        confidence_score: float = 1.0,
    ) -> bool:
        clean_category = (category or "user_fact").strip() or "user_fact"
        clean_key = (key_data or "").strip()
        clean_value = (value_data or "").strip()
        if not clean_key or not clean_value:
            return False

        with self._lock:
            if self._rad_has_user_id:
                row = self.conn.execute(
                    """
                    SELECT id FROM rad_memory
                    WHERE user_id = ?
                      AND lower(category) = lower(?)
                      AND lower(key_data) = lower(?)
                      AND lower(value_data) = lower(?)
                    LIMIT 1
                    """,
                    (int(user_id), clean_category, clean_key, clean_value),
                ).fetchone()
            else:
                row = self.conn.execute(
                    """
                    SELECT id FROM rad_memory
                    WHERE lower(category) = lower(?)
                      AND lower(key_data) = lower(?)
                      AND lower(value_data) = lower(?)
                    LIMIT 1
                    """,
                    (clean_category, clean_key, clean_value),
                ).fetchone()

            if row:
                return False

        self.add_rad_data(
            user_id=user_id,
            category=clean_category,
            key_data=clean_key,
            value_data=clean_value,
            confidence_score=confidence_score,
        )
        return True

    @staticmethod
    def _extract_important_facts(text: str) -> List[tuple[str, str, float]]:
        clean = (text or "").strip()
        if not clean:
            return []

        lowered = clean.lower()
        facts: List[tuple[str, str, float]] = []

        explicit_prefixes = ["remember that", "remember", "note that", "important", "store this"]
        for prefix in explicit_prefixes:
            if lowered.startswith(prefix):
                note = clean[len(prefix):].strip(" .,:;")
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
            match = re.search(pattern, clean, flags=re.IGNORECASE)
            if not match:
                continue
            value = match.group(1).strip(" .")
            if value:
                facts.append((key, value, confidence))

        favorite_match = re.search(
            r"\bmy favorite\s+([a-zA-Z\s]{2,24})\s+is\s+([a-zA-Z0-9\s\.,'-]{1,60})",
            clean,
            flags=re.IGNORECASE,
        )
        if favorite_match:
            topic = re.sub(r"\s+", "_", favorite_match.group(1).strip().lower())
            value = favorite_match.group(2).strip(" .")
            if topic and value:
                facts.append((f"favorite_{topic}", value, 0.91))

        return facts

    @staticmethod
    def _extract_assistant_points(assistant_text: str) -> List[tuple[str, str, float]]:
        clean = (assistant_text or "").strip()
        if not clean:
            return []

        points: List[tuple[str, str, float]] = []
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        for line in lines:
            collapsed = line.strip("-*• ")
            if ":" not in collapsed:
                continue

            key_part, value_part = collapsed.split(":", 1)
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

    def auto_store_important_conversation_data(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str = "",
    ) -> List[Dict[str, str]]:
        if user_id is None:
            return []

        candidates: List[tuple[str, str, float]] = []
        candidates.extend(self._extract_important_facts(user_text))
        candidates.extend(self._extract_assistant_points(assistant_text))

        stored: List[Dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for key, value, confidence in candidates:
            pair = (key, value)
            if pair in seen:
                continue
            seen.add(pair)

            if self.add_rad_data_if_new(
                user_id=int(user_id),
                category="auto_fact",
                key_data=key,
                value_data=value,
                confidence_score=confidence,
            ):
                stored.append({"key": key, "value": value})

        return stored

    def get_rad_data(self, user_id: int, limit: int = 300) -> List[Dict[str, object]]:
        with self._lock:
            if self._rad_has_user_id:
                rows = self.conn.execute(
                    """
                    SELECT id, category, key_data, value_data, confidence_score, created_at
                    FROM rad_memory
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(user_id), max(1, int(limit))),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """
                    SELECT id, category, key_data, value_data, confidence_score, created_at
                    FROM rad_memory
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (max(1, int(limit)),),
                ).fetchall()

        return [
            {
                "id": int(row["id"]),
                "category": str(row["category"]),
                "key_data": str(row["key_data"]),
                "value_data": str(row["value_data"]),
                "confidence_score": float(row["confidence_score"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def delete_rad_data(self, user_id: int, rad_id: int) -> bool:
        with self._lock:
            if self._rad_has_user_id:
                cursor = self.conn.execute(
                    "DELETE FROM rad_memory WHERE id = ? AND user_id = ?",
                    (int(rad_id), int(user_id)),
                )
            else:
                cursor = self.conn.execute(
                    "DELETE FROM rad_memory WHERE id = ?",
                    (int(rad_id),),
                )
            self.conn.commit()
            return cursor.rowcount > 0
