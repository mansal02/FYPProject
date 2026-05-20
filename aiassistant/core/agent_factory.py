# ============================================================================
# AGENT FACTORY
# ============================================================================
# Factory for creating and configuring LLM workers (agents).
# Handles agent specs, model selection, and pipeline configuration
# for different task intents (office, web, os, files, general).
# ============================================================================

from dataclasses import dataclass
from typing import Dict, List, Optional

from aiassistant.workers.offline_worker import OfflineLLMClient, OfflineWorker
from aiassistant.workers.online_worker import OnlineLLMClient, OnlineWorker

try:
    from crewai import Agent, Task, Crew
except ImportError:
    Agent = None
    Task = None
    Crew = None


# Default keyword mappings for intent classification
DEFAULT_INTENTS = {
    "os": ["volume", "brightness", "open", "launch", "shutdown", "restart", "settings"],
    "office": ["excel", "word", "outlook", "teams", "sheet", "document"],
    "web": ["gmail", "calendar", "drive", "youtube", "discord", "whatsapp", "chrome"],
    "files": ["find", "search", "file", "folder", "document", "pdf"],
    "general": [],
}


@dataclass
class WorkerSpec:
    """Specification for an LLM worker agent."""
    name: str
    intent: str
    description: str
    tools: Dict[str, callable]
    offline_pipeline: Dict[str, str]  
    hybrid_pipeline: Dict[str, Dict[str, str]]


class Manager:
    """
    Manager for classifying user intents and optionally running CrewAI.
    Routes tasks to appropriate workers based on intent classification.
    """
    def __init__(self, llm=None, intents=None, config=None):
        """
        Initialize manager with optional LLM and intent definitions.
        
        Args:
            llm: LLM client for advanced classification (optional)
            intents: Dict mapping intent names to keywords
            config: Application configuration
        """
        self.llm = llm
        self.intents = intents or DEFAULT_INTENTS
        self.config = config or {}
        crew_cfg = self.config.get("crewai", {})
        enabled_flag = crew_cfg.get("enabled", self.config.get("use_crewai", False))
        # Check if CrewAI is enabled and library is available
        self.crewai_enabled = bool(enabled_flag) and Agent is not None
        self.crewai_llm = crew_cfg.get("llm")

    def classify(self, query: str, context) -> str:
        """
        Classify user query into one of the predefined intents.
        Uses simple keyword matching.
        
        Args:
            query: User query text
            context: Task context (unused currently)
            
        Returns:
            Intent name (os, office, web, files, or general)
        """
        query_lower = query.lower()
        # Check keywords for each intent
        for intent, keywords in self.intents.items():
            if keywords and any(kw in query_lower for kw in keywords):
                return intent
        # Default to general if no keywords match
        return "general"

    def run_crew(self, query: str, worker_spec: Optional[WorkerSpec]) -> Optional[str]:
        """
        Optional: Run CrewAI workflow for complex tasks.
        Useful for multi-agent coordination and planning.
        
        Args:
            query: User query
            worker_spec: Worker specification for context
            
        Returns:
            CrewAI response or None if CrewAI is disabled
        """
        if not self.crewai_enabled or not Agent or not Task or not Crew:
            return None
        
        # Create manager and worker agents
        manager_agent = Agent(
            role="Manager",
            goal="Route the task to the correct specialist and produce a concise plan.",
            backstory="You coordinate offline and online assistant workers.",
            llm=self.crewai_llm,
            allow_delegation=True,
        )
        worker_agent = Agent(
            role=worker_spec.name if worker_spec else "General Worker",
            goal=worker_spec.description if worker_spec else "Answer user tasks.",
            backstory="You are a specialist worker for the local assistant.",
            llm=self.crewai_llm,
        )
        # Create and execute task
        task = Task(
            description=f"User request: {query}",
            agent=worker_agent,
            expected_output="A concise response and next actions.",
        )
        crew = Crew(agents=[manager_agent, worker_agent], tasks=[task], verbose=False)
        return str(crew.kickoff())


class AgentFactory:
    """
    Factory for creating and configuring LLM workers.
    Handles initialization of specific worker types based on configuration.
    """
    def __init__(self, config, db=None):
        """
        Initialize factory with application config and database.
        
        Args:
            config: Application configuration
            db: Optional database connection for RAG
        """
        self.config = config or {}
        self.db = db

    def create_manager(self) -> Manager:
        """
        Create the task manager/classifier.
        
        Returns:
            Manager instance with configured LLM
        """
        llm_cfg = self.config.get("llm", {}).get("manager", {}).copy()
        provider = llm_cfg.get("provider", "ollama")
        
        # Create appropriate LLM client based on provider
        if provider == "gemini":
            llm = OnlineLLMClient(llm_cfg)
        else:
            llm = OfflineLLMClient(llm_cfg)
            
        return Manager(llm=llm, intents=DEFAULT_INTENTS, config=self.config)

    def create_workers(self, mode: str):
        """
        Create offline or online workers for all intents.
        
        Args:
            mode: 'offline' or 'online' - determines LLM providers used
            
        Returns:
            Dict mapping intent names to worker instances
        """
        specs = self._build_worker_specs()
        workers = {}

        # Create workers based on mode
        for spec in specs:
            if mode == "offline":
                worker = OfflineWorker(spec, spec.offline_pipeline, self.db, self.config)
            else:
                worker = OnlineWorker(spec, spec.hybrid_pipeline, self.db, self.config)
            workers[spec.intent] = worker

        return workers

    def _build_worker_specs(self) -> List[WorkerSpec]:
        """
        Build worker specifications for all task intents.
        Defines which tools and models each worker uses.
        
        Returns:
            List of WorkerSpec instances
        """
        return [
            WorkerSpec(
                name="OS Worker",
                intent="os",
                description="Controls local OS features, settings, and application launching.",
                tools={},
                offline_pipeline={
                    "parser": "qwen2.5:0.5b",
                    "reasoner": "llama3.2:3b",
                    "formatter": "qwen2.5:0.5b"
                },
                hybrid_pipeline={
                    "parser": {"provider": "ollama", "model": "qwen2.5:0.5b"},
                    "reasoner": {"provider": "gemini", "model": "gemini-1.5-flash"},
                    "formatter": {"provider": "ollama", "model": "qwen2.5:0.5b"}
                }
            ),
            WorkerSpec(
                name="Office Worker",
                intent="office",
                description="Handles Excel, Word, Outlook, and Teams tasks, including document summarization.",
                tools={},
                offline_pipeline={
                    "parser": "qwen2.5:0.5b",
                    "reasoner": "qwen2.5:7b",
                    "formatter": "llama3.2:3b"
                },
                hybrid_pipeline={
                    "parser": {"provider": "ollama", "model": "qwen2.5:0.5b"},
                    "reasoner": {"provider": "gemini", "model": "gemini-1.5-pro"},
                    "formatter": {"provider": "ollama", "model": "llama3.2:3b"}
                }
            ),
            WorkerSpec(
                name="Web Worker",
                intent="web",
                description="Operates browser apps like Gmail, Drive, YouTube, and WhatsApp.",
                tools={},
                offline_pipeline={
                    "parser": "qwen2.5:0.5b",
                    "reasoner": "llama3.1:8b",
                    "formatter": "qwen2.5:0.5b"
                },
                hybrid_pipeline={
                    "parser": {"provider": "ollama", "model": "qwen2.5:0.5b"},
                    "reasoner": {"provider": "gemini", "model": "gemini-1.5-flash"},
                    "formatter": {"provider": "ollama", "model": "qwen2.5:0.5b"}
                }
            ),
            WorkerSpec(
                name="File Worker",
                intent="files",
                description="Searches and retrieves local documents using RAG vector embeddings.",
                tools={},
                offline_pipeline={
                    "parser": "qwen2.5:0.5b",
                    "reasoner": "llama3.1:8b",
                    "formatter": "qwen2.5:0.5b"
                },
                hybrid_pipeline={
                    "parser": {"provider": "ollama", "model": "qwen2.5:0.5b"},
                    "reasoner": {"provider": "gemini", "model": "gemini-1.5-flash"},
                    "formatter": {"provider": "ollama", "model": "qwen2.5:0.5b"}
                }
            ),
            WorkerSpec(
                name="General Worker",
                intent="general",
                description="Fallback conversational assistant for general questions and chats.",
                tools={},
                offline_pipeline={
                    "parser": "qwen2.5:0.5b",
                    "reasoner": "llama3.1:8b", 
                    "formatter": "qwen2.5:0.5b"
                },
                hybrid_pipeline={
                    "parser": {"provider": "ollama", "model": "qwen2.5:0.5b"},
                    "reasoner": {"provider": "gemini", "model": "gemini-1.5-flash"},
                    "formatter": {"provider": "ollama", "model": "qwen2.5:0.5b"}
                }
            ),
        ]
