import pyautogui
import re
import time
import os
import threading
import pywhatkit

# Import AppOpener functions
from AppOpener import open as open_app
from AppOpener import close as close_app
from AppOpener import give_appnames

class ActionHandler:
    def __init__(self):
        # 1. CUSTOM APPS / GAMES
        # Add games or portable apps here that the scanner misses.
        # Use double backslashes \\ for paths.
        self.custom_apps = {
            "genshin": r"C:\Program Files\Genshin Impact\Genshin Impact Game\GenshinImpact.exe",
            "minecraft": r"C:\XboxGames\Minecraft Launcher\Content\Minecraft.exe",
            "steam": r"C:\Program Files (x86)\Steam\steam.exe",
            "obs": r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"
        }

    def execute(self, text):
        if not text: return
        text = text.lower().strip()

        # =========================================================
        # 0. SPECIAL COMMAND: UPDATE APP LIST
        # =========================================================
        if "scan apps" in text or "update apps" in text:
            print("[ACTION] Scanning for new apps...")
            # Run this in a thread so it doesn't freeze MARIE
            threading.Thread(target=give_appnames, daemon=True).start()
            return

        # =========================================================
        # 1. YOUTUBE (Play Video)
        # =========================================================
        if text.startswith("play "):
            video_topic = text.replace("play ", "").replace("please", "").strip()
            print(f"[ACTION] Playing on YouTube: {video_topic}")
            try:
                pywhatkit.playonyt(video_topic)
            except Exception as e:
                print(f"[ERROR] YouTube failed: {e}")
            return

        # =========================================================
        # 2. NOTEPAD (Write text)
        # =========================================================
        write_triggers = ["write ", "note ", "type ", "take a note "]
        triggered_word = next((w for w in write_triggers if text.startswith(w)), None)

        if triggered_word:
            content = text.replace(triggered_word, "").strip()
            print(f"[ACTION] Writing to Notepad: {content}")
            os.system("start notepad") 
            time.sleep(1.0) # Wait for it to open
            pyautogui.write(content, interval=0.05)
            return

        # =========================================================
        # 3. SYSTEM CONTROLS
        # =========================================================
        if "volume up" in text:
            pyautogui.press('volumeup')
            return
        elif "volume down" in text:
            pyautogui.press('volumedown')
            return
        elif "mute" in text or "unmute" in text:
            pyautogui.press('volumemute')
            return

        # =========================================================
        # 4. OPEN APPS (Custom + General)
        # =========================================================
        if text.startswith("open "):
            raw_name = text.replace("open ", "").strip()
            app_name = raw_name.replace("please", "").replace("now", "").strip()
            app_name = re.sub(r'[^\w\s]', '', app_name)

            print(f"[ACTION] Opening: '{app_name}'")

            # A. Check Custom List first (Games/Portable)
            for key, path in self.custom_apps.items():
                if key in app_name:
                    print(f"[ACTION] Found custom path for {key}")
                    try:
                        os.startfile(path)
                    except Exception as e:
                        print(f"[ERROR] Custom path failed: {e}")
                    return

            # B. Check General List (AppOpener)
            try:
                open_app(app_name, match_closest=True, output=False, throw_error=True)
            except:
                # C. Last Resort: Windows Start
                try:
                    os.system(f"start {app_name}")
                except:
                    print(f"[ERROR] Could not open '{app_name}'")
            return

        # =========================================================
        # 5. CLOSE APPS
        # =========================================================
        if text.startswith("close "):
            app_name = text.replace("close ", "").replace("please", "").strip()
            app_name = re.sub(r'[^\w\s]', '', app_name)
            
            try:
                close_app(app_name, match_closest=True, output=False, throw_error=True)
            except:
                print(f"[ERROR] Could not close '{app_name}'")
            return