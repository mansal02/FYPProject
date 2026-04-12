import os
import re
import time
import spacy
import speech_recognition as sr
from collections import deque
from PyQt5.QtCore import QThread, pyqtSignal

# Check for GPU
try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WhisperModel = None
    WHISPER_AVAILABLE = False

try:
    import torch
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
except ImportError:
    DEVICE = "cpu"

COMPUTE_TYPE = "float16" if DEVICE == "cuda" else "int8"

try:
    nlp = spacy.load("en_core_web_sm")
except Exception:
    nlp = spacy.blank("en")

class ContextBrain:
    def __init__(self, max_history=3):
        self.history = deque(maxlen=max_history)
        self.keywords = set()

    def update(self, text):
        doc = nlp(text)
        new_keywords = {token.text for token in doc if token.pos_ in ["NOUN", "PROPN"]}
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

    def __init__(self, model_size="base", wake_word="hey"):
        super().__init__()
        self.wake_word = wake_word.lower()
        self.is_active = False          
        self.keyword_mode = False       
        self.running = True
        self.auto_paused = False
        self.model_size = model_size
        
    def run(self):
        if not WHISPER_AVAILABLE:
            self.status_update.emit("Whisper unavailable (install faster-whisper)")
            print("[Voice Error] faster-whisper is not installed. Run: pip install faster-whisper")
            return

        self.status_update.emit(f"Loading Whisper ({DEVICE})...")
        model = WhisperModel(self.model_size, device=DEVICE, compute_type=COMPUTE_TYPE)
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
                
                self.status_update.emit("Transcribing...")
                
                with open("temp_audio.wav", "wb") as f:
                    f.write(audio.get_wav_data())

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