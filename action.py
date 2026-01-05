import pyautogui
import re
import threading
from AppOpener import open as open_app
from AppOpener import close as close_app

class ActionHandler:
    def __init__(self):
        pass

    def execute(self, text):
        """
        Parses USER INPUT for commands.
        """
        if not text:
            return

        text = text.lower().strip()

        # --- 1. SYSTEM CONTROLS ---
        if "volume up" in text:
            pyautogui.press('volumeup')
            print("[ACTION] Volume Up")
            return
        elif "volume down" in text:
            pyautogui.press('volumedown')
            print("[ACTION] Volume Down")
            return
        elif "mute" in text or "unmute" in text:
            pyautogui.press('volumemute')
            print("[ACTION] Mute/Unmute")
            return

        # --- 2. OPEN APPLICATIONS ---
        # Regex to capture app name. We remove "please", "now", etc. later.
        if text.startswith("open "):
            app_name = text.replace("open ", "").strip()
            
            # Clean up common conversational filler words
            for filler in ["please", "now", "for me", "can you"]:
                app_name = app_name.replace(filler, "").strip()

            # Remove punctuation (.,!?)
            app_name = re.sub(r'[^\w\s]', '', app_name)

            print(f"[ACTION] Opening: '{app_name}'")
            try:
                # throw_error=True prevents it from asking for CLI input if ambiguous
                open_app(app_name, match_closest=True, output=False, throw_error=True)
            except Exception as e:
                print(f"[ERROR] AppOpener failed for '{app_name}': {e}")
            return

        # --- 3. CLOSE APPLICATIONS ---
        if text.startswith("close "):
            app_name = text.replace("close ", "").strip()
            
            # Clean up
            for filler in ["please", "now"]:
                app_name = app_name.replace(filler, "").strip()
            app_name = re.sub(r'[^\w\s]', '', app_name)

            print(f"[ACTION] Closing: '{app_name}'")
            try:
                close_app(app_name, match_closest=True, output=False, throw_error=True)
            except Exception as e:
                print(f"[ERROR] Could not close '{app_name}': {e}")
            return