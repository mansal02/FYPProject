# ============================================================================
# PARALLEL ORCHESTRATOR MODULE
# ============================================================================
# Optimized orchestrator with concurrent execution for RAG, tools, and voice.
# Processes: user input -> parser | -> [RAG + Tools] --parallel-> voice
# ============================================================================

import asyncio
import json
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import threading
import time


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
    timing: Dict[str, float] = field(default_factory=dict)


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
        
        # Thread pool for concurrent operations
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
        return self._ping(self.config.get("network", {}).get("ping_host", "8.8.8.8"))

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

        # Track timing
        t_start = time.time()
        context.timing["start"] = t_start

        # === STAGE 1: PARSE (Sequential - dependency) ===
        t_parse_start = time.time()
        intent = self.manager.classify(query, context)
        worker = self._select_worker(intent)
        parsed_query = self._parse_query(worker, query)
        context.timing["parse_ms"] = (time.time() - t_parse_start) * 1000

        print(f"[ParallelOrch] Intent: {intent} | Parsed: {parsed_query[:60]}...")

        # === STAGE 2: PARALLEL EXECUTION (RAG + Reasoning + Tools) ===
        t_parallel_start = time.time()
        futures = {}
        
        # Task 1: Retrieve RAG context
        if self.rag_system and not self.config.get("disable_rag"):
            futures["rag"] = self.executor.submit(
                self._retrieve_rag_context, 
                parsed_query, 
                query
            )

        # Task 2: Run worker reasoning (tool determination)
        futures["reasoning"] = self.executor.submit(
            self._run_worker_reasoning,
            worker,
            parsed_query,
            query,
            context
        )

        # Collect parallel results
        parallel_results = {}
        parallel_errors = {}
        for task_name, future in futures.items():
            try:
                parallel_results[task_name] = future.result(timeout=30)
            except Exception as e:
                parallel_errors[task_name] = str(e)
                print(f"[ParallelOrch] {task_name} error: {e}")
                parallel_results[task_name] = None

        context.timing["parallel_ms"] = (time.time() - t_parallel_start) * 1000
        context.parallel_results = parallel_results

        # === STAGE 3: MERGE RESULTS ===
        t_merge_start = time.time()
        rag_context = parallel_results.get("rag") or ""
        reasoning_result = parallel_results.get("reasoning") or {}
        
        # Inject RAG context into reasoning if available
        if rag_context and reasoning_result:
            reasoning_result["rag_context"] = rag_context

        context.timing["merge_ms"] = (time.time() - t_merge_start) * 1000

        # === STAGE 4: FORMAT AND EXECUTE TOOLS ===
        t_tools_start = time.time()
        tool_outputs = {}
        if reasoning_result.get("tool_name") and reasoning_result.get("tool_name") in worker.tools:
            tool_outputs = self._execute_tool_parallel(
                worker,
                reasoning_result.get("tool_name"),
                reasoning_result.get("tool_args", {})
            )
        context.timing["tools_ms"] = (time.time() - t_tools_start) * 1000

        # === STAGE 5: ASYNC VOICE OUTPUT (Non-blocking) ===
        response_text = reasoning_result.get("response", "Task completed.")
        voice_future = None
        if self.voice_system:
            voice_future = self.executor.submit(
                self._queue_voice_output,
                response_text,
                reasoning_result.get("voice_params", {})
            )

        # Log interaction
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
                }
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
        if not hasattr(worker, '_call_model'):
            return query
        try:
            parser_cfg = worker.pipeline.get("parser")
            sys_parser = "Extract the core intent and parameters. Output ONLY the refined instruction."
            parsed = worker._call_model(parser_cfg, query, sys_parser)
            return parsed.strip() if parsed else query
        except Exception as e:
            print(f"[Parse Error]: {e}")
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
            # Try both parsed and original for better coverage
            context_snippets = self.rag_system.get_rag_context(parsed_query, limit=5)
            if not context_snippets:
                context_snippets = self.rag_system.get_rag_context(original_query, limit=5)
            
            elapsed_ms = (time.time() - t_start) * 1000
            print(f"[RAG] Retrieved context in {elapsed_ms:.1f}ms: {len(context_snippets)} snippets")
            return context_snippets
        except Exception as e:
            print(f"[RAG Error]: {e}")
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
        try:
            t_start = time.time()
            
            # Run the worker's standard reasoning pipeline
            if hasattr(worker, '_call_model'):
                # 3-stage pipeline: Reasoner -> Formatter -> Extract JSON
                reasoner_cfg = worker.pipeline.get("reasoner")
                sys_reasoner = f"You are {worker.spec.name}. {worker.spec.description} Analyze and determine exact steps."
                reasoning = worker._call_model(reasoner_cfg, parsed_query, sys_reasoner)
                
                formatter_cfg = worker.pipeline.get("formatter")
                sys_formatter = "Format as strict JSON: {'tool': string, 'args': dict, 'response': string}."
                formatted_json = worker._call_model(formatter_cfg, reasoning, sys_formatter)
                
                # Parse JSON
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
            else:
                return {"response": "Worker reasoning unavailable.", "actions": {}, "sources": []}
                
        except Exception as e:
            print(f"[Reasoning Error]: {e}")
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
            if tool_name and hasattr(worker, '_run_tool'):
                t_start = time.time()
                result = worker._run_tool(tool_name, tool_args)
                elapsed_ms = (time.time() - t_start) * 1000
                outputs[tool_name] = {
                    "result": result,
                    "duration_ms": elapsed_ms,
                    "status": "success"
                }
                print(f"[Tool] {tool_name} executed in {elapsed_ms:.1f}ms")
            return outputs
        except Exception as e:
            print(f"[Tool Error]: {e}")
            return {tool_name: {"status": "error", "error": str(e)}}

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
        except Exception as e:
            print(f"[Voice Error]: {e}")
            return False

    def _select_worker(self, intent: str):
        """Select appropriate worker based on execution mode and intent."""
        workers = self.online_workers if self.is_online() else self.offline_workers
        if intent in workers:
            return workers[intent]
        return workers.get("general") or next(iter(workers.values()))

    @staticmethod
    def _ping(host: str) -> bool:
        """Check internet connectivity."""
        try:
            socket.create_connection((host, 53), timeout=1.0)
            return True
        except OSError:
            return False

    def shutdown(self):
        """Clean shutdown of thread pool."""
        self.executor.shutdown(wait=True)
        print("[ParallelOrch] Executor shutdown complete.")
