import copy
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

def _find_repo_root() -> Path:
    """Find the project root so config paths stay stable after refactors."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "config.yaml").exists() and (parent / "requirements.txt").exists():
            return parent
    # Safe fallback: old behavior relative to this file.
    return current.parent


ROOT_DIR = _find_repo_root()
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
        "host": "http://127.0.0.1:11434",
        "model": "llama3.2:3b",
        "num_predict": 360,
        "num_ctx": 2048,
        "temperature": 0.2,
        "system_prompt": (
            "You are MARIE, a friendly assistant. "
            "Do not force short replies or fixed word limits. "
            "For simple tasks, answer in easy plain language and be direct. "
            "For complex tasks, provide enough detail to be useful. "
            "Use clear alignment with tool/action output when available. "
            "Stay warm and natural, not robotic."
        ),
    },
    "memory": {
        "max_context_chars": 9000,
        "recent_turn_limit": 10,
        "rad_limit": 220,
        "local_context_file": "./cache/memory.json",
    },
    "memory_agent": {
        "enabled": True,
        "watch_dir": "./knowledge/memory_agent",
        "persist_dir": "./cache/chroma_memory_agent",
        "collection": "marie_memory_agent",
        "embedding_model": "llama3.2:3b",
        "top_k": 4,
        "chunk_size": 850,
        "chunk_overlap": 120,
        "watch_extensions": [
            ".txt",
            ".md",
            ".py",
            ".json",
            ".yaml",
            ".yml",
            ".csv",
            ".rad",
            ".log",
        ],
    },
    "voice": {
        "default_character": "tachyon",
        "speaking_speed": 1.0,
        "whisper_model_size": "base",
        "energy_threshold": 650,
        "whisper_device": "cpu",
        "enable_faster_whisper": False,
        "wake_word": "hey",
        "always_listen_wake_word_only": True,
        "microphone_hotkey": "F4",
        "summon_hotkey": "ctrl+space",
        "allow_commands_without_wake_word": True,
        "allow_online_fallback": True,
        "enable_openwakeword": False,
        "enable_silero_vad": True,
    },
    "ui": {
        "theme": "dark",
        "transparent_face": False,
        "enable_live2d": False,
        "response_only_mode": False,
        "response_only_opacity": 0.88,
    },
    "crew": {
            "researcher": "qwen2.5:7b",
            "coder": "qwen2.5:7b",
            "synthesizer": "llama3.1:8b",
        "provider": "fallback",
        "context_max_chars": 900,
        "verbose": False,
    },
    "vision": {
        "screen_share_enabled": False,
        "vision_model": "moondream",
        "screenshot_dir": "./cache/screens",
        "capture_interval_sec": 0.8,
        "max_width": 1280,
    },
    "runtime": {
        "hybrid_mode": False,
        "external_model": "gemini-2.0-flash",
        "online_mode": "auto",
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


def _as_str_list(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return fallback
    return [part.strip() for part in text.split(",") if part.strip()]


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
    config["voice"]["energy_threshold"] = max(
        600,
        _as_int(
            os.environ.get("MARIE_VOICE_ENERGY_THRESHOLD"),
            config["voice"].get("energy_threshold", 650),
        ),
    )
    whisper_size = str(
        os.environ.get("MARIE_WHISPER_MODEL_SIZE", config["voice"].get("whisper_model_size", "base"))
    ).strip().lower()
    if whisper_size not in {"base", "small"}:
        whisper_size = "base"
    config["voice"]["whisper_model_size"] = whisper_size
    config["voice"]["allow_online_fallback"] = _as_bool(
        os.environ.get("MARIE_ALLOW_ONLINE_FALLBACK"),
        config["voice"].get("allow_online_fallback", True),
    )
    config["voice"]["allow_commands_without_wake_word"] = _as_bool(
        os.environ.get("MARIE_ALLOW_COMMANDS_WITHOUT_WAKE_WORD"),
        config["voice"].get("allow_commands_without_wake_word", True),
    )
    config["ui"]["theme"] = os.environ.get("MARIE_UI_THEME", config["ui"].get("theme", "dark"))
    config["ui"]["enable_live2d"] = _as_bool(
        os.environ.get("MARIE_ENABLE_LIVE2D"),
        config["ui"].get("enable_live2d", False),
    )
    config["ui"]["response_only_mode"] = _as_bool(
        os.environ.get("MARIE_RESPONSE_ONLY_MODE"),
        config["ui"].get("response_only_mode", False),
    )
    config["ui"]["response_only_opacity"] = _as_float(
        os.environ.get("MARIE_RESPONSE_ONLY_OPACITY"),
        config["ui"].get("response_only_opacity", 0.88),
    )
    if _as_bool(os.environ.get("MARIE_DISABLE_LIVE2D"), False):
        config["ui"]["enable_live2d"] = False

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

    config["runtime"]["hybrid_mode"] = _as_bool(
        os.environ.get("MARIE_HYBRID_MODE"),
        config["runtime"].get("hybrid_mode", False),
    )
    config["runtime"]["external_model"] = os.environ.get(
        "MARIE_EXTERNAL_MODEL",
        config["runtime"].get("external_model", "gemini-2.0-flash"),
    )
    online_mode = str(
        os.environ.get("MARIE_ONLINE_MODE", config["runtime"].get("online_mode", "auto"))
    ).strip().lower()
    if online_mode not in {"auto", "online", "offline"}:
        online_mode = "auto"
    config["runtime"]["online_mode"] = online_mode

    config["crew"]["enabled"] = _as_bool(
        os.environ.get("MARIE_CREW_ENABLED"),
        config["crew"].get("enabled", False),
    )
    config["crew"]["mode"] = os.environ.get(
        "MARIE_CREW_MODE",
        config["crew"].get("mode", "assist"),
    )
    config["crew"]["router"] = os.environ.get(
        "MARIE_CREW_ROUTER",
        config["crew"].get("router", "complex_only"),
    )
    config["crew"]["provider"] = os.environ.get(
        "MARIE_CREW_PROVIDER",
        config["crew"].get("provider", "fallback"),
    )
    config["crew"]["context_max_chars"] = _as_int(
        os.environ.get("MARIE_CREW_CONTEXT_MAX_CHARS"),
        config["crew"].get("context_max_chars", 900),
    )
    config["crew"]["verbose"] = _as_bool(
        os.environ.get("MARIE_CREW_VERBOSE"),
        config["crew"].get("verbose", False),
    )

    config["memory"]["local_context_file"] = os.environ.get(
        "MARIE_LOCAL_CONTEXT_FILE",
        config["memory"].get("local_context_file", "./cache/memory.json"),
    )

    config["memory_agent"]["enabled"] = _as_bool(
        os.environ.get("MARIE_MEMORY_AGENT_ENABLED"),
        config["memory_agent"].get("enabled", True),
    )
    config["memory_agent"]["watch_dir"] = os.environ.get(
        "MARIE_MEMORY_AGENT_WATCH_DIR",
        config["memory_agent"].get("watch_dir", "./knowledge/memory_agent"),
    )
    config["memory_agent"]["persist_dir"] = os.environ.get(
        "MARIE_MEMORY_AGENT_PERSIST_DIR",
        config["memory_agent"].get("persist_dir", "./cache/chroma_memory_agent"),
    )
    config["memory_agent"]["collection"] = os.environ.get(
        "MARIE_MEMORY_AGENT_COLLECTION",
        config["memory_agent"].get("collection", "marie_memory_agent"),
    )
    config["memory_agent"]["embedding_model"] = os.environ.get(
        "MARIE_MEMORY_AGENT_EMBED_MODEL",
        config["memory_agent"].get("embedding_model", "llama3.2:3b"),
    )
    config["memory_agent"]["top_k"] = _as_int(
        os.environ.get("MARIE_MEMORY_AGENT_TOP_K"),
        config["memory_agent"].get("top_k", 4),
    )
    config["memory_agent"]["chunk_size"] = _as_int(
        os.environ.get("MARIE_MEMORY_AGENT_CHUNK_SIZE"),
        config["memory_agent"].get("chunk_size", 850),
    )
    config["memory_agent"]["chunk_overlap"] = _as_int(
        os.environ.get("MARIE_MEMORY_AGENT_CHUNK_OVERLAP"),
        config["memory_agent"].get("chunk_overlap", 120),
    )
    config["memory_agent"]["watch_extensions"] = _as_str_list(
        os.environ.get("MARIE_MEMORY_AGENT_EXTENSIONS"),
        config["memory_agent"].get("watch_extensions", [".txt", ".md"]),
    )

    # Normalize paths
    for key in (
        "db_path",
        "auto_login_file",
        "default_live2d_model",
        "piper_dir",
        "rvc_dir",
        "knowledge_dir",
    ):
        config["paths"][key] = _resolve_path(config["paths"][key])

    config["vision"]["screenshot_dir"] = _resolve_path(config["vision"]["screenshot_dir"])
    config["memory"]["local_context_file"] = _resolve_path(config["memory"]["local_context_file"])
    config["memory_agent"]["watch_dir"] = _resolve_path(config["memory_agent"]["watch_dir"])
    config["memory_agent"]["persist_dir"] = _resolve_path(config["memory_agent"]["persist_dir"])

    return config


CONFIG = load_config()
