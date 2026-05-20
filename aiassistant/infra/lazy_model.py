"""
Lazy model loading for mid-tier device optimization.

Defers expensive model initialization until first use,
reducing startup time and initial memory footprint.
"""

from typing import Optional, Dict, Any
import threading
import ollama as ollama_module

from aiassistant.infra.config.app_config import CONFIG


class LazyOllamaClient:
    """Lazy-loading wrapper for Ollama client.
    
    Defers client initialization until first use,
    reducing memory footprint on startup.
    """
    
    def __init__(self, host: str = "http://127.0.0.1:11434"):
        self.host = host
        self._client = None
        self._lock = threading.Lock()
    
    def _ensure_initialized(self) -> Optional[ollama_module.Client]:
        """Initialize client on first use."""
        if self._client is not None:
            return self._client
        
        with self._lock:
            if self._client is not None:
                return self._client
            
            try:
                self._client = ollama_module.Client(host=self.host)
                return self._client
            except Exception:
                return None
    
    def chat(self, **kwargs) -> Any:
        """Lazy-loaded chat call."""
        client = self._ensure_initialized()
        if client is None:
            raise RuntimeError("Ollama client not available")
        return client.chat(**kwargs)
    
    def generate(self, **kwargs) -> Any:
        """Lazy-loaded generate call."""
        client = self._ensure_initialized()
        if client is None:
            raise RuntimeError("Ollama client not available")
        return client.generate(**kwargs)
    
    def __getattr__(self, name: str) -> Any:
        """Proxy all other methods to underlying client."""
        client = self._ensure_initialized()
        if client is None:
            raise RuntimeError("Ollama client not available")
        return getattr(client, name)


class ModelCacheManager:
    """Manage model caching and unloading for mid-tier devices."""
    
    def __init__(self):
        self.loaded_models: Dict[str, float] = {}  # model_name -> load_time
        self._lock = threading.Lock()
    
    def mark_loaded(self, model_name: str) -> None:
        """Record when a model was loaded."""
        import time
        with self._lock:
            self.loaded_models[model_name] = time.time()
    
    def get_loaded_models(self) -> list:
        """Get list of currently tracked models."""
        with self._lock:
            return list(self.loaded_models.keys())
    
    def should_unload_model(self, model_name: str) -> bool:
        """Check if model should be unloaded based on idle time."""
        model_unload_enabled = CONFIG.get("runtime", {}).get(
            "model_unload_after_inference", True
        )
        return model_unload_enabled
    
    def unload_model_after_inference(self) -> None:
        """Unload all tracked models to free memory."""
        if not CONFIG.get("runtime", {}).get("model_unload_after_inference", True):
            return
        
        with self._lock:
            self.loaded_models.clear()


_lazy_client = None
_model_cache_manager = None


def get_lazy_ollama_client() -> LazyOllamaClient:
    """Get singleton lazy Ollama client."""
    global _lazy_client
    if _lazy_client is None:
        host = str(CONFIG.get("ollama", {}).get("host", "http://127.0.0.1:11434"))
        _lazy_client = LazyOllamaClient(host=host)
    return _lazy_client


def get_model_cache_manager() -> ModelCacheManager:
    """Get singleton model cache manager."""
    global _model_cache_manager
    if _model_cache_manager is None:
        _model_cache_manager = ModelCacheManager()
    return _model_cache_manager
