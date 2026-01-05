import os
import subprocess
import pygame
import threading
import time
import random
import re
import queue
import glob
from voice_db import get_character_data, PIPER_DIR

PIPER_EXE = os.path.join(PIPER_DIR, "piper.exe")

class MarieVoice:
    def __init__(self, default_char="tachyon"):
        print(f"[AUDIO] Initializing Piper Engine...")
        if not os.path.exists(PIPER_EXE):
            raise FileNotFoundError("piper.exe not found!")

        # Create a dedicated cache folder to avoid file conflicts
        self.cache_dir = os.path.join(os.path.dirname(__file__), "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._clear_cache()

        # Audio System
        try:
            # Init mixer with standard settings; Sound() handles resampling automatically
            pygame.mixer.init()
            # We use a dedicated Channel for voice to separate it from potential SFX
            self.channel = pygame.mixer.Channel(0)
        except Exception as e:
            print(f"[AUDIO] Mixer Error: {e}")

        # Threading & Queues
        self.speech_queue = queue.Queue()
        self.is_running = True
        self.is_speaking = False
        
        # Start Worker
        self.worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self.worker_thread.start()

        self.set_voice(default_char)
        print("[AUDIO] Engine Ready.")
        self._warmup()

    def set_voice(self, char_id):
        self.char_data, self.model_path = get_character_data(char_id)
        self.current_name = self.char_data["name"]
        self.emotions = self.char_data["emotions"]
        print(f"[AUDIO] Voice set to: {self.current_name}")

    def _clear_cache(self):
        """Cleans up old audio files on startup."""
        for f in glob.glob(os.path.join(self.cache_dir, "*.wav")):
            try: os.remove(f)
            except: pass

    def _warmup(self):
        """Generates a dummy file to ensure Piper is ready."""
        try:
            dummy_path = os.path.join(self.cache_dir, "warmup.wav")
            subprocess.run([PIPER_EXE, "--model", self.model_path, "--output_file", dummy_path], 
                         input=b".", stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except Exception as e:
            print(f"[AUDIO] Warmup skipped: {e}")

    def _get_physics(self, text):
        """Extracts speed/emotion tags from text."""
        target_speed = 1.0
        clean_text = text
        
        match = re.search(r"\[([a-zA-Z]+)\]", text)
        if match:
            tag = match.group(1).lower()
            if tag in self.emotions:
                data = self.emotions[tag]
                target_speed = data.get("speed", 1.0)
            clean_text = re.sub(r"\[([a-zA-Z]+)\]", "", text).strip()
        else:
            data = self.emotions.get("default", {})
            target_speed = data.get("speed", 1.0)
            
        return clean_text, target_speed

    def speak(self, text, face_callback=None):
        if not text.strip(): return
        self.speech_queue.put((text, face_callback))

    def stop(self):
        """Stops playback and clears pending sentences."""
        with self.speech_queue.mutex:
            self.speech_queue.queue.clear()
        if self.channel:
            self.channel.stop()
        self.is_speaking = False

    def _process_queue(self):
        """Main loop: pulls text, generates audio, and plays it sequentially."""
        file_counter = 0
        
        while self.is_running:
            try:
                # Wait for next sentence
                text, face_callback = self.speech_queue.get(timeout=1)
            except queue.Empty:
                continue
            
            self.is_speaking = True
            
            # Use rotating filenames to prevent file locking issues
            filename = f"sentence_{file_counter}.wav"
            filepath = os.path.join(self.cache_dir, filename)
            file_counter = (file_counter + 1) % 20 # Keep only 20 files max
            
            try:
                self._generate_and_play(text, filepath, face_callback)
            except Exception as e:
                print(f"[AUDIO ERROR] {e}")
            finally:
                self.is_speaking = False
                self.speech_queue.task_done()

    def _generate_and_play(self, text, filepath, face_callback):
        clean_text, speed = self._get_physics(text)
        print(f"[{self.current_name.upper()}]: {clean_text}")

        length_scale = 1.0 / float(speed)
        
        # Prepare Piper Command
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        cmd = [
            PIPER_EXE, 
            "--model", self.model_path, 
            "--output_file", filepath, 
            "--length_scale", str(length_scale)
        ]

        # Generate Audio
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, startupinfo=startupinfo)
        proc.communicate(input=clean_text.encode('utf-8'))

        # Playback
        if os.path.exists(filepath):
            try:
                # Load Sound into memory (Fixes file lock issues)
                sound = pygame.mixer.Sound(filepath)
                self.channel.play(sound)
                
                # Block thread until audio finishes (Prevents overlapping)
                clock = pygame.time.Clock()
                while self.channel.get_busy():
                    # Check if app is closing
                    if not self.is_running: 
                        self.channel.stop()
                        break
                    
                    # Optional: Manual mouth movement callback
                    if face_callback:
                        face_callback(random.uniform(0.3, 1.0))
                    
                    clock.tick(30)
                
            except pygame.error as e:
                print(f"[PLAYBACK ERROR] {e}")
            
            # Small pause between sentences for natural flow
            time.sleep(0.05)