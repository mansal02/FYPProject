"""
Vision and audio utilities for an offline desktop assistant.

Design notes:
- Camera tracking is intentionally CPU-only to preserve GPU VRAM for Ollama models.
- Speech listening uses bounded timeouts to avoid hanging loops.
- TTS text is sanitized with regex before speaking.
"""

from __future__ import annotations

import base64
import os
import queue
import re
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

try:
    import cv2
except Exception:  # pragma: no cover - optional dependency
    cv2 = None

try:
    import mediapipe as mp
except Exception:  # pragma: no cover - optional dependency
    mp = None

try:
    import numpy as np
except Exception:  # pragma: no cover - optional dependency
    np = None

try:
    from PIL import ImageGrab
except Exception:  # pragma: no cover - optional dependency
    ImageGrab = None

try:
    import pyautogui
except Exception:  # pragma: no cover - optional dependency
    pyautogui = None

try:
    import pyttsx3
except Exception:  # pragma: no cover - optional dependency
    pyttsx3 = None

try:
    import winsound
except Exception:  # pragma: no cover - optional dependency
    winsound = None

try:
    import speech_recognition as sr
except Exception:  # pragma: no cover - optional dependency
    sr = None

try:
    import pocketsphinx  # noqa: F401
    POCKETSPHINX_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    pocketsphinx = None
    POCKETSPHINX_AVAILABLE = False

try:
    import pvporcupine
except Exception:  # pragma: no cover - optional dependency
    pvporcupine = None

try:
    import pyaudio
except Exception:  # pragma: no cover - optional dependency
    pyaudio = None


def _clean_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def filter_tts_text(text: str) -> str:
    """
    Removes stage-direction style snippets before TTS.

    Example:
    "Hello *smiles* there" -> "Hello there"
    """
    if not text:
        return ""

    # Remove content wrapped in one or more asterisks.
    text = re.sub(r"\*[^*]*\*", " ", text)
    # Remove stray markdown emphasis stars if present.
    text = text.replace("*", " ")
    return _clean_whitespace(text)


def capture_screen_base64_jpeg(quality: int = 65) -> Optional[str]:
    """
    Captures current desktop and returns base64 JPEG for Ollama vision prompts.

    Keeping JPEG quality modest reduces payload and CPU/memory overhead.
    """
    if ImageGrab is None:
        return None

    try:
        from io import BytesIO

        image = ImageGrab.grab(all_screens=True)
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=max(20, min(95, quality)), optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception:
        return None


@dataclass
class CameraState:
    emotion: str = "unknown"
    finger_x: Optional[int] = None
    finger_y: Optional[int] = None
    tracking_ok: bool = False


class CameraTracker:
    """
    Background CPU thread for MediaPipe face + hand tracking.

    MediaPipe Python runs on CPU by default in this setup, which avoids VRAM
    pressure while LLM/Vision model inference is active on GPU.
    """

    def __init__(self, camera_index: int = 0) -> None:
        self.camera_index = camera_index
        self.enable_mouse_control = False
        self.enable_emotion_detection = True

        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state = CameraState()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def set_mouse_control(self, enabled: bool) -> None:
        self.enable_mouse_control = bool(enabled)

    def set_emotion_detection(self, enabled: bool) -> None:
        self.enable_emotion_detection = bool(enabled)

    def get_state(self) -> CameraState:
        with self._lock:
            return CameraState(
                emotion=self._state.emotion,
                finger_x=self._state.finger_x,
                finger_y=self._state.finger_y,
                tracking_ok=self._state.tracking_ok,
            )

    def _set_state(self, **kwargs: object) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self._state, key, value)

    def _loop(self) -> None:
        if cv2 is None or mp is None or np is None:
            self._set_state(tracking_ok=False, emotion="unavailable")
            return

        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            self._set_state(tracking_ok=False, emotion="camera_unavailable")
            return

        # Explicitly instantiate CPU MediaPipe solutions.
        face_mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=False,
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        hands = mp.solutions.hands.Hands(
            static_image_mode=False,
            max_num_hands=1,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )

        self._set_state(tracking_ok=True)

        try:
            while self._running.is_set():
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue

                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w = frame.shape[:2]

                if self.enable_emotion_detection:
                    emotion = self._detect_emotion(face_mesh, frame_rgb, w, h)
                    self._set_state(emotion=emotion)

                if self.enable_mouse_control:
                    self._track_finger_and_move_mouse(hands, frame_rgb, w, h)

                # Keep CPU usage moderate; camera tracking does not need full FPS.
                time.sleep(0.02)
        finally:
            cap.release()
            face_mesh.close()
            hands.close()
            self._set_state(tracking_ok=False)

    def _detect_emotion(self, face_mesh: object, frame_rgb: object, width: int, height: int) -> str:
        """
        Very lightweight heuristic:
        - Smile if mouth appears wide relative to mouth opening.
        - Frown if very narrow and compressed.
        - Neutral otherwise.
        """
        try:
            result = face_mesh.process(frame_rgb)
            if not result.multi_face_landmarks:
                return "unknown"

            landmarks = result.multi_face_landmarks[0].landmark

            # Mouth landmarks in MediaPipe FaceMesh index space.
            left = landmarks[61]
            right = landmarks[291]
            upper = landmarks[13]
            lower = landmarks[14]

            mouth_width = abs((right.x - left.x) * width)
            mouth_open = abs((lower.y - upper.y) * height) + 1e-6
            ratio = mouth_width / mouth_open

            if ratio > 6.0:
                return "smile"
            if ratio < 3.8:
                return "frown"
            return "neutral"
        except Exception:
            return "unknown"

    def _track_finger_and_move_mouse(self, hands: object, frame_rgb: object, width: int, height: int) -> None:
        if pyautogui is None:
            self._set_state(finger_x=None, finger_y=None)
            return

        try:
            result = hands.process(frame_rgb)
            if not result.multi_hand_landmarks:
                self._set_state(finger_x=None, finger_y=None)
                return

            hand_landmarks = result.multi_hand_landmarks[0]
            idx_tip = hand_landmarks.landmark[8]

            frame_x = int(idx_tip.x * width)
            frame_y = int(idx_tip.y * height)

            screen_w, screen_h = pyautogui.size()
            target_x = max(0, min(screen_w - 1, int(idx_tip.x * screen_w)))
            target_y = max(0, min(screen_h - 1, int(idx_tip.y * screen_h)))

            pyautogui.moveTo(target_x, target_y, duration=0.03)
            self._set_state(finger_x=frame_x, finger_y=frame_y)
        except Exception:
            self._set_state(finger_x=None, finger_y=None)


class SpeechListener:
    """Reliable microphone listener with timeout-guarded loops."""

    def __init__(self, energy_threshold: int = 300, pause_threshold: float = 0.8) -> None:
        self.available = sr is not None
        self.recognizer = sr.Recognizer() if self.available else None
        self.microphone = None
        self.last_error = ""
        self.offline_backend = "sphinx" if POCKETSPHINX_AVAILABLE else "none"

        if self.available:
            try:
                self.microphone = sr.Microphone()
            except Exception as exc:
                self.available = False
                self.recognizer = None
                self.microphone = None
                self.last_error = f"Microphone initialization failed: {exc}"

        if self.recognizer is not None:
            self.recognizer.energy_threshold = energy_threshold
            self.recognizer.pause_threshold = pause_threshold
            self.recognizer.dynamic_energy_threshold = True

        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._wake_running = threading.Event()
        self._wake_thread: Optional[threading.Thread] = None

    def get_status(self) -> Dict[str, object]:
        return {
            "available": bool(self.available and self.recognizer is not None and self.microphone is not None),
            "offline_backend": self.offline_backend,
            "last_error": self.last_error,
        }

    def listen_once(
        self,
        timeout: float = 2.0,
        phrase_time_limit: float = 6.0,
        wake_words: Optional[list[str]] = None,
        allow_online_fallback: bool = False,
    ) -> Optional[str]:
        if not self.available or self.recognizer is None or self.microphone is None:
            return None

        try:
            with self.microphone as source:
                # Short ambient adjustment each call keeps behavior stable in noisy rooms.
                self.recognizer.adjust_for_ambient_noise(source, duration=0.2)
                audio = self.recognizer.listen(
                    source,
                    timeout=max(0.5, timeout),
                    phrase_time_limit=max(1.0, phrase_time_limit),
                )
        except sr.WaitTimeoutError:
            return None
        except Exception:
            return None

        transcript = self._transcribe(audio, allow_online_fallback=allow_online_fallback)
        if not transcript:
            return None

        transcript = transcript.strip().lower()
        if wake_words:
            normalized = [w.strip().lower() for w in wake_words if w and w.strip()]
            if normalized and not any(w in transcript for w in normalized):
                return None

        return transcript

    def start_background_listening(
        self,
        callback: Callable[[str], None],
        wake_words: Optional[list[str]] = None,
        allow_online_fallback: bool = False,
    ) -> None:
        if not self.available:
            return
        if self._thread and self._thread.is_alive():
            return

        self._running.set()
        self._thread = threading.Thread(
            target=self._listen_loop,
            args=(callback, wake_words, allow_online_fallback),
            daemon=True,
        )
        self._thread.start()

    def stop_background_listening(self) -> None:
        self._running.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def start_wake_word_listener(
        self,
        callback: Callable[[], None],
        wake_phrase: str = "hey agent",
        access_key: Optional[str] = None,
        keyword_path: Optional[str] = None,
    ) -> bool:
        if pvporcupine is None or pyaudio is None:
            return False

        if self._wake_thread and self._wake_thread.is_alive():
            return True

        resolved_access_key = (
            (access_key or "").strip()
            or os.environ.get("PORCUPINE_ACCESS_KEY", "").strip()
            or os.environ.get("MARIE_PORCUPINE_ACCESS_KEY", "").strip()
        )
        if not resolved_access_key:
            return False

        resolved_keyword_path = (keyword_path or "").strip() or os.environ.get(
            "MARIE_PORCUPINE_KEYWORD_PATH",
            "",
        ).strip()
        if not resolved_keyword_path:
            candidate = Path.cwd() / "aiassistant" / "infra" / "voice" / f"{wake_phrase.strip().lower().replace(' ', '_')}.ppn"
            if candidate.exists():
                resolved_keyword_path = str(candidate.resolve())

        self._wake_running.set()
        self._wake_thread = threading.Thread(
            target=self._wake_word_loop,
            args=(callback, wake_phrase, resolved_access_key, resolved_keyword_path),
            daemon=True,
        )
        self._wake_thread.start()
        return True

    def stop_wake_word_listener(self) -> None:
        self._wake_running.clear()
        if self._wake_thread and self._wake_thread.is_alive():
            self._wake_thread.join(timeout=2.0)

    def _wake_word_loop(
        self,
        callback: Callable[[], None],
        wake_phrase: str,
        access_key: str,
        keyword_path: str,
    ) -> None:
        porcupine_engine = None
        audio_interface = None
        audio_stream = None

        try:
            use_keyword_path = bool(keyword_path) and Path(keyword_path).exists()
            if use_keyword_path:
                porcupine_engine = pvporcupine.create(
                    access_key=access_key,
                    keyword_paths=[keyword_path],
                )
            else:
                # Built-in fallback keeps the detector non-blocking when a custom
                # Hey Agent keyword model is not installed yet.
                porcupine_engine = pvporcupine.create(
                    access_key=access_key,
                    keywords=["porcupine"],
                )

            audio_interface = pyaudio.PyAudio()
            audio_stream = audio_interface.open(
                rate=porcupine_engine.sample_rate,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=porcupine_engine.frame_length,
            )

            while self._wake_running.is_set():
                raw_pcm = audio_stream.read(
                    porcupine_engine.frame_length,
                    exception_on_overflow=False,
                )
                pcm = struct.unpack_from("h" * porcupine_engine.frame_length, raw_pcm)
                keyword_index = porcupine_engine.process(pcm)
                if keyword_index >= 0:
                    try:
                        callback()
                    except Exception:
                        pass
                    # Small debounce window avoids immediate repeated triggers.
                    time.sleep(0.2)
        except Exception:
            return
        finally:
            try:
                if audio_stream is not None:
                    audio_stream.stop_stream()
                    audio_stream.close()
            except Exception:
                pass
            try:
                if audio_interface is not None:
                    audio_interface.terminate()
            except Exception:
                pass
            try:
                if porcupine_engine is not None:
                    porcupine_engine.delete()
            except Exception:
                pass

    def _listen_loop(
        self,
        callback: Callable[[str], None],
        wake_words: Optional[list[str]],
        allow_online_fallback: bool,
    ) -> None:
        while self._running.is_set():
            text = self.listen_once(
                timeout=2.0,
                phrase_time_limit=6.0,
                wake_words=wake_words,
                allow_online_fallback=allow_online_fallback,
            )
            if text:
                try:
                    callback(text)
                except Exception:
                    pass
            time.sleep(0.05)

    def _transcribe(self, audio: object, allow_online_fallback: bool = False) -> Optional[str]:
        if self.recognizer is None or sr is None:
            return None

        # Offline-first path.
        if POCKETSPHINX_AVAILABLE:
            try:
                return self.recognizer.recognize_sphinx(audio)
            except Exception:
                pass

        # If Sphinx is unavailable, we must use an alternate backend to avoid silent mic failure.
        use_online_fallback = bool(allow_online_fallback) or not POCKETSPHINX_AVAILABLE
        if not use_online_fallback:
            return None

        # Optional fallback for environments where offline recognizer is unavailable.
        try:
            return self.recognizer.recognize_google(audio)
        except Exception:
            return None


class TextToSpeechEngine:
    """
    Queue-driven TTS wrapper.

    Supports:
    - pyttsx3 (default, local)
    - Piper CLI (optional) if piper executable + voice model are configured.
    """

    def __init__(
        self,
        mode: str = "auto",
        piper_exe: Optional[str] = None,
        piper_model_path: Optional[str] = None,
        speaking_speed: float = 1.0,
    ) -> None:
        self.mode = mode.strip().lower()
        self.piper_exe = piper_exe
        self.piper_model_path = piper_model_path
        self.speaking_speed = max(0.4, min(float(speaking_speed), 2.5))

        self._queue: "queue.Queue[str]" = queue.Queue()
        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._engine_lock = threading.RLock()

        self._engine = None
        self._last_error = ""
        self._active_mode = "silent"
        self._init_engine()

    def _init_engine(self) -> None:
        self._engine = None
        self._last_error = ""

        preferred_mode = self.mode
        if preferred_mode == "auto":
            preferred_mode = "pyttsx3"

        if preferred_mode == "pyttsx3" and pyttsx3 is not None:
            try:
                self._engine = pyttsx3.init()
                self._active_mode = "pyttsx3"
                self.set_speaking_speed(self.speaking_speed)
                return
            except Exception:
                self._engine = None
                self._last_error = "pyttsx3 initialization failed"

        if self._can_use_piper():
            self._active_mode = "piper"
            return

        self._active_mode = "silent"
        if not self._last_error:
            self._last_error = "No available TTS backend"

    @staticmethod
    def list_system_voices() -> List[Dict[str, str]]:
        options: List[Dict[str, str]] = []
        if pyttsx3 is None:
            return options

        engine = None
        try:
            engine = pyttsx3.init()
            for voice in engine.getProperty("voices") or []:
                voice_id = str(getattr(voice, "id", "") or "").strip()
                if not voice_id:
                    continue
                voice_name = str(getattr(voice, "name", "") or voice_id).strip()
                options.append({"id": voice_id, "name": voice_name})
        except Exception:
            return []
        finally:
            if engine is not None:
                try:
                    engine.stop()
                except Exception:
                    pass

        return options

    def is_available(self) -> bool:
        return self._active_mode in {"pyttsx3", "piper"}

    def get_active_mode(self) -> str:
        return self._active_mode

    def set_mode(
        self,
        mode: str,
        piper_exe: Optional[str] = None,
        piper_model_path: Optional[str] = None,
    ) -> None:
        self.mode = (mode or "auto").strip().lower()
        if piper_exe is not None:
            self.piper_exe = piper_exe
        if piper_model_path is not None:
            self.piper_model_path = piper_model_path
        self._init_engine()

    def set_pyttsx3_voice(self, voice_id: str) -> None:
        clean_voice = (voice_id or "").strip()
        if not clean_voice or self._engine is None:
            return

        with self._engine_lock:
            try:
                self._engine.setProperty("voice", clean_voice)
            except Exception:
                pass

    def set_speaking_speed(self, speaking_speed: float) -> None:
        self.speaking_speed = max(0.4, min(float(speaking_speed), 2.5))
        if self._engine is None:
            return

        # pyttsx3 rate baseline differs by platform; 185 is a practical midpoint.
        target_rate = int(185 * self.speaking_speed)
        with self._engine_lock:
            try:
                self._engine.setProperty("rate", target_rate)
            except Exception:
                pass

    def _can_use_piper(self) -> bool:
        if not self.piper_exe or not self.piper_model_path:
            return False
        return Path(self.piper_exe).exists() and Path(self.piper_model_path).exists()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        self._drain_queue()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        if self._engine is not None:
            with self._engine_lock:
                try:
                    self._engine.stop()
                except Exception:
                    pass

    def interrupt(self) -> None:
        """Stops current playback and drops queued sentences without shutting down the TTS worker."""
        self._drain_queue()

        if self._engine is not None:
            with self._engine_lock:
                try:
                    self._engine.stop()
                except Exception:
                    pass

        if winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass

    def _drain_queue(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            return

    def speak(self, text: str) -> None:
        clean = filter_tts_text(text)
        if clean:
            self._queue.put(clean)

    def _loop(self) -> None:
        while self._running.is_set():
            try:
                text = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if self._active_mode == "piper":
                self._speak_with_piper(text)
            elif self._active_mode == "pyttsx3":
                self._speak_with_pyttsx3(text)
            self._queue.task_done()

    def _speak_with_pyttsx3(self, text: str) -> None:
        if self._engine is None:
            if self._can_use_piper():
                self._active_mode = "piper"
                self._speak_with_piper(text)
            return
        try:
            with self._engine_lock:
                self._engine.say(text)
                self._engine.runAndWait()
        except Exception:
            pass

    def _speak_with_piper(self, text: str) -> None:
        if not self.piper_exe or not self.piper_model_path:
            return

        exe = Path(self.piper_exe)
        model = Path(self.piper_model_path)
        if not exe.exists() or not model.exists():
            return

        temp_path = Path(tempfile.gettempdir()) / f"marie_tts_{time.time_ns()}.wav"
        length_scale = max(0.35, min(2.8, 1.0 / self.speaking_speed))

        try:
            subprocess.run(
                [
                    str(exe),
                    "--model",
                    str(model),
                    "--output_file",
                    str(temp_path),
                    "--length_scale",
                    str(length_scale),
                ],
                input=text.encode("utf-8", errors="ignore"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if not temp_path.exists():
                return

            if winsound is not None:
                winsound.PlaySound(str(temp_path), winsound.SND_FILENAME)
        except Exception:
            pass
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
