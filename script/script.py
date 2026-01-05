import ctypes
from ctypes import wintypes
import time

def force_vts_on_top():
    print("Searching for VTube Studio window...")
    
    # Access Windows User32 DLL
    user32 = ctypes.windll.user32
    
    # Find the window by its exact title "VTube Studio"
    hwnd = user32.FindWindowW(None, "VTube Studio")
    
    if hwnd:
        print("Found VTube Studio! Forcing it to top...")
        # Magic numbers for Windows API:
        # HWND_TOPMOST = -1
        # Flags = SWP_NOMOVE | SWP_NOSIZE (Ignore position/size, just change Z-order)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003)
        print("Success. VTube Studio is now pinned.")
    else:
        print("Error: VTube Studio is not running.")

if __name__ == "__main__":
    force_vts_on_top()