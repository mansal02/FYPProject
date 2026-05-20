"""
Vision and audio utilities stub for offline desktop assistant.

This module is kept minimal for offline-only operation.
Camera tracking, speech recognition, and TTS are disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class CameraState:
    """Placeholder camera state."""
    emotion: str = "unavailable"
    finger_x: Optional[int] = None
    finger_y: Optional[int] = None
    tracking_ok: bool = False


class CameraTracker:
    """Stub: camera tracking is disabled in offline mode."""

    def __init__(self, camera_index: int = 0) -> None:
        self.camera_index = camera_index

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def set_mouse_control(self, enabled: bool) -> None:
        pass

    def set_emotion_detection(self, enabled: bool) -> None:
        pass

    def get_state(self) -> CameraState:
        return CameraState(tracking_ok=False, emotion="unavailable")


class SpeechListener:
    """Stub: voice recognition is disabled in offline mode."""

    def __init__(self, energy_threshold: int = 650, pause_threshold: float = 0.8) -> None:
        self.available = False
        self.last_error = "Voice recognition disabled in offline mode."

    def get_status(self) -> Dict[str, object]:
        return {
            "available": False,
            "offline_backend": "none",
            "last_error": self.last_error,
        }

    def listen_once(
        self,
        timeout: float = 2.0,
        phrase_time_limit: float = 6.0,
        wake_words: Optional[list[str]] = None,
        allow_online_fallback: bool = False,
        allow_commands_without_wake: bool = False,
    ) -> Optional[str]:
        return None

    def start_background_listening(
        self,
        callback,
        wake_words: Optional[list[str]] = None,
        allow_online_fallback: bool = False,
        allow_commands_without_wake: bool = False,
    ) -> None:
        pass

    def stop_background_listening(self) -> None:
        pass

    def start_wake_word_listener(
        self,
        callback,
        wake_phrase: str = "hey agent",
        access_key: Optional[str] = None,
        keyword_path: Optional[str] = None,
    ) -> bool:
        return False

    def stop_wake_word_listener(self) -> None:
        pass


class TextToSpeechEngine:
    """Stub: text-to-speech is disabled in offline mode."""

    def __init__(
        self,
        mode: str = "auto",
        piper_exe: Optional[str] = None,
        piper_model_path: Optional[str] = None,
        speaking_speed: float = 1.0,
    ) -> None:
        self.speaking_speed = float(speaking_speed)

    @staticmethod
    def list_system_voices() -> list[Dict[str, str]]:
        return []

    def is_available(self) -> bool:
        return False

    def get_active_mode(self) -> str:
        return "silent"

    def set_mode(
        self,
        mode: str,
        piper_exe: Optional[str] = None,
        piper_model_path: Optional[str] = None,
    ) -> None:
        pass

    def set_pyttsx3_voice(self, voice_id: str) -> None:
        pass

    def set_speaking_speed(self, speaking_speed: float) -> None:
        self.speaking_speed = float(speaking_speed)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def interrupt(self) -> None:
        pass

    def speak(self, text: str) -> None:
        pass
