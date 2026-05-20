# ============================================================================
# PARALLEL WORKER ENHANCEMENT
# ============================================================================
# Adds concurrent execution support to existing worker classes.
# Enables parallel tool execution and async RAG retrieval.
# ============================================================================

import asyncio
import concurrent.futures
import time
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass


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
                    tool_args
                )
                futures[tool_name] = future
        
        # Collect results as they complete
        for tool_name, future in concurrent.futures.as_completed(futures, timeout=30):
            try:
                results[tool_name] = future.result()
            except Exception as e:
                results[tool_name] = ConcurrentTaskResult(
                    tool_name=tool_name,
                    result=None,
                    duration_ms=0,
                    status="error",
                    error=str(e)
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
                    tool_args
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
                status="success"
            )
        except Exception as e:
            duration_ms = (time.time() - t_start) * 1000
            return ConcurrentTaskResult(
                tool_name=tool_name,
                result=None,
                duration_ms=duration_ms,
                status="error",
                error=str(e)
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
        Parser runs first (dependency), then reasoner & formatter can run concurrently on parsed output.
        
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
        
        # Stage 2: Reasoner & Formatter can run in parallel on parsed_query
        t_start = time.time()
        reasoner_future = self.executor.submit(reasoner_fn, parsed_query)
        formatter_on_query_future = self.executor.submit(formatter_fn, query)  # Also try on original
        
        try:
            reasoning_result = reasoner_future.result(timeout=30)
            timings["reasoner_ms"] = (time.time() - t_start) * 1000
        except Exception as e:
            print(f"[Reasoner Error]: {e}")
            reasoning_result = ""
            timings["reasoner_ms"] = (time.time() - t_start) * 1000
        
        # Now run formatter on reasoning result
        t_start = time.time()
        formatter_future = self.executor.submit(formatter_fn, reasoning_result)
        try:
            formatted_result = formatter_future.result(timeout=30)
            timings["formatter_ms"] = (time.time() - t_start) * 1000
        except Exception as e:
            print(f"[Formatter Error]: {e}")
            formatted_result = reasoning_result
            timings["formatter_ms"] = (time.time() - t_start) * 1000
        
        return {
            "parsed_query": parsed_query,
            "reasoning": reasoning_result,
            "formatted": formatted_result,
            "timings": timings
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
                # Run voice synthesis in thread pool to not block
                await self._synthesize_and_play(text, params)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[AsyncVoice Error]: {e}")

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
            # Run in thread pool to not block event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                self.executor,
                lambda: self.voice_system.queue_output(text, **params)
            )
        except Exception as e:
            print(f"[AsyncVoice Error]: {e}")

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
            # Check cache first
            cache_key = f"{query}:{limit}"
            if cache_key in self._cache:
                results[query] = self._cache[cache_key]
                continue
            
            # Submit concurrent retrieval
            future = self.executor.submit(
                self._retrieve_and_cache,
                query,
                limit,
                cache_key
            )
            futures[query] = future
        
        # Collect results
        for query, future in futures.items():
            try:
                results[query] = future.result(timeout=30)
            except Exception as e:
                print(f"[RAG Error for '{query}']: {e}")
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
        except Exception as e:
            print(f"[RAG Retrieval Error]: {e}")
            return ""

    def shutdown(self):
        """Clean shutdown."""
        self.executor.shutdown(wait=True)
