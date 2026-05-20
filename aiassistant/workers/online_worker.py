# ============================================================================
# ONLINE WORKER AGENT
# ============================================================================
# This module implements the OnlineWorker class for processing tasks
# using hybrid pipelines - combining online LLMs (Google Gemini) with
# offline LLMs for specific stages. Great for complex reasoning tasks
# where online models excel, combined with privacy of offline inference.
# ============================================================================

from typing import Any, Dict

try:
    import google.generativeai as genai
except ImportError:
    genai = None

from aiassistant.workers.offline_worker import BaseWorker, OfflineLLMClient


class OnlineLLMClient:
    """
    Client for calling Google Gemini API (online LLM).
    Handles authentication and generation requests.
    """
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Gemini client with API credentials.
        
        Args:
            config: Dict with 'api_key', 'model', and 'temperature' settings
        """
        self.api_key = config.get("api_key")
        self.default_model = config.get("model", "gemini-1.5-flash")
        self.temperature = config.get("temperature", 0.2)

        # Configure Gemini API if library and key are available
        if genai and self.api_key:
            genai.configure(api_key=self.api_key)

    def generate(self, prompt: str, system: str = "", model: str = None) -> str:
        """
        Generate text using Gemini API.
        
        Args:
            prompt: User prompt/question
            system: System instruction for model behavior
            model: Specific model to use (default: self.default_model)
            
        Returns:
            Generated text response or error message
        """
        if not genai or not self.api_key:
            return "Online model not configured. Provide GEMINI_API_KEY."
        
        target_model = model or self.default_model
        try:
            # Create model instance with system instruction
            model_instance = genai.GenerativeModel(
                model_name=target_model,
                system_instruction=system
            )
            # Generate response with temperature setting
            result = model_instance.generate_content(
                prompt, 
                generation_config={"temperature": self.temperature}
            )
            return (result.text or "").strip()
        except Exception as e:
            print(f"[OnlineLLM Error]: {e}")
            return ""


class OnlineWorker(BaseWorker):
    """
    Worker using hybrid online/offline LLM pipeline.
    Can use either Gemini (online) or Ollama (offline) for each stage.
    Useful for: parser (fast offline), reasoner (powerful Gemini),
    formatter (reliable offline), or any custom combination.
    """
    def __init__(self, spec, pipeline: dict, db, config):
        """
        Initialize with both online and offline LLM clients.
        
        Args:
            spec: Worker specification
            pipeline: Pipeline config with provider and model for each stage
            db: Database connection
            config: Application configuration
        """
        super().__init__(spec, pipeline, db, config)
        
        # Online worker needs BOTH clients for flexible pipeline configuration
        self.online_client = OnlineLLMClient(config.get("llm", {}).get("online", {}))
        self.offline_client = OfflineLLMClient(config.get("llm", {}).get("offline", {}))

    def _call_model(self, step_config: dict, prompt: str, system: str) -> str:
        """
        Call appropriate LLM based on pipeline stage configuration.
        
        Args:
            step_config: Dict with 'provider' and 'model' keys
            prompt: User prompt
            system: System instruction
            
        Returns:
            Generated response from selected provider
        """
        provider = step_config.get("provider")
        model_name = step_config.get("model")
        
        # Route to online or offline based on provider
        if provider == "gemini":
            return self.online_client.generate(prompt, system=system, model=model_name)
        else:
            return self.offline_client.generate(prompt, system=system, model=model_name)
