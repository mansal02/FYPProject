"""
Mid-tier device optimization utilities.

Provides memory management, model quantization handling, and device class detection
for running MARIE efficiently on constrained hardware (4-8GB RAM, budget GPUs).
"""

import gc
import os
import psutil
from typing import Dict, Optional

from aiassistant.infra.config.app_config import CONFIG


class DeviceCapabilities:
    """Detect device capabilities and recommend optimizations."""

    def __init__(self):
        self.total_memory_gb = psutil.virtual_memory().total / (1024**3)
        self.device_class = self._detect_device_class()
        self.optimization_profile = self._get_optimization_profile()

    def _detect_device_class(self) -> str:
        """Auto-detect device class based on available system RAM."""
        device_class = str(CONFIG.get("runtime", {}).get("device_class", "auto")).strip().lower()
        
        if device_class != "auto":
            return device_class
        
        # Auto-detect based on RAM
        if self.total_memory_gb < 4:
            return "low_end"
        elif self.total_memory_gb < 8:
            return "mid_tier"
        elif self.total_memory_gb < 16:
            return "high_mid"
        else:
            return "high_end"

    def _get_optimization_profile(self) -> Dict[str, object]:
        """Get optimization settings for detected device class."""
        profiles = {
            "low_end": {
                "model_size": "1b",
                "context_window": 1024,
                "quantization": "q5_0",
                "enable_rag": True,
                "enable_live2d": False,
                "embedding_batch_size": 8,
                "top_k": 2,
                "aggressive_gc": True,
                "max_context_chars": 3000,
            },
            "mid_tier": {
                "model_size": "3b",
                "context_window": 2048,
                "quantization": "q4_0",
                "enable_rag": True,
                "enable_live2d": True,
                "embedding_batch_size": 16,
                "top_k": 3,
                "aggressive_gc": True,
                "max_context_chars": 6000,
            },
            "high_mid": {
                "model_size": "7b",
                "context_window": 2048,
                "quantization": "q4_0",
                "enable_rag": True,
                "enable_live2d": False,
                "embedding_batch_size": 32,
                "top_k": 4,
                "aggressive_gc": False,
                "max_context_chars": 9000,
            },
            "high_end": {
                "model_size": "7b",
                "context_window": 4096,
                "quantization": None,
                "enable_rag": True,
                "enable_live2d": True,
                "embedding_batch_size": 64,
                "top_k": 5,
                "aggressive_gc": False,
                "max_context_chars": 12000,
            },
        }
        return profiles.get(self.device_class, profiles["mid_tier"])

    def should_enable_feature(self, feature: str) -> bool:
        """Check if a feature should be enabled based on device class."""
        feature_key = f"enable_{feature}"
        return self.optimization_profile.get(feature_key, False)

    def get_context_window(self) -> int:
        """Get recommended context window for this device."""
        return self.optimization_profile.get("context_window", 2048)

    def get_quantization(self) -> Optional[str]:
        """Get recommended quantization level."""
        return self.optimization_profile.get("quantization", None)

    def get_model_size(self) -> str:
        """Get recommended model size."""
        return self.optimization_profile.get("model_size", "7b")

    def get_max_context_chars(self) -> int:
        """Get max context characters for memory management."""
        return self.optimization_profile.get("max_context_chars", 9000)


class MemoryManager:
    """Aggressive memory management for mid-tier devices."""

    @staticmethod
    def cleanup():
        """Perform aggressive memory cleanup."""
        if CONFIG.get("runtime", {}).get("enable_aggressive_gc", False):
            gc.collect()

    @staticmethod
    def get_available_memory_gb() -> float:
        """Get current available system memory in GB."""
        return psutil.virtual_memory().available / (1024**3)

    @staticmethod
    def is_memory_pressure() -> bool:
        """Check if system is under memory pressure."""
        available_pct = psutil.virtual_memory().percent
        return available_pct > 85  # High memory pressure above 85%

    @staticmethod
    def warn_if_low_memory():
        """Log warning if available memory is critically low."""
        available_gb = MemoryManager.get_available_memory_gb()
        if available_gb < 1:
            print(f"WARNING: Low available memory ({available_gb:.1f}GB)", flush=True)


class QuantizationHelper:
    """Handle Ollama model quantization settings."""

    @staticmethod
    def get_quantization_env_vars(quantization: Optional[str]) -> Dict[str, str]:
        """Get Ollama environment variables for quantization."""
        env = {}
        
        if not quantization:
            # Default: fast quantization for speed
            env["OLLAMA_FLASH_ATTENTION"] = "1"
            env["OLLAMA_KV_CACHE_TYPE"] = "q4_0"
            return env
        
        quantization = str(quantization).lower()
        
        if quantization in {"q4_0", "q4_1"}:
            env["OLLAMA_FLASH_ATTENTION"] = "1"
            env["OLLAMA_KV_CACHE_TYPE"] = quantization
        elif quantization in {"q5_0", "q5_1"}:
            env["OLLAMA_FLASH_ATTENTION"] = "1"
            env["OLLAMA_KV_CACHE_TYPE"] = quantization
        elif quantization == "fp16":
            env["OLLAMA_FLASH_ATTENTION"] = "1"
            env["OLLAMA_KV_CACHE_TYPE"] = "fp16"
        
        return env

    @staticmethod
    def apply_quantization_env():
        """Apply quantization settings from config to environment."""
        quantization = CONFIG.get("ollama", {}).get("quantization_enabled", False)
        if not quantization:
            quantization = "q4_0"  # Default safe quantization
        
        env_vars = QuantizationHelper.get_quantization_env_vars(quantization)
        for key, value in env_vars.items():
            os.environ.setdefault(key, value)


# Global device capabilities instance
_device_caps = None


def get_device_capabilities() -> DeviceCapabilities:
    """Get or create global device capabilities instance."""
    global _device_caps
    if _device_caps is None:
        _device_caps = DeviceCapabilities()
    return _device_caps


def get_memory_manager() -> MemoryManager:
    """Get memory manager instance."""
    return MemoryManager()
