import copy
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")

_DEFAULT_CONFIG = {
    "paths": {
        "db_path": "./marie_data.db",
        "auto_login_file": "./.marie_autologin.json",
        "default_live2d_model": "./models/kei/runtime/kei_vowels_pro.model3.json",
        "piper_dir": "./piper",
        "rvc_dir": "./rvc_models",
        "knowledge_dir": "./knowledge",
    },
    "servers": {
        "reasoning_url": "http://127.0.0.1:8000/chat",
        "reasoning_stream_url": "http://127.0.0.1:8000/chat/stream",
        "reasoning_stop_url": "http://127.0.0.1:8000/chat/stop",
        "voice_url": "http://127.0.0.1:8001/speak",
        "voice_stop_url": "http://127.0.0.1:8001/stop",
        "reasoning_host": "127.0.0.1",
        "reasoning_port": 8000,
        "voice_host": "127.0.0.1",
        "voice_port": 8001,
    },
    "ollama": {
        "model": "llama3",
        "num_predict": 180,
        "num_ctx": 2048,
        "temperature": 0.2,
        "system_prompt": (
            "You are MARIE, a friendly assistant. "
            "Reply with only the important points and keep answers short. "
            "Use clear alignment with tool/action output when available. "
            "Default to 2-5 concise bullet points when explaining. "
            "Stay warm and natural, not robotic."
        ),
    },
    "memory": {
        "max_context_chars": 9000,
        "recent_turn_limit": 10,
        "rad_limit": 220,
    },
    "voice": {
        "default_character": "tachyon",
        "speaking_speed": 1.0,
        "whisper_device": "cpu",
        "enable_faster_whisper": False,
        "wake_word": "hey marie",
        "microphone_hotkey": "F4",
        "summon_hotkey": "ctrl+space",
        "enable_openwakeword": False,
        "enable_silero_vad": True,
    },
    "ui": {
        "theme": "dark",
        "transparent_face": False,
    },
    "vision": {
        "screen_share_enabled": False,
        "vision_model": "",
        "screenshot_dir": "./cache/screens",
        "capture_interval_sec": 0.8,
        "max_width": 1280,
    },
    "actions": {
        "safe_mode": True,
        "allow_legacy_text_commands": True,
    },
}


def _as_int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _as_bool(value, fallback=False):
    if value is None:
        return fallback
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return fallback


def _merge_dict(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_dict(base[key], value)
        else:
            base[key] = value


def _resolve_path(raw_path):
    path_obj = Path(raw_path)
    if path_obj.is_absolute():
        return str(path_obj)
    return str((ROOT_DIR / path_obj).resolve())


def load_config():
    config = copy.deepcopy(_DEFAULT_CONFIG)

    config_path = os.environ.get("MARIE_CONFIG_PATH", "./config.yaml")
    config_file = Path(config_path)
    if not config_file.is_absolute():
        config_file = (ROOT_DIR / config_file).resolve()

    if config_file.exists():
        with config_file.open("r", encoding="utf-8") as fp:
            loaded = yaml.safe_load(fp) or {}
        if isinstance(loaded, dict):
            _merge_dict(config, loaded)

    # Environment overrides
    config["paths"]["db_path"] = os.environ.get("MARIE_DB_PATH", config["paths"]["db_path"])
    config["paths"]["auto_login_file"] = os.environ.get("MARIE_AUTO_LOGIN_FILE", config["paths"]["auto_login_file"])
    config["paths"]["default_live2d_model"] = os.environ.get("MARIE_DEFAULT_MODEL", config["paths"]["default_live2d_model"])

    config["servers"]["reasoning_url"] = os.environ.get("MARIE_REASONING_URL", config["servers"]["reasoning_url"])
    config["servers"]["reasoning_stream_url"] = os.environ.get("MARIE_REASONING_STREAM_URL", config["servers"]["reasoning_stream_url"])
    config["servers"]["reasoning_stop_url"] = os.environ.get("MARIE_REASONING_STOP_URL", config["servers"]["reasoning_stop_url"])
    config["servers"]["voice_url"] = os.environ.get("MARIE_VOICE_URL", config["servers"]["voice_url"])
    config["servers"]["voice_stop_url"] = os.environ.get("MARIE_VOICE_STOP_URL", config["servers"]["voice_stop_url"])
    config["servers"]["reasoning_port"] = _as_int(os.environ.get("MARIE_REASONING_PORT"), config["servers"]["reasoning_port"])
    config["servers"]["voice_port"] = _as_int(os.environ.get("MARIE_VOICE_PORT"), config["servers"]["voice_port"])

    config["ollama"]["model"] = os.environ.get("MARIE_OLLAMA_MODEL", config["ollama"]["model"])
    config["ollama"]["num_predict"] = _as_int(os.environ.get("MARIE_NUM_PREDICT"), config["ollama"]["num_predict"])
    config["ollama"]["num_ctx"] = _as_int(os.environ.get("MARIE_NUM_CTX"), config["ollama"]["num_ctx"])
    config["ollama"]["temperature"] = _as_float(os.environ.get("MARIE_TEMPERATURE"), config["ollama"]["temperature"])
    config["voice"]["speaking_speed"] = _as_float(
        os.environ.get("MARIE_SPEAKING_SPEED"),
        config["voice"].get("speaking_speed", 1.0),
    )
    config["ui"]["theme"] = os.environ.get("MARIE_UI_THEME", config["ui"].get("theme", "dark"))

    config["voice"]["enable_openwakeword"] = _as_bool(
        os.environ.get("MARIE_ENABLE_OPENWAKEWORD"),
        config["voice"].get("enable_openwakeword", False),
    )
    config["voice"]["enable_silero_vad"] = _as_bool(
        os.environ.get("MARIE_ENABLE_SILERO_VAD"),
        config["voice"].get("enable_silero_vad", True),
    )

    config["vision"]["screen_share_enabled"] = _as_bool(
        os.environ.get("MARIE_SCREEN_SHARE_ENABLED"),
        config["vision"].get("screen_share_enabled", False),
    )
    config["vision"]["vision_model"] = os.environ.get(
        "MARIE_VISION_MODEL",
        config["vision"].get("vision_model", ""),
    )
    config["vision"]["screenshot_dir"] = os.environ.get(
        "MARIE_SCREENSHOT_DIR",
        config["vision"].get("screenshot_dir", "./cache/screens"),
    )
    config["vision"]["capture_interval_sec"] = _as_float(
        os.environ.get("MARIE_SCREEN_CAPTURE_INTERVAL_SEC"),
        config["vision"].get("capture_interval_sec", 0.8),
    )
    config["vision"]["max_width"] = _as_int(
        os.environ.get("MARIE_SCREEN_MAX_WIDTH"),
        config["vision"].get("max_width", 1280),
    )

    # Normalize paths
    for key in ("db_path", "auto_login_file", "default_live2d_model", "piper_dir", "rvc_dir", "knowledge_dir"):
        config["paths"][key] = _resolve_path(config["paths"][key])

    config["vision"]["screenshot_dir"] = _resolve_path(config["vision"]["screenshot_dir"])

    return config


CONFIG = load_config()
