import os
import time
import spacy
import speech_recognition as sr
import keyboard 
from collections import deque
from PyQt5.QtCore import QThread, pyqtSignal

# Check for GPU
try:
    import torch
    from faster_whisper import WhisperModel
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    COMPUTE_TYPE = "float16" if DEVICE == "cuda" else "int8"
except ImportError:
    DEVICE = "cpu"
    COMPUTE_TYPE = "int8"

nlp = spacy.load("en_core_web_sm")

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
        self.model_size = model_size
        
    def run(self):
        self.status_update.emit(f"Loading Whisper ({DEVICE})...")
        model = WhisperModel(self.model_size, device=DEVICE, compute_type=COMPUTE_TYPE)
        brain = ContextBrain()
        
        r = sr.Recognizer()
        r.pause_threshold = 0.8
        r.dynamic_energy_adjustment_damping = 0.15
        mic = sr.Microphone()

        self.status_update.emit("Voice Ready (Press F4 to Toggle)")

        while self.running:
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
                    if r.energy_threshold == 300: 
                        r.adjust_for_ambient_noise(source, duration=0.5)

                    self.status_update.emit("Listening...")
                    audio = r.listen(source, timeout=5, phrase_time_limit=10) 
                
                self.status_update.emit("Transcribing...")
                
                with open("temp_audio.wav", "wb") as f:
                    f.write(audio.get_wav_data())

                segments, _ = model.transcribe(
                    "temp_audio.wav", 
                    beam_size=5, 
                    initial_prompt=current_prompt
                )
                
                text = "".join([s.text for s in segments]).strip()
                
                if os.path.exists("temp_audio.wav"): os.remove("temp_audio.wav")

                if text:
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

    def toggle_listening(self):
        self.is_active = not self.is_active
        state = "ON" if self.is_active else "OFF"
        self.status_update.emit(f"Mic {state}")