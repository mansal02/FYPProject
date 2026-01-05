import pyautogui
import re
# Import the open/close functions. 
# We alias them to avoid conflict with python's built-in open()
from AppOpener import open as open_app
from AppOpener import close as close_app

class ActionHandler:
    def __init__(self):
        # We don't need to hardcode paths anymore!
        # AppOpener handles the database of installed apps.
        pass

    def execute(self, text):
        """
        Parses the text for commands.
        1. "open <name>" -> Uses AppOpener
        2. "close <name>" -> Uses AppOpener
        3. System controls -> Uses PyAutoGUI
        """
        if not text:
            return

        text = text.lower().strip()

        # --- 1. SYSTEM CONTROLS (Volume, etc.) ---
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
        # Regex to capture everything after "open "
        match_open = re.search(r"open\s+(.+)", text)
        if match_open:
            app_name = match_open.group(1).strip()
            print(f"[ACTION] Attempting to open: {app_name}")
            try:
                # match_closest=True allows fuzzy matching (e.g., "google" opens "chrome")
                open_app(app_name, match_closest=True, output=False) 
            except Exception as e:
                print(f"[ERROR] Could not open {app_name}: {e}")
            return

        # --- 3. CLOSE APPLICATIONS ---
        match_close = re.search(r"close\s+(.+)", text)
        if match_close:
            app_name = match_close.group(1).strip()
            print(f"[ACTION] Attempting to close: {app_name}")
            try:
                close_app(app_name, match_closest=True, output=False)
            except Exception as e:
                print(f"[ERROR] Could not close {app_name}: {e}")
            return