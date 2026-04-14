import os
import time
from pathlib import Path
from threading import RLock

from app_config import CONFIG

try:
    import ollama

    OLLAMA_AVAILABLE = True
except Exception:
    ollama = None
    OLLAMA_AVAILABLE = False

try:
    import pyautogui

    PYAUTOGUI_AVAILABLE = True
except Exception:
    pyautogui = None
    PYAUTOGUI_AVAILABLE = False

_CAPTURE_LOCK = RLock()
_LAST_CAPTURE_TS = 0.0
_LAST_CAPTURE_PAYLOAD = {}


def _safe_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _safe_int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _get_vision_config():
    return CONFIG.get("vision", {})


def _active_window_title():
    if not PYAUTOGUI_AVAILABLE:
        return ""

    try:
        active_window = pyautogui.getActiveWindow()
        if not active_window:
            return ""
        return str(getattr(active_window, "title", "") or "").strip()
    except Exception:
        return ""


def _cleanup_old_captures(screenshot_dir, keep_last=6):
    try:
        captures = sorted(
            screenshot_dir.glob("screen_*.jpg"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return

    for stale_path in captures[keep_last:]:
        try:
            stale_path.unlink()
        except Exception:
            pass


def capture_screen_snapshot():
    """Capture the current screen and return path/title metadata."""
    if not PYAUTOGUI_AVAILABLE:
        return {"error": "PyAutoGUI is unavailable. Install pyautogui to enable screen capture."}

    vision_cfg = _get_vision_config()
    screenshot_dir = Path(str(vision_cfg.get("screenshot_dir", "./cache/screens")))
    min_interval_sec = max(0.0, _safe_float(vision_cfg.get("capture_interval_sec", 0.8), 0.8))
    max_width = max(320, _safe_int(vision_cfg.get("max_width", 1280), 1280))

    global _LAST_CAPTURE_TS, _LAST_CAPTURE_PAYLOAD

    with _CAPTURE_LOCK:
        now = time.time()
        if _LAST_CAPTURE_PAYLOAD and (now - _LAST_CAPTURE_TS) < min_interval_sec:
            last_path = _LAST_CAPTURE_PAYLOAD.get("image_path", "")
            if last_path and os.path.exists(last_path):
                cached = dict(_LAST_CAPTURE_PAYLOAD)
                cached["reused"] = True
                return cached

        try:
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            screenshot = pyautogui.screenshot()

            if screenshot.width > max_width:
                ratio = max_width / float(screenshot.width)
                resized_height = max(1, int(screenshot.height * ratio))
                screenshot = screenshot.resize((max_width, resized_height))

            timestamp_ms = int(now * 1000)
            image_path = str((screenshot_dir / f"screen_{timestamp_ms}.jpg").resolve())
            screenshot.save(image_path, format="JPEG", quality=85, optimize=True)
            _cleanup_old_captures(screenshot_dir)
        except Exception as e:
            return {"error": f"Screen capture failed: {e}"}

        payload = {
            "image_path": image_path,
            "window_title": _active_window_title(),
            "captured_at": now,
            "reused": False,
        }
        _LAST_CAPTURE_TS = now
        _LAST_CAPTURE_PAYLOAD = dict(payload)
        return payload


def describe_screen_snapshot(image_path, user_text="", window_title=""):
    """Return textual screen understanding from an Ollama vision model if configured."""
    if not image_path or not os.path.exists(image_path):
        return f"Active window title: {window_title}" if window_title else ""

    vision_cfg = _get_vision_config()
    vision_model = str(vision_cfg.get("vision_model", "") or "").strip()

    # A configured vision model is optional; without it we still provide the active title.
    if not vision_model or not OLLAMA_AVAILABLE:
        return f"Active window title: {window_title}" if window_title else ""

    intent = user_text.strip() or "No explicit user request was provided."
    prompt = (
        "You are assisting a desktop AI agent. Analyze this screenshot and describe what is visible. "
        "Return 3-8 concise bullet points with the focused app/window, important visible text, errors/warnings, "
        "and actionable UI controls.\n"
        f"User intent: {intent}"
    )

    try:
        response = ollama.chat(
            model=vision_model,
            messages=[{"role": "user", "content": prompt, "images": [image_path]}],
            options={"temperature": 0.1, "num_predict": 220},
        )
        content = ((response or {}).get("message") or {}).get("content", "").strip()
    except Exception:
        content = ""

    title_prefix = f"Active window title: {window_title}\n" if window_title else ""
    if content:
        return f"{title_prefix}{content}".strip()
    return title_prefix.strip()
