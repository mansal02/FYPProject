# ============================================================================
# ORCHESTRATOR MODULE
# ============================================================================
# Main task routing and orchestration engine.
# Classifies user queries into intents and routes them to appropriate workers.
# Handles online/offline switching, CrewAI delegation, and interaction logging.
# ============================================================================

import socket
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class TaskContext:
    """Context for a user task, including user/session info and metadata."""
    user_id: int
    session_id: str
    mode: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskResult:
    """Result of task execution with response text and actions."""
    text: str
    actions: Dict[str, Any] = field(default_factory=dict)
    sources: list = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


class Orchestrator:
    """
    Main orchestrator for routing tasks to appropriate workers.
    Handles intent classification, online/offline detection, and task logging.
    """
    def __init__(self, config, db, manager, offline_workers, online_workers):
        """
        Initialize orchestrator with all components.
        
        Args:
            config: Application configuration
            db: Database connection
            manager: Intent manager/classifier
            offline_workers: Dict of offline worker instances
            online_workers: Dict of online worker instances
        """
        self.config = config
        self.db = db
        self.manager = manager
        self.offline_workers = offline_workers
        self.online_workers = online_workers
        # Mode: 'auto' (auto-detect), 'online' (force online), 'offline' (force offline)
        self.mode = config.get("mode", "auto")

    def set_mode(self, mode: str) -> None:
        """
        Set execution mode.
        
        Args:
            mode: 'auto', 'online', or 'offline'
        """
        self.mode = mode

    def is_online(self) -> bool:
        """
        Determine if system should use online mode.
        
        Returns:
            True if online mode should be used, False for offline
        """
        if self.mode == "offline":
            return False
        if self.mode == "online":
            return True
        # Auto-detect: ping a test host to check internet connectivity
        return self._ping(self.config.get("network", {}).get("ping_host", "8.8.8.8"))

    def route_task(self, query: str, context: Optional[TaskContext] = None) -> TaskResult:
        """
        Main task routing method.
        Classifies query and routes to appropriate worker.
        
        Args:
            query: User query text
            context: Optional task context (created if None)
            
        Returns:
            TaskResult with response and metadata
        """
        # Create default context if none provided
        if context is None:
            context = TaskContext(user_id=0, session_id="", mode=self.mode)

        # Check for pending confirmations (from previous interaction)
        pending = context.metadata.get("pending_action") if context else None
        decision = _parse_confirmation(query)
        if pending and decision in {"confirm", "cancel"}:
            # User is responding to a confirmation prompt
            intent = pending.get("intent", "general")
            worker = self._select_worker(intent)
            return worker.execute(query, context)

        # Classify the intent of the query
        intent = self.manager.classify(query, context)
        worker = self._select_worker(intent)

        # Optional: Run CrewAI workflow for complex tasks
        crew_text = self.manager.run_crew(query, getattr(worker, "spec", None))
        if crew_text:
            result = TaskResult(text=crew_text, meta={"crewai": True})
        else:
            # Standard worker execution
            result = worker.execute(query, context)

        # Log interaction for history and analytics
        if self.db:
            self.db.log_interaction(
                user_id=context.user_id,
                session_id=context.session_id,
                query=query,
                response=result.text,
                intent=intent,
                mode="online" if self.is_online() else "offline",
            )

        return result

    def _select_worker(self, intent: str):
        """
        Select appropriate worker based on execution mode and intent.
        
        Args:
            intent: Task intent classification
            
        Returns:
            Worker instance for the intent
        """
        # Choose worker dict based on online/offline mode
        workers = self.online_workers if self.is_online() else self.offline_workers
        if intent in workers:
            return workers[intent]
        # Fallback to general worker if intent not found
        return workers.get("general") or next(iter(workers.values()))

    @staticmethod
    def _ping(host: str) -> bool:
        """
        Check internet connectivity by attempting socket connection.
        
        Args:
            host: Hostname or IP to ping
            
        Returns:
            True if host is reachable, False otherwise
        """
        try:
            socket.create_connection((host, 53), timeout=1.0)
            return True
        except OSError:
            return False


def _parse_confirmation(text: str) -> Optional[str]:
    """
    Parse user response to confirm/cancel an action.
    
    Args:
        text: User response text
        
    Returns:
        'confirm', 'cancel', or None
    """
    confirm = {"yes", "confirm", "proceed", "do it", "ok", "okay", "sure", "y"}
    cancel = {"no", "cancel", "stop", "never mind", "n"}
    lower = text.lower()
    if any(token in lower for token in confirm):
        return "confirm"
    if any(token in lower for token in cancel):
        return "cancel"
    return None
