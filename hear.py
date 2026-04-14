import os
import re
import time
import speech_recognition as sr
from collections import deque
from PyQt5.QtCore import QThread, pyqtSignal
from app_config import CONFIG

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

    def __init__(self, model_size="base", wake_word="hey"):
        super().__init__()
        voice_cfg = CONFIG.get("voice", {})
        resolved_wake = wake_word or voice_cfg.get("wake_word", "hey marie")
        self.wake_word = resolved_wake.lower()
        self.is_active = False          
        self.keyword_mode = False       
        self.running = True
        self.auto_paused = False
        self.model_size = model_size
        self.whisper_device = DEVICE
        self.compute_type = "float16" if self.whisper_device == "cuda" else "int8"
        self.enable_faster_whisper = bool(voice_cfg.get("enable_faster_whisper", False))
        self.enable_silero_vad = bool(voice_cfg.get("enable_silero_vad", True))
        self.enable_openwakeword = bool(voice_cfg.get("enable_openwakeword", False))
        self.wakeword_threshold = float(voice_cfg.get("wakeword_threshold", 0.35))

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
        r.dynamic_energy_threshold = True
        r.dynamic_energy_adjustment_damping = 0.15
        r.non_speaking_duration = 0.25
        r.phrase_threshold = 0.2
        mic = sr.Microphone()
        last_noise_calibration = 0.0
        was_auto_paused = False

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

            try:
                current_prompt = brain.get_prompt()
                
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
                            beam_size=5, 
                            initial_prompt=current_prompt,
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
                            beam_size=5, 
                            initial_prompt=current_prompt,
                            vad_filter=True,
                        )
                    text = "".join([s.text for s in segments]).strip()
                else:
                    text = ""
                    try:
                        text = r.recognize_sphinx(audio).strip()
                    except Exception:
                        try:
                            text = r.recognize_google(audio).strip()
                        except Exception:
                            text = ""

                text = re.sub(r"\s+", " ", text)
                
                if os.path.exists("temp_audio.wav"): os.remove("temp_audio.wav")

                cleaned = re.sub(r"[^a-zA-Z0-9]", "", text)
                if text and len(cleaned) >= 2:
                    brain.update(text)
                    self.process_text(text)
                
                self.status_update.emit("Idle")

            except sr.WaitTimeoutError:
                pass 
            except Exception as e:
                print(f"[Voice Error] {e}")
                self.status_update.emit("Error")

    def process_text(self, text):
        clean_text = text.lower()
        
        
        if self.is_active:
            self.text_received.emit(text) 
            
        elif self.keyword_mode:
            if self.wake_word in clean_text:
                # Remove wake word (optional) and send
                # valid_command = clean_text.split(self.wake_word, 1)[1]
                self.text_received.emit(text)
                self.status_update.emit("Wake Word Detected!")

    def pause_for_processing(self):
        self.auto_paused = True

    def resume_after_processing(self):
        self.auto_paused = False
        if self.is_active:
            self.status_update.emit("Mic ON")
        elif self.keyword_mode:
            self.status_update.emit("Keyword Mode")
        else:
            self.status_update.emit("Mic OFF")

    def toggle_listening(self):
        self.is_active = not self.is_active
        state = "ON" if self.is_active else "OFF"
        if self.auto_paused:
            self.status_update.emit(f"Mic {state} (Auto-paused)")
        else:
            self.status_update.emit(f"Mic {state}")