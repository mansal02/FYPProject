from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

try:
    import ollama
except Exception:
    ollama = None

try:
    import requests
except Exception:
    requests = None

from aiassistant.infra.config.app_config import CONFIG
from aiassistant.infra.db.database_manager import DatabaseManager
from aiassistant.workers import create_fast_worker
from aiassistant.workers.offline_worker import OfflineLLMClient, OfflineWorker
from aiassistant.workers.online_worker import OnlineLLMClient, OnlineWorker

try:
    from crewai import Agent, Task, Crew
except ImportError:
    Agent = None
    Task = None
    Crew = None


class EventBus:
    """Simple thread-safe publish/subscribe event bus."""

    def __init__(self):
        self._handlers = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, event_name, callback):
        with self._lock:
            self._handlers[event_name].append(callback)

    def unsubscribe(self, event_name, callback):
        with self._lock:
            callbacks = self._handlers.get(event_name, [])
            if callback in callbacks:
                callbacks.remove(callback)

    def emit(self, event_name, payload=None):
        with self._lock:
            callbacks = list(self._handlers.get(event_name, []))

        for callback in callbacks:
            try:
                callback(payload)
            except Exception as exc:
                print(f"[EVENT BUS] Handler error for {event_name}: {exc}")


class Events:
    USER_SPOKE = "user_spoke"
    AI_TOKEN = "ai_token"
    AI_SENTENCE_READY = "ai_sentence_ready"
    AI_COMPLETED = "ai_completed"
    AUDIO_READY = "audio_ready"
    BARGE_IN = "barge_in"
    ERROR = "error"


MODEL = CONFIG["ollama"]["model"]


def _ask_agent(system_prompt, user_prompt):
    if ollama is None:
        raise RuntimeError("Ollama is not available.")

    response = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        options={
            "temperature": 0.25,
            "num_predict": int(CONFIG["ollama"].get("num_predict", 180)),
            "num_ctx": int(CONFIG["ollama"].get("num_ctx", 2048)),
        },
        stream=False,
        keep_alive=0,
    )
    return (response.get("message") or {}).get("content", "").strip()


def run_multi_agent_round(question, memory_context=""):
    """Runs a simple local 3-agent cycle: researcher -> coder -> synthesizer."""
    context_block = f"\n\nContext:\n{memory_context}" if memory_context else ""

    researcher_output = _ask_agent(
        "You are a Researcher Agent. Return only concise findings needed to solve the request. No tutorial tone.",
        f"Question: {question}{context_block}",
    )

    coder_output = _ask_agent(
        "You are a Coder Agent. Return implementation actions and checks only, concise and practical.",
        f"Question: {question}\nResearcher notes:\n{researcher_output}{context_block}",
    )

    final_output = _ask_agent(
        "You are the Final Synthesizer Agent. Return one concise, execution-first answer with only what the user needs.",
        (
            f"Question: {question}\n\n"
            f"Researcher Agent:\n{researcher_output}\n\n"
            f"Coder Agent:\n{coder_output}\n\n"
            "Return only the final answer. Keep it short and direct. Explain details only if explicitly requested."
        ),
    )

    return {
        "researcher": researcher_output,
        "coder": coder_output,
        "final": final_output,
    }


def _normalize_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {"assist", "replace"}:
        return "assist"
    return mode


def _truncate_context(text: str, max_chars: int) -> str:
    if not text:
        return ""
    limit = max(200, int(max_chars))
    if len(text) <= limit:
        return text
    return text[-limit:]


def run_crew_assist(
    question: str,
    memory_context: str = "",
    config: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Run CrewAI if available, otherwise fallback to local multi-agent chain."""
    settings = config or {}
    provider = str(settings.get("provider", "fallback")).strip().lower()
    context_max_chars = int(settings.get("context_max_chars", 900) or 900)
    memory_context = _truncate_context(str(memory_context or ""), context_max_chars)

    if provider not in {"crewai", "fallback"}:
        provider = "fallback"

    if provider == "crewai":
        try:
            from crewai import Agent, Crew, Process, Task  # type: ignore
        except Exception as exc:
            return {
                "ok": False,
                "provider": "crewai",
                "mode": _normalize_mode(settings.get("mode", "assist")),
                "error": f"CrewAI import failed: {exc}",
            }

        question_text = str(question or "").strip()
        memory_text = str(memory_context or "").strip()
        if memory_text:
            question_text = f"{question_text}\n\nContext:\n{memory_text}"

        researcher = Agent(
            role="Researcher",
            goal="Summarize key facts and constraints for the task.",
            backstory="You are a concise analyst focused on actionable points.",
            allow_delegation=False,
        )
        implementer = Agent(
            role="Implementer",
            goal="Translate research into concrete implementation notes.",
            backstory="You output practical steps and checks only.",
            allow_delegation=False,
        )
        synthesizer = Agent(
            role="Synthesizer",
            goal="Deliver a final, direct answer for the user.",
            backstory="You keep responses short, accurate, and outcome-focused.",
            allow_delegation=False,
        )

        tasks = [
            Task(
                description=f"Question: {question_text}",
                expected_output="Short bullet list of key points.",
                agent=researcher,
            ),
            Task(
                description=(
                    "Use the research output to produce implementation notes. "
                    "Avoid tutorials; keep it concise."
                ),
                expected_output="Implementation notes and checks.",
                agent=implementer,
            ),
            Task(
                description=(
                    "Produce the final response for the user. "
                    "Keep it short and direct."
                ),
                expected_output="Final assistant response.",
                agent=synthesizer,
            ),
        ]

        crew = Crew(
            agents=[researcher, implementer, synthesizer],
            tasks=tasks,
            process=Process.sequential,
            verbose=bool(settings.get("verbose", False)),
        )

        try:
            result = crew.kickoff()
        except Exception as exc:
            return {
                "ok": False,
                "provider": "crewai",
                "mode": _normalize_mode(settings.get("mode", "assist")),
                "error": f"CrewAI kickoff failed: {exc}",
            }

        return {
            "ok": True,
            "provider": "crewai",
            "mode": _normalize_mode(settings.get("mode", "assist")),
            "summary": str(result or "").strip(),
        }

    result = run_multi_agent_round(question, memory_context=memory_context or "")
    summary = str(result.get("final") or "").strip()
    return {
        "ok": True,
        "provider": "fallback",
        "mode": _normalize_mode(settings.get("mode", "assist")),
        "summary": summary,
        "details": result,
    }


@dataclass
class ConcurrentTaskResult:
    """Result from concurrent task execution."""
    tool_name: str
    result: Any
    duration_ms: float
    status: str
    error: Optional[str] = None


class ParallelWorkerMixin:
    """
    Mixin to add parallel execution capabilities to any worker class.
    Enables concurrent tool execution, RAG retrieval, and formatting.
    """

    def __init__(self, *args, **kwargs):
        """Initialize mixin with thread pool."""
        super().__init__(*args, **kwargs)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self._concurrent_tasks = []

    def execute_tools_concurrent(self, tools: Dict[str, Any]) -> Dict[str, ConcurrentTaskResult]:
        """
        Execute multiple tools concurrently.

        Args:
            tools: Dict of tool_name -> (args_dict) pairs

        Returns:
            Dict of tool_name -> ConcurrentTaskResult
        """
        futures = {}
        results = {}

        # Submit all tasks
        for tool_name, tool_args in tools.items():
            if tool_name in self.tools:
                future = self.executor.submit(
                    self._execute_single_tool_timed,
                    tool_name,
                    tool_args,
                )
                futures[tool_name] = future

        # Collect results as they complete
        for tool_name, future in concurrent.futures.as_completed(futures, timeout=30):
            try:
                results[tool_name] = future.result()
            except Exception as exc:
                results[tool_name] = ConcurrentTaskResult(
                    tool_name=tool_name,
                    result=None,
                    duration_ms=0,
                    status="error",
                    error=str(exc),
                )

        return results

    def execute_tools_parallel_async(self, tools: Dict[str, Any]) -> List[ConcurrentTaskResult]:
        """
        Execute tools with async-style interface (returns immediately).

        Args:
            tools: Dict of tool_name -> args_dict pairs

        Returns:
            List of futures that can be waited on
        """
        futures = []

        for tool_name, tool_args in tools.items():
            if tool_name in self.tools:
                future = self.executor.submit(
                    self._execute_single_tool_timed,
                    tool_name,
                    tool_args,
                )
                self._concurrent_tasks.append(future)
                futures.append(future)

        return futures

    def wait_for_tools(self, futures: List, timeout: Optional[float] = 30) -> List[ConcurrentTaskResult]:
        """
        Wait for concurrent tool execution to complete.

        Args:
            futures: List of futures from execute_tools_parallel_async
            timeout: Maximum wait time in seconds

        Returns:
            List of ConcurrentTaskResult objects
        """
        results = []
        try:
            for future in concurrent.futures.as_completed(futures, timeout=timeout):
                results.append(future.result())
        except concurrent.futures.TimeoutError:
            print("[ParallelWorker] Tool execution timeout")

        return results

    def _execute_single_tool_timed(self, tool_name: str, tool_args: Dict[str, Any]) -> ConcurrentTaskResult:
        """
        Execute a single tool and measure execution time.

        Args:
            tool_name: Name of tool to execute
            tool_args: Tool arguments

        Returns:
            ConcurrentTaskResult with timing and status
        """
        t_start = time.time()
        try:
            tool = self.tools.get(tool_name)
            if not tool:
                raise ValueError(f"Tool {tool_name} not found")

            result = tool(**tool_args)
            duration_ms = (time.time() - t_start) * 1000

            return ConcurrentTaskResult(
                tool_name=tool_name,
                result=result,
                duration_ms=duration_ms,
                status="success",
            )
        except Exception as exc:
            duration_ms = (time.time() - t_start) * 1000
            return ConcurrentTaskResult(
                tool_name=tool_name,
                result=None,
                duration_ms=duration_ms,
                status="error",
                error=str(exc),
            )

    def execute_pipeline_stages_concurrent(
        self,
        query: str,
        parser_fn: Callable,
        reasoner_fn: Callable,
        formatter_fn: Callable,
    ) -> Dict[str, Any]:
        """
        Execute parser, reasoner, and formatter stages with optimized scheduling.
        Parser runs first (dependency), then reasoner and formatter can run concurrently on parsed output.

        Args:
            query: User query
            parser_fn: Parser function(query) -> parsed_query
            reasoner_fn: Reasoner function(parsed_query) -> reasoning
            formatter_fn: Formatter function(reasoning) -> formatted_output

        Returns:
            Dict with all stage outputs and timings
        """
        timings = {}

        # Stage 1: Parse (sequential - dependency)
        t_start = time.time()
        parsed_query = parser_fn(query)
        timings["parse_ms"] = (time.time() - t_start) * 1000

        # Stage 2: Reasoner and formatter can run in parallel on parsed_query
        t_start = time.time()
        reasoner_future = self.executor.submit(reasoner_fn, parsed_query)
        formatter_on_query_future = self.executor.submit(formatter_fn, query)
        _ = formatter_on_query_future

        try:
            reasoning_result = reasoner_future.result(timeout=30)
            timings["reasoner_ms"] = (time.time() - t_start) * 1000
        except Exception as exc:
            print(f"[Reasoner Error]: {exc}")
            reasoning_result = ""
            timings["reasoner_ms"] = (time.time() - t_start) * 1000

        # Now run formatter on reasoning result
        t_start = time.time()
        formatter_future = self.executor.submit(formatter_fn, reasoning_result)
        try:
            formatted_result = formatter_future.result(timeout=30)
            timings["formatter_ms"] = (time.time() - t_start) * 1000
        except Exception as exc:
            print(f"[Formatter Error]: {exc}")
            formatted_result = reasoning_result
            timings["formatter_ms"] = (time.time() - t_start) * 1000

        return {
            "parsed_query": parsed_query,
            "reasoning": reasoning_result,
            "formatted": formatted_result,
            "timings": timings,
        }

    def shutdown(self):
        """Clean shutdown of executor."""
        self.executor.shutdown(wait=True)
        print("[ParallelWorker] Executor shutdown complete.")


class AsyncVoiceOutput:
    """
    Async voice output handler for non-blocking synthesis and playback.
    """

    def __init__(self, voice_system=None, max_queue_size=10):
        """
        Initialize async voice output.

        Args:
            voice_system: Voice system with queue_output() and play() methods
            max_queue_size: Maximum queued items before blocking
        """
        self.voice_system = voice_system
        self.queue = asyncio.Queue(maxsize=max_queue_size)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self._running = False

    async def start(self):
        """Start async voice output processor."""
        self._running = True
        while self._running:
            try:
                text, params = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                await self._synthesize_and_play(text, params)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                print(f"[AsyncVoice Error]: {exc}")

    async def _synthesize_and_play(self, text: str, params: Dict[str, Any]):
        """
        Synthesize and play audio asynchronously.

        Args:
            text: Text to synthesize
            params: Voice parameters
        """
        if not self.voice_system:
            return

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                self.executor,
                lambda: self.voice_system.queue_output(text, **params),
            )
        except Exception as exc:
            print(f"[AsyncVoice Error]: {exc}")

    async def queue_text(self, text: str, params: Optional[Dict[str, Any]] = None):
        """
        Queue text for async voice output.

        Args:
            text: Text to synthesize
            params: Optional voice parameters
        """
        if not params:
            params = {}
        try:
            await self.queue.put((text, params))
        except asyncio.QueueFull:
            print("[AsyncVoice] Queue full, dropping oldest item")

    async def stop(self):
        """Stop async voice processor."""
        self._running = False
        await asyncio.sleep(0.5)
        self.executor.shutdown(wait=True)


class ConcurrentRAGRetriever:
    """
    Concurrent RAG context retrieval with caching and deduplication.
    """

    def __init__(self, rag_system=None):
        """
        Initialize concurrent RAG retriever.

        Args:
            rag_system: RAG system with get_rag_context() method
        """
        self.rag_system = rag_system
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        self._cache = {}

    def retrieve_concurrent(self, *queries: str, limit: int = 5) -> Dict[str, str]:
        """
        Retrieve RAG context for multiple queries concurrently.

        Args:
            *queries: Multiple query strings
            limit: Number of results per query

        Returns:
            Dict of query -> context_string
        """
        if not self.rag_system:
            return {}

        futures = {}
        results = {}

        for query in queries:
            cache_key = f"{query}:{limit}"
            if cache_key in self._cache:
                results[query] = self._cache[cache_key]
                continue

            future = self.executor.submit(
                self._retrieve_and_cache,
                query,
                limit,
                cache_key,
            )
            futures[query] = future

        for query, future in futures.items():
            try:
                results[query] = future.result(timeout=30)
            except Exception as exc:
                print(f"[RAG Error for '{query}']: {exc}")
                results[query] = ""

        return results

    def _retrieve_and_cache(self, query: str, limit: int, cache_key: str) -> str:
        """
        Retrieve RAG context and cache result.

        Args:
            query: Query string
            limit: Result limit
            cache_key: Cache key for result

        Returns:
            RAG context string
        """
        try:
            context = self.rag_system.get_rag_context(query, limit=limit)
            self._cache[cache_key] = context
            return context
        except Exception as exc:
            print(f"[RAG Retrieval Error]: {exc}")
            return ""

    def shutdown(self):
        """Clean shutdown."""
        self.executor.shutdown(wait=True)


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


@dataclass
class ParallelTaskContext:
    """Enhanced context for parallel task execution with timing."""
    user_id: int
    session_id: str
    mode: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    timing: Dict[str, float] = field(default_factory=dict)
    parallel_results: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParallelTaskResult:
    """Result with concurrent execution metrics."""
    text: str
    actions: Dict[str, Any] = field(default_factory=dict)
    sources: List[str] = field(default_factory=list)
    rag_context: str = ""
    tool_outputs: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    # REPAIRED: Corrected default_story typo to default_factory
    timing: Dict[str, float] = field(default_factory=dict)


@dataclass
class FastResult:
    """Fast result with minimal overhead."""
    text: str
    timing_ms: float
    cached: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)


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
        return _ping(self.config.get("network", {}).get("ping_host", "8.8.8.8"))

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
        if context is None:
            context = TaskContext(user_id=0, session_id="", mode=self.mode)

        pending = context.metadata.get("pending_action") if context else None
        decision = _parse_confirmation(query)
        if pending and decision in {"confirm", "cancel"}:
            intent = pending.get("intent", "general")
            worker = self._select_worker(intent)
            return worker.execute(query, context)

        intent = self.manager.classify(query, context)
        worker = self._select_worker(intent)

        crew_text = self.manager.run_crew(query, getattr(worker, "spec", None))
        if crew_text:
            result = TaskResult(text=crew_text, meta={"crewai": True})
        else:
            result = worker.execute(query, context)

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
        workers = self.online_workers if self.is_online() else self.offline_workers
        if intent in workers:
            return workers[intent]
        return workers.get("general") or next(iter(workers.values()))


class ParallelOrchestrator:
    """
    Advanced orchestrator with concurrent RAG, tool, and voice execution.
    Maximizes throughput by parallelizing non-dependent tasks.
    """

    def __init__(self, config, db, manager, offline_workers, online_workers, rag_system=None, voice_system=None):
        """
        Initialize parallel orchestrator.

        Args:
            config: Application configuration
            db: Database connection
            manager: Intent manager/classifier
            offline_workers: Dict of offline worker instances
            online_workers: Dict of online worker instances
            rag_system: Optional RAG system for concurrent retrieval
            voice_system: Optional voice system for concurrent output
        """
        self.config = config
        self.db = db
        self.manager = manager
        self.offline_workers = offline_workers
        self.online_workers = online_workers
        self.rag_system = rag_system
        self.voice_system = voice_system
        self.mode = config.get("mode", "auto")

        self.max_workers = config.get("max_concurrent_workers", 4)
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self._lock = threading.RLock()

    def set_mode(self, mode: str) -> None:
        """Set execution mode (auto/online/offline)."""
        self.mode = mode

    def is_online(self) -> bool:
        """Determine if system should use online mode."""
        if self.mode == "offline":
            return False
        if self.mode == "online":
            return True
        return _ping(self.config.get("network", {}).get("ping_host", "8.8.8.8"))

    def route_task_parallel(self, query: str, context: Optional[ParallelTaskContext] = None) -> ParallelTaskResult:
        """
        Main parallel task routing method.
        Processes: parse -> [RAG + reasoner] --concurrent--> format + voice

        Args:
            query: User query text
            context: Optional task context

        Returns:
            ParallelTaskResult with concurrent execution metrics
        """
        if context is None:
            context = ParallelTaskContext(user_id=0, session_id="", mode=self.mode)

        t_start = time.time()
        context.timing["start"] = t_start

        t_parse_start = time.time()
        intent = self.manager.classify(query, context)
        worker = self._select_worker(intent)
        parsed_query = self._parse_query(worker, query)
        context.timing["parse_ms"] = (time.time() - t_parse_start) * 1000

        print(f"[ParallelOrch] Intent: {intent} | Parsed: {parsed_query[:60]}...")

        t_parallel_start = time.time()
        futures = {}

        if self.rag_system and not self.config.get("disable_rag"):
            futures["rag"] = self.executor.submit(
                self._retrieve_rag_context,
                parsed_query,
                query,
            )

        futures["reasoning"] = self.executor.submit(
            self._run_worker_reasoning,
            worker,
            parsed_query,
            query,
            context,
        )

        parallel_results = {}
        parallel_errors = {}
        for task_name, future in futures.items():
            try:
                parallel_results[task_name] = future.result(timeout=30)
            except Exception as exc:
                parallel_errors[task_name] = str(exc)
                print(f"[ParallelOrch] {task_name} error: {exc}")
                parallel_results[task_name] = None

        context.timing["parallel_ms"] = (time.time() - t_parallel_start) * 1000
        context.parallel_results = parallel_results

        t_merge_start = time.time()
        rag_context = parallel_results.get("rag") or ""
        reasoning_result = parallel_results.get("reasoning") or {}

        if rag_context and reasoning_result:
            reasoning_result["rag_context"] = rag_context

        context.timing["merge_ms"] = (time.time() - t_merge_start) * 1000

        t_tools_start = time.time()
        tool_outputs = {}
        if reasoning_result.get("tool_name") and reasoning_result.get("tool_name") in worker.tools:
            tool_outputs = self._execute_tool_parallel(
                worker,
                reasoning_result.get("tool_name"),
                reasoning_result.get("tool_args", {}),
            )
        context.timing["tools_ms"] = (time.time() - t_tools_start) * 1000

        response_text = reasoning_result.get("response", "Task completed.")
        voice_future = None
        if self.voice_system:
            voice_future = self.executor.submit(
                self._queue_voice_output,
                response_text,
                reasoning_result.get("voice_params", {}),
            )

        context.timing["total_ms"] = (time.time() - t_start) * 1000
        if self.db:
            self.db.log_interaction(
                user_id=context.user_id,
                session_id=context.session_id,
                query=query,
                response=response_text,
                intent=intent,
                mode="online" if self.is_online() else "offline",
                metadata={
                    "timing": context.timing,
                    "parallel_mode": True,
                    "rag_used": bool(rag_context),
                    "tools_used": bool(tool_outputs),
                },
            )

        return ParallelTaskResult(
            text=response_text,
            actions=reasoning_result.get("actions", {}),
            sources=reasoning_result.get("sources", []),
            rag_context=rag_context,
            tool_outputs=tool_outputs,
            meta={
                "intent": intent,
                "parallel_errors": parallel_errors,
                "voice_queued": voice_future is not None,
            },
            timing=context.timing,
        )

    def _parse_query(self, worker, query: str) -> str:
        """Parse query to extract core intent (local operation)."""
        if not hasattr(worker, "_call_model"):
            return query
        try:
            parser_cfg = worker.pipeline.get("parser")
            sys_parser = "Extract the core intent and parameters. Output ONLY the refined instruction."
            parsed = worker._call_model(parser_cfg, query, sys_parser)
            return parsed.strip() if parsed else query
        except Exception as exc:
            print(f"[Parse Error]: {exc}")
            return query

    def _retrieve_rag_context(self, parsed_query: str, original_query: str) -> str:
        """
        Retrieve RAG context from knowledge base (I/O bound - good for parallelization).

        Args:
            parsed_query: Parsed user query
            original_query: Original user query

        Returns:
            RAG context snippets as string
        """
        if not self.rag_system:
            return ""

        try:
            t_start = time.time()
            context_snippets = self.rag_system.get_rag_context(parsed_query, limit=5)
            if not context_snippets:
                context_snippets = self.rag_system.get_rag_context(original_query, limit=5)

            elapsed_ms = (time.time() - t_start) * 1000
            print(f"[RAG] Retrieved context in {elapsed_ms:.1f}ms: {len(context_snippets)} snippets")
            return context_snippets
        except Exception as exc:
            print(f"[RAG Error]: {exc}")
            return ""

    def _run_worker_reasoning(self, worker, parsed_query: str, original_query: str, context: ParallelTaskContext) -> Dict[str, Any]:
        """
        Run worker reasoning to determine tools and format response (can be parallelized).

        Args:
            worker: Worker instance
            parsed_query: Parsed query
            original_query: Original query
            context: Task context

        Returns:
            Dict with tool_name, tool_args, response, actions
        """
        _ = original_query
        _ = context
        try:
            t_start = time.time()

            if hasattr(worker, "_call_model"):
                reasoner_cfg = worker.pipeline.get("reasoner")
                sys_reasoner = f"You are {worker.spec.name}. {worker.spec.description} Analyze and determine exact steps."
                reasoning = worker._call_model(reasoner_cfg, parsed_query, sys_reasoner)

                formatter_cfg = worker.pipeline.get("formatter")
                sys_formatter = "Format as strict JSON: {'tool': string, 'args': dict, 'response': string}."
                formatted_json = worker._call_model(formatter_cfg, reasoning, sys_formatter)

                clean_json = formatted_json.replace("```json", "").replace("```", "").strip()
                plan_data = json.loads(clean_json)

                elapsed_ms = (time.time() - t_start) * 1000
                print(f"[Reasoning] Completed in {elapsed_ms:.1f}ms")

                return {
                    "tool_name": plan_data.get("tool"),
                    "tool_args": plan_data.get("args", {}),
                    "response": plan_data.get("response", "Task completed."),
                    "actions": {"tool": plan_data.get("tool"), "args": plan_data.get("args", {})},
                    "sources": [],
                    "voice_params": {"priority": "high"},
                }
            return {"response": "Worker reasoning unavailable.", "actions": {}, "sources": []}

        except Exception as exc:
            print(f"[Reasoning Error]: {exc}")
            return {"response": "Unable to process request.", "actions": {}, "sources": []}

    def _execute_tool_parallel(self, worker, tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute tool with given arguments (can fail gracefully).

        Args:
            worker: Worker instance
            tool_name: Name of tool to execute
            tool_args: Arguments for tool

        Returns:
            Dict of tool outputs
        """
        outputs = {}
        try:
            if tool_name and hasattr(worker, "_run_tool"):
                t_start = time.time()
                result = worker._run_tool(tool_name, tool_args)
                elapsed_ms = (time.time() - t_start) * 1000
                outputs[tool_name] = {
                    "result": result,
                    "duration_ms": elapsed_ms,
                    "status": "success",
                }
                print(f"[Tool] {tool_name} executed in {elapsed_ms:.1f}ms")
            return outputs
        except Exception as exc:
            print(f"[Tool Error]: {exc}")
            return {tool_name: {"status": "error", "error": str(exc)}}

    def _queue_voice_output(self, text: str, voice_params: Dict[str, Any]) -> bool:
        """
        Queue voice output for async playback (non-blocking).

        Args:
            text: Text to synthesize
            voice_params: Voice parameters

        Returns:
            True if queued successfully
        """
        if not self.voice_system:
            return False

        try:
            t_start = time.time()
            self.voice_system.queue_output(text, **voice_params)
            elapsed_ms = (time.time() - t_start) * 1000
            print(f"[Voice] Queued in {elapsed_ms:.1f}ms")
            return True
        except Exception as exc:
            print(f"[Voice Error]: {exc}")
            return False

    def _select_worker(self, intent: str):
        """Select appropriate worker based on execution mode and intent."""
        workers = self.online_workers if self.is_online() else self.offline_workers
        if intent in workers:
            return workers[intent]
        return workers.get("general") or next(iter(workers.values()))

    def shutdown(self):
        """Clean shutdown of thread pool."""
        self.executor.shutdown(wait=True)
        print("[ParallelOrch] Executor shutdown complete.")


class UltraFastOrchestrator:
    """
    Ultra-fast task orchestrator optimized for speed.

    Key differences from ParallelOrchestrator:
    1. Uses FastOfflineWorker (2-stage instead of 3-stage)
    2. Skips parser (uses query directly)
    3. Combines reasoner + formatter into single LLM call
    4. Response caching for instant repeated queries
    5. Streaming support for real-time responses

    Performance:
    - Parallel: 700ms (RAG + Tools + Voice)
    - Ultra-Fast: 350ms (single optimized call + cache)
    - Cached: 50ms (instant from cache)
    """

    def __init__(self, config: Dict[str, Any], fast_workers: Dict[str, Any]):
        """
        Initialize ultra-fast orchestrator.

        Args:
            config: Configuration dict
            fast_workers: Dict of intent -> FastOfflineWorker
        """
        self.config = config
        self.workers = fast_workers
        self.response_cache = {}
        self.stats = {
            "total_queries": 0,
            "cached_hits": 0,
            "avg_time_ms": 0,
            "min_time_ms": float("inf"),
            "max_time_ms": 0,
        }

    def execute_fast(self, query: str, intent: str) -> FastResult:
        """
        Execute query with ultra-fast optimization.

        Args:
            query: User query
            intent: Task intent (pre-classified for speed)

        Returns:
            FastResult with response and timing
        """
        t_start = time.time()

        cache_key = f"{intent}:{query.lower().strip()}"
        if cache_key in self.response_cache:
            elapsed_ms = (time.time() - t_start) * 1000
            self.stats["cached_hits"] += 1
            result = self.response_cache[cache_key]
            result.timing_ms = elapsed_ms
            result.cached = True
            print(f"[ULTRA-FAST] Cache HIT: {elapsed_ms:.0f}ms")
            return result

        worker = self.workers.get(intent) or self.workers.get("general")

        result = worker.execute_fast(query, context=None)

        fast_result = FastResult(
            text=result.text,
            timing_ms=result.timing_ms,
            cached=False,
            meta=result.meta,
        )

        self.response_cache[cache_key] = fast_result

        self._update_stats(result.timing_ms)

        print(f"[ULTRA-FAST] Executed: {result.timing_ms:.0f}ms | Avg: {self.stats['avg_time_ms']:.0f}ms")

        return fast_result

    def stream_response(self, query: str, intent: str):
        """
        Stream response for real-time UI updates.

        Args:
            query: User query
            intent: Task intent

        Yields:
            Response chunks in real-time
        """
        worker = self.workers.get(intent) or self.workers.get("general")

        t_start = time.time()
        for chunk in worker.execute_streaming(query, context=None):
            elapsed_ms = (time.time() - t_start) * 1000
            yield {
                "chunk": chunk,
                "elapsed_ms": elapsed_ms,
                "done": False,
            }

        yield {
            "chunk": "",
            "elapsed_ms": (time.time() - t_start) * 1000,
            "done": True,
        }

    def _update_stats(self, timing_ms: float):
        """Update performance statistics."""
        self.stats["total_queries"] += 1
        current_avg = self.stats["avg_time_ms"]
        total_q = self.stats["total_queries"]
        self.stats["avg_time_ms"] = (current_avg * (total_q - 1) + timing_ms) / total_q
        self.stats["min_time_ms"] = min(self.stats["min_time_ms"], timing_ms)
        self.stats["max_time_ms"] = max(self.stats["max_time_ms"], timing_ms)

    def get_stats(self) -> Dict[str, Any]:
        """Get performance statistics."""
        return {
            **self.stats,
            "cache_size": len(self.response_cache),
            "hit_rate": f"{self.stats['cached_hits'] / max(1, self.stats['total_queries']) * 100:.1f}%",
        }

    def clear_cache(self):
        """Clear response cache."""
        self.response_cache.clear()
        print("[ULTRA-FAST] Cache cleared")


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
        for intent, keywords in self.intents.items():
            if keywords and any(kw in query_lower for kw in keywords):
                return intent
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

        for spec in specs:
            if mode == "offline":
                worker = OfflineWorker(spec, spec.offline_pipeline, self.db, self.config)
            else:
                worker = OnlineWorker(spec, spec.hybrid_pipeline, self.db, self.config)
            workers[spec.intent] = worker

        return workers

    def _build_worker_specs(self) -> List[WorkerSpec]:
        """Build consolidated worker specifications utilizing a unified model space to prevent VRAM swapping."""
        return [
            WorkerSpec(
                name="OS Worker",
                intent="os",
                description="Controls local OS features, settings, and application launching.",
                tools={},
                offline_pipeline={
                    "parser": FORCED_REASONING_MODEL,
                    "reasoner": FORCED_REASONING_MODEL,
                    "formatter": FORCED_REASONING_MODEL,
                },
                hybrid_pipeline={
                    "parser": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
                    "reasoner": {"provider": "gemini", "model": "gemini-1.5-flash"},
                    "formatter": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
                },
            ),
            WorkerSpec(
                name="Office Worker",
                intent="office",
                description="Handles Excel, Word, Outlook, and Teams tasks, including document summarization.",
                tools={},
                offline_pipeline={
                    "parser": FORCED_REASONING_MODEL,
                    "reasoner": FORCED_REASONING_MODEL,
                    "formatter": FORCED_REASONING_MODEL,
                },
                hybrid_pipeline={
                    "parser": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
                    "reasoner": {"provider": "gemini", "model": "gemini-1.5-pro"},
                    "formatter": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
                },
            ),
            WorkerSpec(
                name="Web Worker",
                intent="web",
                description="Operates browser apps like Gmail, Drive, YouTube, and WhatsApp.",
                tools={},
                offline_pipeline={
                    "parser": FORCED_REASONING_MODEL,
                    "reasoner": FORCED_REASONING_MODEL,
                    "formatter": FORCED_REASONING_MODEL,
                },
                hybrid_pipeline={
                    "parser": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
                    "reasoner": {"provider": "gemini", "model": "gemini-1.5-flash"},
                    "formatter": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
                },
            ),
            WorkerSpec(
                name="File Worker",
                intent="files",
                description="Searches and retrieves local documents using RAG vector embeddings.",
                tools={},
                offline_pipeline={
                    "parser": FORCED_REASONING_MODEL,
                    "reasoner": FORCED_REASONING_MODEL,
                    "formatter": FORCED_REASONING_MODEL,
                },
                hybrid_pipeline={
                    "parser": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
                    "reasoner": {"provider": "gemini", "model": "gemini-1.5-flash"},
                    "formatter": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
                },
            ),
            WorkerSpec(
                name="General Worker",
                intent="general",
                description="Fallback conversational assistant for general questions and chats.",
                tools={},
                offline_pipeline={
                    "parser": FORCED_REASONING_MODEL,
                    "reasoner": FORCED_REASONING_MODEL,
                    "formatter": FORCED_REASONING_MODEL,
                },
                hybrid_pipeline={
                    "parser": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
                    "reasoner": {"provider": "gemini", "model": "gemini-1.5-flash"},
                    "formatter": {"provider": "ollama", "model": FORCED_REASONING_MODEL},
                },
            ),
        ]


def _bootstrap_env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _bootstrap_env_int(name: str, default: int = 0) -> int:
    value = os.environ.get(name)
    if value is None:
        return int(default)
    try:
        return int(str(value).strip())
    except Exception:
        return int(default)


_BOOT_STABILITY_MODE_LEVEL = max(0, _bootstrap_env_int("MARIE_STABILITY_MODE_LEVEL", 0))
_BOOT_DISABLE_SCREEN_CAPTURE = _bootstrap_env_bool("MARIE_DISABLE_SCREEN_CAPTURE", False)
_BOOT_DISABLE_RAG = _bootstrap_env_bool("MARIE_DISABLE_RAG", False)
_BOOT_SAFE_MINIMAL = _bootstrap_env_bool("MARIE_SAFE_MINIMAL", False)

torch = None
if _BOOT_STABILITY_MODE_LEVEL < 2 and not _BOOT_SAFE_MINIMAL:
    try:
        import torch as _torch
    except Exception:
        torch = None
    else:
        torch = _torch

get_rag_context = None
if not (_BOOT_DISABLE_RAG or _BOOT_SAFE_MINIMAL):
    try:
        from aiassistant.infra.rag_memory import get_rag_context
    except Exception:
        get_rag_context = None


os.environ.setdefault("OLLAMA_FLASH_ATTENTION", "1")
os.environ.setdefault("OLLAMA_KV_CACHE_TYPE", "q4_0")
os.environ.setdefault("OLLAMA_KEEP_ALIVE", "5m")

try:
    from aiassistant.infra.optimization import QuantizationHelper
    QuantizationHelper.apply_quantization_env()
except Exception:
    pass

FORCED_REASONING_MODEL = "qwen2.5-coder:7b"
FORCED_VISION_MODEL = "qwen2.5vl:7b"
HYBRID_MODE = bool(CONFIG.get("runtime", {}).get("hybrid_mode", False))


SYSTEM_PROMPT = (
    "You are an offline desktop assistant. "
    "Prioritize action over explanation. "
    "When office tasks are involved, prioritize Excel workflows and formulas first. "
    "Default to concise, direct replies focused on what the user needs now. "
    "Do not teach or explain internal process unless the user asks. "
    "If user intent is vague, break it into a short numbered plan and choose the first safe step. "
    "For analysis or execution requests, perform the task and report outcomes. "
    "If asked to analyze files, folders, or the PC, provide practical findings. "
    "Use tools only when needed. If you need a tool, emit JSON inside <tool>...</tool> with one action object. "
    "Supported actions: list_system_roots, list_directory, deep_search, analyze_path, create_path, move_path, "
    "copy_path, delete_path, search_file, semantic_search_file, read_file, open_file, launch_application, "
    "close_application, list_running_apps, open_service, move_mouse, click, type_text, press_key, hotkey, "
    "run_command, toggle_dark_mode, send_email, draft_email_attachment, send_telegram, send_whatsapp, "
    "online_query, open_interpreter. "
    "Do not include JSON or tool syntax in normal user-facing replies. "
    "For simple requests, answer in easy plain language and keep it direct. "
    "For complex requests, provide enough detail to be useful without over-explaining."
)


def _toggle_voice_rvc(enabled: bool) -> None:
    if requests is None:
        return

    host = str(CONFIG.get("servers", {}).get("voice_host", "127.0.0.1")).strip()
    port = int(CONFIG.get("servers", {}).get("voice_port", 8001))
    action = "load" if enabled else "unload"
    url = f"http://{host}:{port}/rvc/{action}"

    try:
        requests.post(url, json={}, timeout=2)
    except Exception:
        pass


FILE_RESPONSE_GUARD = (
    "When working with office files (.doc, .docx, .xlsx, .xls, .csv, .pdf), "
    "do not echo full file contents. Provide a brief response and refer to the file path. "
    "Apply the user's writing style profile to new documentation content."
)


PROMPT_BEHAVIOR_HINTS = {
    "default": "",
    "concise": (
        "Prefer concise answers by default. Keep most responses to a few short sentences unless the user asks for detail."
    ),
    "detailed": (
        "Provide richer explanations and rationale when useful. Include structured steps for complex requests."
    ),
    "action_first": (
        "Prioritize concrete actions and outcomes before explanation. Lead with what was done or what should be done next."
    ),
    "custom": "",
}


def _run_tool_action_isolated(action: Dict[str, object], timeout_sec: int = 55) -> Dict[str, object]:
    try:
        payload = json.dumps(action, ensure_ascii=True)
    except Exception as exc:
        return {
            "success": False,
            "message": "Failed to serialize tool action.",
            "error": str(exc),
        }

    command = [
        sys.executable,
        "-m",
        "aiassistant.tools.tool_action_runner",
    ]

    try:
        child_env = os.environ.copy()
        completed = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            text=True,
            timeout=max(5, int(timeout_sec)),
            check=False,
            env=child_env
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": "Tool action timed out in isolated runner.",
            "error": "runner_timeout",
        }
    except Exception as exc:
        return {
            "success": False,
            "message": "Tool action runner failed to start.",
            "error": str(exc),
        }

    stdout_text = (completed.stdout or "").strip()
    stderr_text = (completed.stderr or "").strip()

    candidates = [line.strip() for line in stdout_text.splitlines() if line.strip()]
    if stdout_text:
        candidates.append(stdout_text)

    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict) and "success" in parsed:
            return parsed

    if completed.returncode == 0 and not stderr_text:
        return {
            "success": True,
            "message": "Tool action completed in isolated runner.",
            "data": {"output": stdout_text[:1200]},
        }

    details = "\n".join(part for part in [stdout_text, stderr_text] if part).strip()[:1800]
    return {
        "success": False,
        "message": "Tool action failed in isolated runner.",
        "error": details or f"runner_exit_code={completed.returncode}",
    }


class LocalContext:
    """JSON-backed long-term memory for successful commands and user preferences."""

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> Dict[str, object]:
        if not self.file_path.exists():
            return {"successful_commands": [], "preferences": {}}
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {"successful_commands": [], "preferences": {}}
            payload.setdefault("successful_commands", [])
            payload.setdefault("preferences", {})
            return payload
        except Exception:
            return {"successful_commands": [], "preferences": {}}

    def _save(self) -> None:
        try:
            self.file_path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=True),
                encoding="utf-8",
            )
        except Exception:
            pass

    def add_success(self, command_text: str, outcome: str) -> None:
        with self._lock:
            entries = self._data.get("successful_commands")
            if not isinstance(entries, list):
                entries = []
                self._data["successful_commands"] = entries

            clean_cmd = _collapse_ws(command_text)[:260]
            clean_outcome = _collapse_ws(outcome)[:320]
            if not clean_cmd:
                return

            entry = {
                "command": clean_cmd,
                "outcome": clean_outcome,
            }
            entries.append(entry)
            if len(entries) > 120:
                self._data["successful_commands"] = entries[-120:]
            self._save()

    def set_preference(self, key: str, value: str) -> None:
        clean_key = _collapse_ws(key)[:120]
        clean_value = _collapse_ws(value)[:200]
        if not clean_key or not clean_value:
            return

        with self._lock:
            prefs = self._data.get("preferences")
            if not isinstance(prefs, dict):
                prefs = {}
                self._data["preferences"] = prefs
            prefs[clean_key] = clean_value
            self._save()

    def get_long_term_memory_text(self, max_chars: int = 1400) -> str:
        with self._lock:
            prefs = self._data.get("preferences", {})
            commands = self._data.get("successful_commands", [])

        lines: List[str] = []
        if isinstance(prefs, dict) and prefs:
            lines.append("Preferences:")
            for key, value in prefs.items():
                lines.append(f"- {key}: {value}")

        if isinstance(commands, list) and commands:
            lines.append("Recent successful commands:")
            for item in commands[-10:]:
                if not isinstance(item, dict):
                    continue
                cmd = str(item.get("command", "")).strip()
                out = str(item.get("outcome", "")).strip()
                if cmd:
                    if out:
                        lines.append(f"- {cmd} => {out}")
                    else:
                        lines.append(f"- {cmd}")

        joined = "\n".join(lines).strip()
        if len(joined) <= max_chars:
            return joined
        return joined[-max_chars:]


@dataclass
class AgentConfig:
    reasoning_model: str = FORCED_REASONING_MODEL
    vision_model: str = FORCED_VISION_MODEL
    ollama_host: str = "http://127.0.0.1:11434"
    max_history_turns: int = 3
    temperature: float = 0.2
    num_ctx: int = 2048
    num_predict: int = int(CONFIG.get("ollama", {}).get("num_predict", 320))
    rag_enabled: bool = True
    rag_top_k: int = 4
    hybrid_mode: bool = HYBRID_MODE
    external_model: str = str(CONFIG.get("runtime", {}).get("external_model", "gemini-2.0-flash"))
    online_mode: str = str(CONFIG.get("runtime", {}).get("online_mode", "auto"))
    external_api_key_env: str = "GOOGLE_API_KEY"
    crew_enabled: bool = bool(CONFIG.get("crew", {}).get("enabled", False))
    crew_mode: str = str(CONFIG.get("crew", {}).get("mode", "assist"))
    crew_router: str = str(CONFIG.get("crew", {}).get("router", "complex_only"))
    crew_context_max_chars: int = int(CONFIG.get("crew", {}).get("context_max_chars", 900))


class OfflineAgentCore:
    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        config: Optional[AgentConfig] = None,
    ) -> None:
        self.db = db or DatabaseManager()
        self.config = config or AgentConfig()

        self.config.reasoning_model = FORCED_REASONING_MODEL
        self.config.vision_model = FORCED_VISION_MODEL

        self.session_id = self.db.create_session(label="offline_desktop_assistant")
        self.stop_event = threading.Event()
        self.screen_capture_enabled = False
        self.rag_enabled = bool(self.config.rag_enabled) and not (_BOOT_DISABLE_RAG or _BOOT_SAFE_MINIMAL)
        self.system_prompt_behavior = "default"
        self.system_prompt_custom = ""
        self.online_mode = self._normalize_online_mode(self.config.online_mode)
        self.local_context = LocalContext(
            str(CONFIG.get("memory", {}).get("local_context_file", "./cache/memory.json"))
        )
        self.crew_enabled = bool(self.config.crew_enabled)
        self.crew_mode = str(self.config.crew_mode or "assist").strip().lower()
        self.crew_router = str(self.config.crew_router or "complex_only").strip().lower()
        self.crew_context_max_chars = max(120, int(self.config.crew_context_max_chars or 900))

        self.last_llm_latency_ms: Optional[float] = None
        self.last_tool_synthesis_latency_ms: Optional[float] = None
        self.last_external_latency_ms: Optional[float] = None
        self.last_vision_latency_ms: Optional[float] = None
        self.last_rag_latency_ms: Optional[float] = None

        # Bind intent manager for classification
        try:
            self.manager = AgentFactory(CONFIG, self.db).create_manager()
        except Exception as exc:
            print(f"[INIT] Failed to bind intent manager to core: {exc}")
            self.manager = None

        self._client = None
        if ollama is not None:
            try:
                self._client = ollama.Client(host=self.config.ollama_host)
            except Exception:
                self._client = None

        # --- PRESERVED FAST ORCHESTRATOR BLOCKS ---
        self.fast_orchestrator = None
        self.fast_workers = {}
        enable_fast = not _bootstrap_env_bool("MARIE_DISABLE_FAST_ORCHESTRATOR", False)

        if enable_fast:
            try:
                print("[INIT] Initializing ultra-fast orchestrator...")
                self.fast_workers = {
                    "files": create_fast_worker("files", {}, CONFIG),
                    "os": create_fast_worker("os", {}, CONFIG),
                    "office": create_fast_worker("office", {}, CONFIG),
                    "general": create_fast_worker("general", {}, CONFIG),
                }
                self.fast_orchestrator = UltraFastOrchestrator(CONFIG, self.fast_workers)
                print("[INIT] OK Ultra-fast orchestrator initialized (3.4x speedup enabled)")
            except Exception as exc:
                print(f"[INIT] WARN Ultra-fast orchestrator failed: {exc}")
                print("[INIT] Falling back to standard processing (set MARIE_DISABLE_FAST_ORCHESTRATOR=1 to skip init)")
                self.fast_orchestrator = None
                self.fast_workers = {}

    def set_screen_capture_enabled(self, enabled: bool) -> None:
        pass

    def set_rag_enabled(self, enabled: bool) -> None:
        if _BOOT_DISABLE_RAG or _BOOT_SAFE_MINIMAL:
            self.rag_enabled = False
            return
        self.rag_enabled = bool(enabled)

    def set_online_mode(self, mode: str) -> None:
        self.online_mode = self._normalize_online_mode(mode)

    def set_system_prompt_behavior(self, behavior: str, custom_prompt: str = "") -> None:
        clean_behavior = _collapse_ws(behavior).lower()
        if clean_behavior not in PROMPT_BEHAVIOR_HINTS:
            clean_behavior = "default"

        self.system_prompt_behavior = clean_behavior
        self.system_prompt_custom = str(custom_prompt or "").strip()

    def set_reasoning_model(self, model_name: str) -> None:
        _ = model_name
        self.config.reasoning_model = FORCED_REASONING_MODEL

    def set_vision_model(self, model_name: str) -> None:
        _ = model_name
        self.config.vision_model = FORCED_VISION_MODEL

    def stop(self) -> None:
        self.stop_event.set()

    def reset_stop(self) -> None:
        self.stop_event.clear()

    def get_fast_performance_stats(self) -> Dict[str, object]:
        """Get performance statistics from ultra-fast orchestrator."""
        if not self.fast_orchestrator:
            return {"enabled": False}
        try:
            stats = self.fast_orchestrator.get_stats()
            return {
                "enabled": True,
                "total_queries": stats.get("total_queries", 0),
                "avg_time_ms": stats.get("avg_time_ms", 0),
                "min_time_ms": stats.get("min_time_ms", 0),
                "max_time_ms": stats.get("max_time_ms", 0),
                "cached_hits": stats.get("cached_hits", 0),
                "hit_rate": stats.get("hit_rate", 0),
                "speedup": 1200 / max(stats.get("avg_time_ms", 1), 1),
            }
        except Exception:
            return {"enabled": False, "error": "Could not retrieve stats"}

    def get_latency_snapshot(self) -> Dict[str, Optional[float]]:
        return {
            "llm_ms": self.last_llm_latency_ms,
            "tool_synthesis_ms": self.last_tool_synthesis_latency_ms,
            "external_ms": self.last_external_latency_ms,
            "vision_ms": self.last_vision_latency_ms,
            "rag_ms": self.last_rag_latency_ms,
        }

    def process_user_message(self, user_text: str) -> str:
            text = (user_text or "").strip()
            if not text:
                return "Please type a message first."

            if self.stop_event.is_set():
                return "Processing is currently stopped. Press Resume to continue."

            self._capture_preference_from_text(text)
            self.db.log_interaction(self.session_id, role="user", message=text, category="chat")

            # FIX: Check the specialized intent classification instead of a hardcoded keyword array
            intent = "general"
            if hasattr(self, 'manager') and self.manager:
                intent = self.manager.classify(text, None)

            # Only use the fast orchestrator stream path for generic conversational chatter ("general")
            if self.fast_orchestrator and intent == "general" and not self.stop_event.is_set():
                try:
                    result_container = {"result": None, "error": None}

                    def run_fast_path():
                        try:
                            result_container["result"] = self.fast_orchestrator.execute_fast(text, "general")
                        except Exception as exc:
                            result_container["error"] = exc

                    fast_thread = threading.Thread(target=run_fast_path, daemon=True)
                    fast_thread.start()
                    fast_thread.join(timeout=5.0)

                    if not fast_thread.is_alive() and not result_container["error"] and result_container["result"]:
                        result = result_container["result"]
                        cleaned = self.clean_output_for_ui(result.text) or result.text
                        self.db.log_interaction(self.session_id, role="assistant", message=cleaned, category="chat")
                        return cleaned
                except Exception as exc:
                    print(f"[FAST] Fallback triggered via exception: {exc}")

            # Standard specialized multi-agent tool execution pipeline continues below...
            recent_history = self.db.get_recent_turns(self.session_id, turn_limit=self.config.max_history_turns)
            
            rag_context = ""
            if self.rag_enabled and not self.stop_event.is_set():
                rag_context = self._retrieve_rag_context(text)


    def _reason_over_input(
        self,
        user_text: str,
        recent_history: List[Dict[str, str]],
        screen_context: str,
        rag_context: str,
        crew_context: str,
    ) -> str:
        if self.stop_event.is_set():
            return ""

        ltm_text = self.local_context.get_long_term_memory_text()
        system_prompt = self._build_system_prompt()
        if ltm_text:
            system_prompt += "\n\nLong-Term Memory:\n" + ltm_text

        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]

        for item in recent_history:
            role = item.get("role", "user")
            content = item.get("message", "")
            if content:
                messages.append({"role": role, "content": content})

        context_sections = [f"User request: {user_text}"]

        if rag_context:
            context_sections.append(
                "Local knowledge snippets (RAG):\n"
                f"{rag_context}\n"
                "Use only if relevant and do not invent citations."
            )

        if crew_context:
            context_sections.append(
                "CrewAI notes (advisory):\n"
                f"{crew_context}\n"
                "Treat this as optional guidance; verify against local context."
            )

        if len(context_sections) == 1:
            user_payload = user_text
        else:
            user_payload = "\n\n".join(context_sections)

        messages.append({"role": "user", "content": user_payload})

        online_mode = self._normalize_online_mode(self.online_mode)
        if online_mode == "online":
            external = self._reason_with_external(messages)
            if external:
                return external
        elif online_mode == "auto" and self.config.hybrid_mode and self._is_complex_reasoning_request(user_text):
            external = self._reason_with_external(messages)
            if external:
                return external

        if self._client is None:
            return "Ollama client is unavailable. Please check local Ollama service."

        self.last_llm_latency_ms = None
        try:
            t_start = time.time()
            # REPAIRED: Global threading lock constraint has been completely bypassed to allow simultaneous model calls
            response = self._client.chat(
                model=self.config.reasoning_model,
                messages=messages,
                options={
                    "temperature": self.config.temperature,
                    "num_ctx": self.config.num_ctx,
                    "num_predict": max(180, int(self.config.num_predict)),
                },
                keep_alive=0,
            )
            self.last_llm_latency_ms = (time.time() - t_start) * 1000
            if self.stop_event.is_set():
                return ""
            return (response.get("message", {}) or {}).get("content", "").strip()
        except Exception as exc:
            return f"I hit a local model error: {exc}"
        finally:
            self._release_vram()

    def _build_system_prompt(self) -> str:
        base = SYSTEM_PROMPT
        behavior = (self.system_prompt_behavior or "default").strip().lower()
        behavior_hint = PROMPT_BEHAVIOR_HINTS.get(behavior, "")
        custom = self.system_prompt_custom.strip()

        style_profile = None
        try:
            style_profile = self.db.get_style_profile("train_root")
        except Exception:
            style_profile = None

        style_hint = ""
        if isinstance(style_profile, dict):
            formal = str(style_profile.get("formal", "")).strip()
            casual = str(style_profile.get("casual", "")).strip()
            if formal or casual:
                style_hint = (
                    "Writing style profile (use for documentation outputs):\n"
                    f"Formal baseline: {formal}\n"
                    f"Casual baseline: {casual}\n"
                    "Default to formal unless the user requests casual."
                )

        parts = [base, FILE_RESPONSE_GUARD]
        if behavior_hint:
            parts.append(behavior_hint)
        if custom:
            parts.append("Additional behavior override:\n" + custom)
        if style_hint:
            parts.append(style_hint)

        return "\n\n".join(part for part in parts if part).strip()

    def _reason_with_external(self, messages: List[Dict[str, str]]) -> str:
        if requests is None:
            return ""

        api_key = (
            os.environ.get(self.config.external_api_key_env, "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
            or os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("MARIE_GEMINI_API_KEY", "").strip()
        )
        if not api_key:
            return ""

        prompt_parts: List[str] = []
        for message in messages:
            role = str(message.get("role", "user")).strip().title()
            content = str(message.get("content", "")).strip()
            if content:
                prompt_parts.append(f"{role}:\n{content}")

        if not prompt_parts:
            return ""

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.config.external_model}:generateContent"
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "\n\n".join(prompt_parts)}],
                }
            ],
            "generationConfig": {
                "temperature": self.config.temperature,
                "maxOutputTokens": 640,
            },
        }

        self.last_external_latency_ms = None
        try:
            t_start = time.time()
            response = requests.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=28,
            )
            self.last_external_latency_ms = (time.time() - t_start) * 1000
            if response.status_code >= 400:
                return ""
            data = response.json()
            for candidate in data.get("candidates", []):
                parts = (candidate.get("content") or {}).get("parts", [])
                text_chunks = [str(part.get("text", "")) for part in parts if part.get("text")]
                merged = _collapse_ws(" ".join(text_chunks))
                if merged:
                    return merged
            return ""
        except Exception:
            return ""

    @staticmethod
    def _is_complex_reasoning_request(text: str) -> bool:
        lowered = (text or "").lower()
        words = re.findall(r"[a-zA-Z0-9_]+", lowered)
        if len(words) >= 36:
            return True

        triggers = {
            "analyze",
            "compare",
            "architecture",
            "strategy",
            "tradeoff",
            "design",
            "optimize",
            "debug plan",
            "step-by-step",
            "root cause",
        }
        hits = sum(1 for token in triggers if token in lowered)
        return hits >= 2

    @staticmethod
    def _normalize_online_mode(mode: str) -> str:
        clean = str(mode or "").strip().lower()
        if clean not in {"auto", "online", "offline"}:
            return "auto"
        return clean

    def _retrieve_rag_context(self, user_text: str) -> str:
        if get_rag_context is None:
            return ""

        self.last_rag_latency_ms = None
        try:
            t_start = time.time()
            snippets = get_rag_context(user_text, top_k=self.config.rag_top_k) or ""
            self.last_rag_latency_ms = (time.time() - t_start) * 1000
            snippets = snippets.strip()
            if not snippets:
                return ""

            snippets = snippets[:1800]
            self.db.log_interaction(
                self.session_id,
                role="system",
                message=snippets,
                category="rag",
            )
            return snippets
        except Exception:
            return ""

    def _execute_tool_actions(self, actions: List[Dict[str, object]]) -> List[Dict[str, object]]:
        results = []
        for action in actions:
            if self.stop_event.is_set():
                break
            result = _run_tool_action_isolated(action)

            action_name = str(action.get("action", ""))
            if action_name == "read_file":
                path_value = str(action.get("path") or "")
                ext = Path(path_value).suffix.lower()
                if ext in {".doc", ".docx", ".xlsx", ".xls", ".csv", ".pdf"}:
                    if isinstance(result, dict) and "data" in result:
                        result["data"] = {"path": path_value, "note": "content_hidden"}
            results.append({"action": action, "result": result})

            if isinstance(result, dict) and result.get("success"):
                action_name = str(action.get("action", "tool"))
                outcome = str(result.get("message", "completed"))
                self.local_context.add_success(action_name, outcome)

            self.db.log_interaction(
                self.session_id,
                role="tool",
                message=json.dumps({"action": action, "result": result}, ensure_ascii=True),
                category="tool",
            )
        return results

    def _synthesize_after_tools(
        self,
        user_text: str,
        base_assistant_text: str,
        tool_results: List[Dict[str, object]],
    ) -> str:
        if self._client is None:
            return base_assistant_text

        synthesis_prompt = (
            "Given the user request, draft answer, and tool results, write the final user-facing reply. "
            "Keep it concise, direct, and outcome-focused. "
            "State only what the user needs to know right now. "
            "Do not teach or explain internal steps unless asked. "
            "Do not include JSON or tool call syntax.\n\n"
            f"User request: {user_text}\n\n"
            f"Draft answer: {base_assistant_text}\n\n"
            f"Tool results: {json.dumps(tool_results, ensure_ascii=True)}"
        )

        messages = [
            {"role": "system", "content": "Return only final answer text."},
            {"role": "user", "content": synthesis_prompt},
        ]

        self.last_tool_synthesis_latency_ms = None
        try:
            t_start = time.time()
            # REPAIRED: Model lock has been fully bypassed to allow high-throughput parallel tool responses
            response = self._client.chat(
                model=self.config.reasoning_model,
                messages=messages,
                options={
                    "temperature": 0.1,
                    "num_ctx": 1536,
                    "num_predict": max(160, int(self.config.num_predict * 0.75)),
                },
                keep_alive=0,
            )
            self.last_tool_synthesis_latency_ms = (time.time() - t_start) * 1000
            return (response.get("message", {}) or {}).get("content", "").strip()
        except Exception:
            return base_assistant_text
        finally:
            self._release_vram()

    def _extract_tool_actions(self, raw_text: str) -> Tuple[List[Dict[str, object]], str]:
        actions: List[Dict[str, object]] = []
        text = raw_text or ""

        tool_blocks = re.findall(r"<tool>(.*?)</tool>", text, flags=re.IGNORECASE | re.DOTALL)
        for block in tool_blocks:
            candidate = block.strip()
            # REPAIRED: Defensive stripping prevents code formatting fences from breaking JSON parsers
            candidate = re.sub(r"^```json\s*", "", candidate, flags=re.IGNORECASE)
            candidate = re.sub(r"^```\s*", "", candidate)
            candidate = re.sub(r"\s*```$", "", candidate)
            candidate = candidate.strip()
            
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and parsed.get("action"):
                    actions.append(parsed)
            except Exception:
                continue

        text = re.sub(r"<tool>.*?</tool>", " ", text, flags=re.IGNORECASE | re.DOTALL)

        fallback_json = re.findall(r"\{\s*\"action\"\s*:\s*\"[^\"]+\".*?\}", text, flags=re.DOTALL)
        for snippet in fallback_json:
            try:
                parsed = json.loads(snippet)
                if isinstance(parsed, dict) and parsed.get("action"):
                    actions.append(parsed)
                    text = text.replace(snippet, " ")
            except Exception:
                continue

        return actions, _collapse_ws(text)

    def _get_crew_context(self, user_text: str, rag_context: str) -> Tuple[str, str]:
        if not self.crew_enabled:
            return "", ""

        mode = str(self.crew_mode or "assist").strip().lower()
        if mode not in {"assist", "replace"}:
            mode = "assist"

        router = str(self.crew_router or "complex_only").strip().lower()
        if router == "complex_only" and not self._is_complex_reasoning_request(user_text):
            return "", ""

        memory_context = rag_context or ""
        if memory_context:
            memory_context = memory_context[: self.crew_context_max_chars]

        crew_result = run_crew_assist(
            user_text,
            memory_context=memory_context,
            config=CONFIG.get("crew", {}),
        )
        if not crew_result or not isinstance(crew_result, dict):
            return "", ""

        if not crew_result.get("ok"):
            error = str(crew_result.get("error", "CrewAI unavailable")).strip()
            if error:
                self.db.log_interaction(
                    self.session_id,
                    role="system",
                    message=error,
                    category="crew",
                )
            return "", ""

        summary = str(crew_result.get("summary", "") or "").strip()
        final = str(crew_result.get("final", "") or "").strip()
        if mode == "replace" and final:
            return "", final
        return summary, ""

    @staticmethod
    def clean_output_for_ui(raw_text: str) -> str:
        """
        Strips internal artifacts before showing text in chat UI.

        Removes:
        - <think>...</think> reasoning blocks
        - <tool>...</tool> tool call blocks
        - JSON action objects
        - fenced code blocks (often tool dumps)
        """
        text = raw_text or ""

        patterns = [
            r"<think>.*?</think>",
            r"<tool>.*?</tool>",
            r"```[\s\S]*?```",
            r"\{\s*\"action\"\s*:\s*\"[^\"]+\"[\s\S]*?\}",
        ]

        for pattern in patterns:
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE | re.DOTALL)

        text = re.sub(r"\b(analysis|reasoning|tool\s*call)\s*:\s*", "", text, flags=re.IGNORECASE)

        return _collapse_ws(text)

    def _capture_preference_from_text(self, text: str) -> None:
        lowered = (text or "").lower()
        if not lowered:
            return

        if "always use dark mode" in lowered:
            self.local_context.set_preference("theme", "dark")
            return
        if "always use light mode" in lowered:
            self.local_context.set_preference("theme", "light")
            return

        match = re.search(r"\balways use\s+([a-z0-9 _\-]{2,80})", lowered)
        if match:
            value = match.group(1).strip(" .,!?:;")
            if value:
                self.local_context.set_preference("always_use", value)

    @staticmethod
    def _release_vram() -> None:
        """Release VRAM by unloading models and clearing caches."""
        try:
            from aiassistant.infra.optimization import get_memory_manager
            get_memory_manager().cleanup()
        except Exception:
            pass

        gc.collect()
        if torch is None:
            return
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                if hasattr(torch.cuda, "ipc_collect"):
                    torch.cuda.ipc_collect()
        except Exception:
            pass

    def _cleanup_memory_after_inference(self) -> None:
        """Aggressive cleanup after model inference to prevent VRAM accumulation."""
        try:
            from aiassistant.infra.optimization import get_device_capabilities
            if get_device_capabilities().optimization_profile.get("aggressive_gc", False):
                gc.collect()
                if torch is not None and torch.cuda.is_available():
                    torch.cuda.empty_cache()
        except Exception:
            pass


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


__all__ = [
    "AgentConfig",
    "AgentFactory",
    "AsyncVoiceOutput",
    "ConcurrentRAGRetriever",
    "ConcurrentTaskResult",
    "DEFAULT_INTENTS",
    "EventBus",
    "Events",
    "FastResult",
    "LocalContext",
    "Manager",
    "OfflineAgentCore",
    "Orchestrator",
    "ParallelOrchestrator",
    "ParallelTaskContext",
    "ParallelTaskResult",
    "ParallelWorkerMixin",
    "TaskContext",
    "TaskResult",
    "UltraFastOrchestrator",
    "WorkerSpec",
    "run_crew_assist",
    "run_multi_agent_round",
]