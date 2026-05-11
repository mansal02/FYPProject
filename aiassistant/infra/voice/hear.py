import os
import re
import time
import math
import speech_recognition as sr
from difflib import get_close_matches
from collections import deque
from PyQt5.QtCore import QThread, pyqtSignal
from aiassistant.infra.config.app_config import CONFIG

# Keep startup responsive even if spaCy (and its optional torch deps) are slow.
_SPACY_NLP = None
_SPACY_READY = False
_SIMPLE_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "had", "has", "have", "he", "her", "him", "i", "if", "in", "into", "is",
    "it", "its", "me", "my", "not", "of", "on", "or", "our", "she", "so",
    "that", "the", "their", "them", "there", "they", "this", "to", "us", "was",
    "we", "were", "what", "when", "where", "who", "why", "with", "you", "your",
}


def _get_optional_spacy_nlp():
    global _SPACY_NLP, _SPACY_READY
    if _SPACY_READY:
        return _SPACY_NLP

    _SPACY_READY = True
    disable_spacy = os.getenv("MARIE_DISABLE_SPACY", "1").strip().lower()
    if disable_spacy in {"1", "true", "yes", "on"}:
        return None

    try:
        import spacy

        try:
            _SPACY_NLP = spacy.load("en_core_web_sm")
        except Exception:
            _SPACY_NLP = spacy.blank("en")
    except BaseException as e:
        _SPACY_NLP = None
        print(f"[Voice] spaCy disabled: {e}")

    return _SPACY_NLP


def _extract_keywords(text):
    nlp_model = _get_optional_spacy_nlp()
    if nlp_model is not None:
        try:
            doc = nlp_model(text)
            return {
                token.text.lower()
                for token in doc
                if token.pos_ in {"NOUN", "PROPN"} and token.is_alpha and len(token.text) > 2
            }
        except Exception:
            pass

    words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    return {word for word in words if word not in _SIMPLE_STOPWORDS}

# Check for GPU
try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WhisperModel = None
    WHISPER_AVAILABLE = False

def _resolve_whisper_device():
    device = str(CONFIG.get("voice", {}).get("whisper_device", "cpu")).strip().lower()
    if device not in {"cpu", "cuda"}:
        return "cpu"
    return device


DEVICE = _resolve_whisper_device()
ALLOWED_WHISPER_MODEL_SIZES = {"base", "small"}
DEFAULT_WHISPER_MODEL_SIZE = "base"
DEFAULT_ENERGY_THRESHOLD = 650
MIN_ENERGY_THRESHOLD = 600
DEFAULT_BEAM_SIZE = 5
DEFAULT_COMMAND_HINT = "open close launch start run search find locate files software service open website volume mute unmute"
VOICE_COMMAND_KEYWORDS = [
    "open",
    "close",
    "launch",
    "start",
    "run",
    "search",
    "find",
    "locate",
    "browse",
    "play",
    "pause",
    "stop",
    "volume",
    "mute",
    "unmute",
    "software",
    "service",
    "files",
    "email",
    "telegram",
    "whatsapp",
]
VOICE_COMMAND_LEAD_CORRECTIONS = {
    "oven": "open",
    "openn": "open",
    "oppen": "open",
    "opan": "open",
    "cloze": "close",
    "clos": "close",
    "clothes": "close",
    "lunch": "launch",
    "serch": "search",
    "seach": "search",
    "mutee": "mute",
    "unmutee": "unmute",
}


def _normalize_transcribed_command(text):
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return ""

    parts = compact.split(" ")
    lead = re.sub(r"[^a-z0-9]", "", parts[0].lower())
    if not lead:
        return compact

    corrected = VOICE_COMMAND_LEAD_CORRECTIONS.get(lead, lead)
    if corrected not in VOICE_COMMAND_KEYWORDS:
        match = get_close_matches(corrected, VOICE_COMMAND_KEYWORDS, n=1, cutoff=0.87)
        if match:
            corrected = match[0]

    if corrected != lead:
        parts[0] = corrected
        return " ".join(parts).strip()

    return compact


def _looks_like_voice_command(text):
    normalized = _normalize_transcribed_command(text).lower()
    if not normalized:
        return False

    lead = normalized.split(" ", 1)[0]
    if lead in VOICE_COMMAND_KEYWORDS:
        return True

    return bool(
        re.search(
            r"\b(?:open|close|launch|start|run|search|find|locate|browse|play|pause|stop|volume|mute|unmute|files|software|service)\b",
            normalized,
        )
    )

load_silero_vad = None
read_audio = None
get_speech_timestamps = None

np = None
OpenWakeWordModel = None


def _load_silero_tools():
    global load_silero_vad, read_audio, get_speech_timestamps
    if load_silero_vad and read_audio and get_speech_timestamps:
        return True

    try:
        from silero_vad import load_silero_vad as _load
        from silero_vad import read_audio as _read_audio
        from silero_vad import get_speech_timestamps as _timestamps

        load_silero_vad = _load
        read_audio = _read_audio
        get_speech_timestamps = _timestamps
        return True
    except BaseException as e:
        print(f"[Voice] Silero VAD unavailable: {e}")
        return False


def _load_openwakeword_tools():
    global np, OpenWakeWordModel
    if np is not None and OpenWakeWordModel is not None:
        return True

    try:
        import numpy as _np
        from openwakeword.model import Model as _OpenWakeWordModel

        np = _np
        OpenWakeWordModel = _OpenWakeWordModel
        return True
    except BaseException as e:
        print(f"[Voice] OpenWakeWord unavailable: {e}")
        return False

COMPUTE_TYPE = "float16" if DEVICE == "cuda" else "int8"

class ContextBrain:
    def __init__(self, max_history=3):
        self.history = deque(maxlen=max_history)
        self.keywords = set()

    def update(self, text):
        new_keywords = _extract_keywords(text)
        self.keywords.update(new_keywords)
        self.history.append(text)
        if len(self.keywords) > 15:
            self.keywords = set(list(self.keywords)[-15:])

    def get_prompt(self):
        if not self.keywords: return "General conversation."
        return f"Context: {', '.join(self.keywords)}."


class VoiceWorker(QThread):
    text_received = pyqtSignal(str)     
    status_update = pyqtSignal(str)     
    speech_detected = pyqtSignal()
    clarification_needed = pyqtSignal(str, float)

    def __init__(self, model_size="base", wake_word="hey"):
        super().__init__()
        voice_cfg = CONFIG.get("voice", {})
        resolved_wake = str(wake_word or voice_cfg.get("wake_word", "hey")).strip().lower()
        self.wake_word = resolved_wake or "hey"
        self.wake_aliases = self._build_wake_aliases(self.wake_word)
        self.always_listen_wake_word_only = bool(voice_cfg.get("always_listen_wake_word_only", True))
        self.is_active = False
        self.keyword_mode = self.always_listen_wake_word_only
        self.running = True
        self.auto_paused = False
        configured_model_size = str(voice_cfg.get("whisper_model_size", model_size)).strip().lower()
        self.model_size = (
            configured_model_size
            if configured_model_size in ALLOWED_WHISPER_MODEL_SIZES
            else DEFAULT_WHISPER_MODEL_SIZE
        )
        raw_energy_threshold = voice_cfg.get("energy_threshold", DEFAULT_ENERGY_THRESHOLD)
        try:
            configured_energy_threshold = int(float(raw_energy_threshold))
        except (TypeError, ValueError):
            configured_energy_threshold = DEFAULT_ENERGY_THRESHOLD
        self.energy_threshold = max(MIN_ENERGY_THRESHOLD, configured_energy_threshold)
        # Keep Whisper beam size fixed for predictable latency and quality.
        self.beam_size = DEFAULT_BEAM_SIZE
        self.whisper_device = DEVICE
        self.compute_type = "float16" if self.whisper_device == "cuda" else "int8"
        self.enable_faster_whisper = bool(voice_cfg.get("enable_faster_whisper", False))
        self.enable_silero_vad = bool(voice_cfg.get("enable_silero_vad", True))
        self.enable_openwakeword = bool(voice_cfg.get("enable_openwakeword", False))
        self.allow_online_fallback = bool(voice_cfg.get("allow_online_fallback", True))
        self.allow_commands_without_wake_word = bool(voice_cfg.get("allow_commands_without_wake_word", True))
        self.transcription_confidence_threshold = float(
            voice_cfg.get("transcription_confidence_threshold", 0.60)
        )
        self.command_hint = str(voice_cfg.get("command_hint", DEFAULT_COMMAND_HINT)).strip()
        self.wakeword_threshold = float(voice_cfg.get("wakeword_threshold", 0.35))
        self._sphinx_keyword_entries = self._build_sphinx_keyword_entries()

        self._silero_model = None
        if self.enable_silero_vad:
            if _load_silero_tools():
                try:
                    self._silero_model = load_silero_vad()
                except Exception as e:
                    self.enable_silero_vad = False
                    print(f"[Voice] Silero VAD disabled: {e}")
            else:
                self.enable_silero_vad = False

        self._wakeword_model = None
        if self.enable_openwakeword:
            if _load_openwakeword_tools():
                try:
                    self._wakeword_model = OpenWakeWordModel()
                except Exception as e:
                    self.enable_openwakeword = False
                    print(f"[Voice] OpenWakeWord disabled: {e}")
            else:
                self.enable_openwakeword = False

    @staticmethod
    def _build_wake_aliases(wake_word):
        aliases = []
        normalized = re.sub(r"\s+", " ", str(wake_word or "").strip().lower())
        if normalized:
            aliases.append(normalized)
            first_token = normalized.split(" ", 1)[0]
            if first_token and first_token not in aliases:
                aliases.append(first_token)
        if "hey" not in aliases:
            aliases.append("hey")
        return aliases

    def _match_wake_alias(self, text):
        for alias in self.wake_aliases:
            if re.search(rf"\b{re.escape(alias)}\b", text):
                return alias
        return None

    def _strip_wake_prefix(self, text):
        stripped_text = text.strip()
        for alias in sorted(self.wake_aliases, key=len, reverse=True):
            candidate = re.sub(
                rf"^\s*{re.escape(alias)}(?:\b|[\s,.:;!?-])*\s*",
                "",
                stripped_text,
                flags=re.IGNORECASE,
            ).strip()
            if candidate != stripped_text:
                return candidate
        return stripped_text

    def _build_sphinx_keyword_entries(self):
        entries = []
        for phrase in sorted(set(self.wake_aliases + VOICE_COMMAND_KEYWORDS)):
            if phrase:
                entries.append((phrase, 1e-20))
        return entries

    def _normalize_for_emit(self, text):
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if not compact:
            return ""
        if _looks_like_voice_command(compact):
            return _normalize_transcribed_command(compact)
        return compact

    def _transcribe_with_speech_recognition(self, recognizer, audio):
        text = ""

        if self.allow_online_fallback:
            try:
                text = recognizer.recognize_google(audio).strip()
            except Exception:
                text = ""

        if not text:
            try:
                if self._sphinx_keyword_entries:
                    text = recognizer.recognize_sphinx(
                        audio,
                        keyword_entries=self._sphinx_keyword_entries,
                    ).strip()
                else:
                    text = recognizer.recognize_sphinx(audio).strip()
            except TypeError:
                try:
                    text = recognizer.recognize_sphinx(audio).strip()
                except Exception:
                    text = ""
            except Exception:
                text = ""

        if not text and not self.allow_online_fallback:
            try:
                text = recognizer.recognize_google(audio).strip()
            except Exception:
                text = ""

        return text

    def _has_speech_silero(self, wav_path):
        if not self.enable_silero_vad or not self._silero_model:
            return True
        try:
            wav = read_audio(wav_path, sampling_rate=16000)
            stamps = get_speech_timestamps(wav, self._silero_model, sampling_rate=16000)
            return bool(stamps)
        except Exception:
            return True

    def _wakeword_score(self, audio):
        if not self.enable_openwakeword or not self._wakeword_model or np is None:
            return 0.0
        try:
            samples = np.frombuffer(audio.get_raw_data(), dtype=np.int16).astype(np.float32) / 32768.0
            prediction = self._wakeword_model.predict(samples)
            best = 0.0
            if isinstance(prediction, dict):
                for value in prediction.values():
                    if isinstance(value, dict):
                        for nested in value.values():
                            try:
                                best = max(best, float(nested))
                            except Exception:
                                continue
                    else:
                        try:
                            best = max(best, float(value))
                        except Exception:
                            continue
            return best
        except Exception:
            return 0.0

    @staticmethod
    def _estimate_whisper_confidence(segments):
        scores = []
        for seg in segments or []:
            avg_logprob = getattr(seg, "avg_logprob", None)
            if avg_logprob is None:
                continue
            try:
                scores.append(float(avg_logprob))
            except Exception:
                continue

        if not scores:
            return None

        avg_logprob = sum(scores) / float(len(scores))
        return max(0.0, min(1.0, math.exp(avg_logprob)))
        
    def run(self):
        model = None
        if self.enable_faster_whisper:
            if not WHISPER_AVAILABLE:
                self.status_update.emit("Whisper unavailable; using speech fallback")
                print("[Voice] faster-whisper missing. Falling back to speech_recognition backend.")
            else:
                try:
                    self.status_update.emit(f"Loading Whisper ({self.whisper_device})...")
                    model = WhisperModel(self.model_size, device=self.whisper_device, compute_type=self.compute_type)
                except Exception as e:
                    model = None
                    self.status_update.emit("Whisper failed; using speech fallback")
                    print(f"[Voice] Whisper init failed, fallback enabled: {e}")
        else:
            self.status_update.emit("Speech fallback mode")

        brain = ContextBrain()
        
        r = sr.Recognizer()
        r.pause_threshold = 0.8
        r.dynamic_energy_threshold = False
        r.energy_threshold = self.energy_threshold
        r.dynamic_energy_adjustment_damping = 0.15
        r.non_speaking_duration = 0.25
        r.phrase_threshold = 0.2

        mic = None
        mic_error_reported = False
        last_mic_retry = 0.0
        try:
            mic = sr.Microphone()
        except Exception as e:
            print(f"[Voice] Microphone unavailable on startup: {e}")

        last_noise_calibration = 0.0
        was_auto_paused = False

        if self.keyword_mode:
            self.status_update.emit(f"Voice Ready (Wake: {self.wake_aliases[0]})")
        else:
            self.status_update.emit("Voice Ready (Press F4 to Toggle)")

        while self.running:
            if self.auto_paused:
                if not was_auto_paused:
                    self.status_update.emit("Mic Auto-paused (processing reply)")
                    was_auto_paused = True
                time.sleep(0.15)
                continue
            elif was_auto_paused:
                was_auto_paused = False

            # 1. CHECK IF ACTIVE
            # We listen if: 
            #   a) The user toggled it ON (is_active) 
            #   OR 
            #   b) We are in 'Keyword Mode' (always listening, filtering for wake word)
            
            if not self.is_active and not self.keyword_mode:
                time.sleep(0.2)
                continue

            if mic is None:
                now = time.time()
                if now - last_mic_retry >= 2.0:
                    last_mic_retry = now
                    try:
                        mic = sr.Microphone()
                        mic_error_reported = False
                        last_noise_calibration = 0.0
                        self.status_update.emit("Mic Ready")
                    except Exception as e:
                        if not mic_error_reported:
                            print(f"[Voice] Microphone unavailable: {e}")
                            self.status_update.emit("Mic unavailable")
                            mic_error_reported = True
                time.sleep(0.2)
                continue

            try:
                current_prompt = brain.get_prompt()
                command_prompt = f"{current_prompt} Commands: {self.command_hint}."
                
                with mic as source:
                    if time.time() - last_noise_calibration > 20:
                        self.status_update.emit("Calibrating noise...")
                        r.adjust_for_ambient_noise(source, duration=0.4)
                        last_noise_calibration = time.time()

                    self.status_update.emit("Listening...")
                    audio = r.listen(source, timeout=5, phrase_time_limit=10) 
                    self.speech_detected.emit()
                
                self.status_update.emit("Transcribing...")
                
                with open("temp_audio.wav", "wb") as f:
                    f.write(audio.get_wav_data())

                if self.keyword_mode and self.enable_openwakeword and self._wakeword_model:
                    score = self._wakeword_score(audio)
                    if score < self.wakeword_threshold:
                        if os.path.exists("temp_audio.wav"):
                            os.remove("temp_audio.wav")
                        self.status_update.emit("Idle")
                        continue

                if not self._has_speech_silero("temp_audio.wav"):
                    if os.path.exists("temp_audio.wav"):
                        os.remove("temp_audio.wav")
                    self.status_update.emit("No speech detected")
                    continue

                if model is not None:
                    try:
                        segments, _ = model.transcribe(
                            "temp_audio.wav", 
                            beam_size=self.beam_size,
                            initial_prompt=command_prompt,
                            vad_filter=True,
                            vad_parameters={
                                "min_silence_duration_ms": 250,
                                "speech_pad_ms": 180,
                            },
                        )
                    except TypeError:
                        # Fallback for older faster-whisper builds that do not accept vad_parameters.
                        segments, _ = model.transcribe(
                            "temp_audio.wav", 
                            beam_size=self.beam_size,
                            initial_prompt=command_prompt,
                            vad_filter=True,
                        )
                    text = "".join([s.text for s in segments]).strip()
                    confidence = self._estimate_whisper_confidence(segments)
                else:
                    text = self._transcribe_with_speech_recognition(r, audio)
                    confidence = None

                text = re.sub(r"\s+", " ", text)
                
                if os.path.exists("temp_audio.wav"): os.remove("temp_audio.wav")

                cleaned = re.sub(r"[^a-zA-Z0-9]", "", text)
                if confidence is not None and confidence < self.transcription_confidence_threshold:
                    self.clarification_needed.emit(text, confidence)
                    self.status_update.emit("Low confidence - ask to repeat")
                    continue

                if text and len(cleaned) >= 2:
                    brain.update(text)
                    self.process_text(text)
                
                self.status_update.emit("Idle")

            except sr.WaitTimeoutError:
                pass 
            except OSError as e:
                print(f"[Voice Error] Microphone IO error: {e}")
                mic = None
                self.status_update.emit("Mic unavailable")
            except Exception as e:
                print(f"[Voice Error] {e}")
                self.status_update.emit("Error")

    def process_text(self, text):
        clean_text = text.lower().strip()
        prepared_text = self._normalize_for_emit(text)

        # Keyword mode takes priority so open-mic state cannot bypass wake-word checks.
        if self.keyword_mode:
            if self._match_wake_alias(clean_text):
                command_text = self._normalize_for_emit(self._strip_wake_prefix(prepared_text))
                if command_text:
                    self.text_received.emit(command_text)
                self.status_update.emit("Wake Word Detected!")
                return

            if self.allow_commands_without_wake_word and _looks_like_voice_command(clean_text):
                if prepared_text:
                    self.text_received.emit(prepared_text)
                self.status_update.emit("Command Detected")
            return

        if self.is_active:
            self.text_received.emit(prepared_text or text)

    def pause_for_processing(self):
        self.auto_paused = True

    def resume_after_processing(self):
        self.auto_paused = False
        if self.keyword_mode:
            self.status_update.emit("Keyword Mode")
        elif self.is_active:
            self.status_update.emit("Mic ON")
        else:
            self.status_update.emit("Mic OFF")

    def toggle_listening(self):
        if self.always_listen_wake_word_only:
            self.keyword_mode = True
            self.is_active = False
            if self.auto_paused:
                self.status_update.emit("Keyword Mode (Auto-paused)")
            else:
                self.status_update.emit("Keyword Mode (always listening)")
            return

        self.is_active = not self.is_active
        state = "ON" if self.is_active else "OFF"
        if self.auto_paused:
            self.status_update.emit(f"Mic {state} (Auto-paused)")
        else:
            self.status_update.emit(f"Mic {state}")