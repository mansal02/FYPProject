import gc
import os
import psutil
from typing import Dict, Optional
from aiassistant.infra.config.app_config import CONFIG

def calculate_dynamic_context_window(base_ctx: int = 2048, max_ctx: int = 8192) -> int:
    """
    Dynamically removes static context constraints. Scales the context window 
    up for large PDF summarization if the system has enough free RAM.
    """
    try:
        free_ram_gb = psutil.virtual_memory().available / (1024 ** 3)
        dynamic_ctx = int(base_ctx + (free_ram_gb / 1.5) * 1000)
        return max(base_ctx, min(dynamic_ctx, max_ctx))
    except Exception:
        return base_ctx 

class DeviceCapabilities:
    """Standard device capabilities without tier restrictions."""
    def __init__(self):
        self.optimization_profile = {
            "model_size": "7b",
            "context_window": 2048, 
            "quantization": None,
            "enable_rag": True,
            "enable_live2d": True,
            "embedding_batch_size": 32,
            "top_k": 4,
            "aggressive_gc": False,
            "max_context_chars": 9000,
        }

    def should_enable_feature(self, feature: str) -> bool:
        return self.optimization_profile.get(f"enable_{feature}", False)

    def get_context_window(self) -> int:
        # WIRED UP: Dynamically scale context based on available RAM
        base_ctx = self.optimization_profile.get("context_window", 2048)
        return calculate_dynamic_context_window(base_ctx=base_ctx)

    def get_quantization(self) -> Optional[str]:
        return self.optimization_profile.get("quantization", None)

    def get_model_size(self) -> str:
        return self.optimization_profile.get("model_size", "7b")

    def get_max_context_chars(self) -> int:
        return self.optimization_profile.get("max_context_chars", 9000)

class MemoryManager:
    @staticmethod
    def cleanup():
        gc.collect()

    @staticmethod
    def get_available_memory_gb() -> float:
        return psutil.virtual_memory().available / (1024**3)

    @staticmethod
    def is_memory_pressure() -> bool:
        return (psutil.virtual_memory().percent > 85)

    @staticmethod
    def warn_if_low_memory():
        available_gb = MemoryManager.get_available_memory_gb()
        if available_gb < 1:
            print(f"WARNING: Low available memory ({available_gb:.1f}GB)", flush=True)

class QuantizationHelper:
    @staticmethod
    def get_quantization_env_vars(quantization: Optional[str]) -> Dict[str, str]:
        env = {"OLLAMA_FLASH_ATTENTION": "1"}
        if not quantization:
            env["OLLAMA_KV_CACHE_TYPE"] = "q4_0"
            return env
        
        quantization = str(quantization).lower()
        if quantization in {"q4_0", "q4_1", "q5_0", "q5_1", "fp16"}:
            env["OLLAMA_KV_CACHE_TYPE"] = quantization
        return env

    @staticmethod
    def apply_quantization_env():
        quantization = CONFIG.get("ollama", {}).get("quantization_enabled", False)
        env_vars = QuantizationHelper.get_quantization_env_vars(quantization if quantization else "q4_0")
        for key, value in env_vars.items():
            os.environ.setdefault(key, value)

_device_caps = None

def get_device_capabilities() -> DeviceCapabilities:
    global _device_caps
    if _device_caps is None:
        _device_caps = DeviceCapabilities()
    return _device_caps

def get_memory_manager() -> MemoryManager:
    return MemoryManager()
