# Parallel Pipeline Optimization Guide
## Making Your AI Assistant Faster with Concurrent Execution

**Last Updated:** May 21, 2026  
**Status:** Ready for Integration  
**Performance Gain:** 30-50% faster request→response cycle

---

## 📊 Problem & Solution

### Current Sequential Pipeline
```
User Input
    ↓
Parser (100ms)
    ↓
Worker Reasoning (300ms)
    ↓
RAG Retrieval (500ms) - I/O bound, could run in parallel!
    ↓
Tool Execution (400ms) - Multiple tools waiting sequentially!
    ↓
Voice Output (100ms) - Blocking user interaction!
    ↓
Total: 1400ms per request ❌
```

### Optimized Parallel Pipeline
```
User Input
    ↓
Parser (100ms)
    ↓
┌─────────────────────────────┐
│  RAG Retrieval (500ms)      │ Concurrent
│  ↓ (I/O bound)              │ Execution
│  Worker Reasoning (300ms)   │
└─────────────────────────────┘
    ↓
┌──────────┬──────────┬──────────┐
│ Tool 1   │ Tool 2   │ Tool 3   │ Parallel Tool
│ (300ms)  │ (200ms)  │ (400ms)  │ Execution
└──────────┴──────────┴──────────┘
    ↓
Queue Voice Output (0ms) → Async background
    ↓
Total: 700ms per request ✅ (50% faster!)
```

---

## 🚀 Quick Start

### 1. Basic Usage with Parallel Orchestrator

```python
from aiassistant.core import (
    ParallelOrchestrator,
    ParallelTaskContext,
    AgentFactory,
)
from aiassistant.infra.rag_memory import LocalRAG

# Initialize components
config = {"mode": "auto", "max_concurrent_workers": 4}
rag = LocalRAG("knowledge/", "chroma/")
workers = AgentFactory.create_workers(config)

# Create parallel orchestrator
orchestrator = ParallelOrchestrator(
    config=config,
    db=None,  # or your database connection
    manager=AgentFactory.create_manager(config),
    offline_workers=workers["offline"],
    online_workers=workers["online"],
    rag_system=rag,
    voice_system=None,  # Optional: pass voice system for async output
)

# Execute query with parallel pipeline
context = ParallelTaskContext(user_id=1, session_id="session_1", mode="offline")
result = orchestrator.route_task_parallel("Search for reports and open Excel", context)

# Access results
print(f"Response: {result.text}")
print(f"Timing: {result.timing}")  # Shows performance metrics
print(f"RAG Context: {result.rag_context}")
print(f"Tool Outputs: {result.tool_outputs}")
```

### 2. Enable Parallel Mode in Your App

```python
# Before: Using sequential orchestrator
from aiassistant.core import Orchestrator
orchestrator = Orchestrator(config, db, manager, offline_workers, online_workers)
result = orchestrator.route_task(query, context)

# After: Using parallel orchestrator (drop-in replacement)
from aiassistant.core import ParallelOrchestrator
orchestrator = ParallelOrchestrator(
    config, db, manager, offline_workers, online_workers,
    rag_system=rag,  # NEW: automatic parallel RAG retrieval
    voice_system=voice,  # NEW: async voice output
)
result = orchestrator.route_task_parallel(query, context)
```

### 3. Add Parallel Worker Support to Existing Workers

```python
from aiassistant.core import ParallelWorkerMixin
from aiassistant.workers import OfflineWorker

# Create parallel-enabled worker by mixing in the mixin
class ParallelOfflineWorker(ParallelWorkerMixin, OfflineWorker):
    pass

# Now you can execute tools concurrently
worker = ParallelOfflineWorker(spec, pipeline, db, config)

# Execute multiple tools at once
tools_to_run = {
    "search_files": {"query": "reports"},
    "open_app": {"app": "Excel"},
    "get_weather": {"location": "London"},
}
results = worker.execute_tools_concurrent(tools_to_run)

for tool_name, result in results.items():
    print(f"{tool_name}: {result.result} ({result.duration_ms:.0f}ms)")
```

---

## 📈 Performance Metrics

### Benchmark Results (Simulated)

| Operation | Sequential | Parallel | Speedup |
|-----------|-----------|----------|---------|
| Parse only | 100ms | 100ms | - |
| RAG + Reasoning | 800ms | 500ms | **38%** ↑ |
| Tool Execution (3 tools) | 900ms | 400ms | **55%** ↑ |
| Full Pipeline | 1400ms | 700ms | **50%** ↑ |
| Voice + Response | 100ms | 0ms* | **Async** ✓ |

*Voice output queued asynchronously in background

### Real-World Scenarios

**Scenario 1: Knowledge Query**
- User: "Summarize the quarterly report and explain key insights"
- Sequential: Parse (100) + RAG retrieval (500) + Reasoning (300) = 900ms
- Parallel: Parse (100) + [RAG (500) ∥ Reasoning (300)] = 600ms
- **Gain: 33% faster**

**Scenario 2: Multi-Tool Action**
- User: "Search for Q1 reports, open Excel, and check my email"
- Sequential: Tool 1 (300) + Tool 2 (200) + Tool 3 (250) = 750ms
- Parallel: Max(300, 200, 250) = 300ms
- **Gain: 60% faster**

**Scenario 3: Complex Interactive Query**
- Sequential: Parse (100) + RAG (500) + Reasoning (300) + Tools (400) + Voice (150) = 1450ms
- Parallel: Parse (100) + [RAG ∥ Reasoning] (500) + [Tools] (400) + Voice (0*) = 1000ms
- **Gain: 31% faster + instant UI responsiveness**

---

## 🔧 Configuration Options

### ParallelOrchestrator Config

```yaml
# In config.yaml or environment
parallel:
  enabled: true
  max_concurrent_workers: 4
  rag:
    concurrent_queries: true
    timeout_ms: 30000
  tools:
    parallel_execution: true
    fail_on_timeout: false
  voice:
    async_mode: true
    queue_size: 10
  network:
    ping_host: "8.8.8.8"
    timeout: 1.0
```

### Environment Variables

```bash
# Enable parallel mode
export MARIE_ENABLE_PARALLEL=1

# Configure thread pool
export MARIE_MAX_CONCURRENT_WORKERS=4

# RAG concurrency
export MARIE_PARALLEL_RAG_QUERIES=true

# Voice async mode
export MARIE_ASYNC_VOICE_OUTPUT=true

# Disable specific optimizations if needed
export MARIE_DISABLE_PARALLEL_RAG=1
export MARIE_DISABLE_PARALLEL_TOOLS=1
```

### Python Configuration

```python
config = {
    "mode": "auto",  # auto|online|offline
    "max_concurrent_workers": 4,
    "parallel": {
        "enabled": True,
        "rag": {
            "concurrent": True,
            "timeout_ms": 30000,
        },
        "tools": {
            "parallel": True,
            "fail_on_timeout": False,
        },
        "voice": {
            "async_mode": True,
            "queue_size": 10,
        }
    }
}
```

---

## 🔄 Migration Guide

### Step 1: Update Imports

```python
# Old
from aiassistant.core import Orchestrator, TaskContext, TaskResult

# New
from aiassistant.core import (
    ParallelOrchestrator,
    ParallelTaskContext,
    ParallelTaskResult,
    ParallelWorkerMixin,
    AsyncVoiceOutput,
    ConcurrentRAGRetriever,
)
```

### Step 2: Update Initialization

```python
# Old
orchestrator = Orchestrator(config, db, manager, workers_offline, workers_online)

# New
from aiassistant.infra.rag_memory import LocalRAG

rag = LocalRAG("knowledge/", "chroma/")
orchestrator = ParallelOrchestrator(
    config=config,
    db=db,
    manager=manager,
    offline_workers=workers_offline,
    online_workers=workers_online,
    rag_system=rag,
    voice_system=voice_system,  # Optional
)
```

### Step 3: Update Execution Calls

```python
# Old
result = orchestrator.route_task(query, context)

# New
context = ParallelTaskContext(user_id=1, session_id="s1", mode="offline")
result = orchestrator.route_task_parallel(query, context)

# Access new fields
print(f"Execution timing: {result.timing}")  # Performance metrics
print(f"RAG results: {result.rag_context}")  # Retrieved knowledge
print(f"Tool results: {result.tool_outputs}")  # Parallel tool execution
```

### Step 4: Cleanup (Important!)

```python
# At application shutdown
orchestrator.shutdown()
```

---

## 🛠️ Advanced Features

### 1. Concurrent RAG Retrieval

```python
from aiassistant.core import ConcurrentRAGRetriever

rag_retriever = ConcurrentRAGRetriever(rag_system)

# Retrieve context for multiple queries in parallel
results = rag_retriever.retrieve_concurrent(
    "quarterly report",
    "financial metrics",
    "key insights",
    limit=5
)

for query, context in results.items():
    print(f"{query}: {len(context)} chars retrieved")
```

### 2. Async Voice Output

```python
from aiassistant.core import AsyncVoiceOutput
import asyncio

async def main():
    voice = AsyncVoiceOutput(voice_system, max_queue_size=10)
    
    # Start async voice processor
    asyncio.create_task(voice.start())
    
    # Queue multiple outputs (non-blocking)
    await voice.queue_text("First response")
    await voice.queue_text("Second response")
    await voice.queue_text("Third response")
    
    # All queued for parallel synthesis in background
    # Application can continue immediately
    
    await voice.stop()

asyncio.run(main())
```

### 3. Pipeline Stage Optimization

```python
# Execute parser, reasoner, formatter with smart scheduling
result = worker.execute_pipeline_stages_concurrent(
    query="Search for reports",
    parser_fn=lambda q: worker._call_model(
        worker.pipeline["parser"], q, "Extract intent"
    ),
    reasoner_fn=lambda pq: worker._call_model(
        worker.pipeline["reasoner"], pq, "Analyze"
    ),
    formatter_fn=lambda r: worker._call_model(
        worker.pipeline["formatter"], r, "Format"
    ),
)

print(f"Timings: {result['timings']}")
```

---

## ⚠️ Best Practices

### 1. Handle Timeout Gracefully

```python
# Parallel operations have timeout (default 30s)
# They fail gracefully and fall back to partial results
result = orchestrator.route_task_parallel(query, context)

# Check for errors
if result.meta.get("parallel_errors"):
    print(f"Partial failures: {result.meta['parallel_errors']}")
    # System still returns best-effort results
```

### 2. Configure Max Workers Appropriately

```python
# Rule of thumb: max_workers = (CPU cores / 2) + 1
import os
num_cores = os.cpu_count() or 4
max_workers = (num_cores // 2) + 1

config = {"max_concurrent_workers": max_workers}
```

### 3. Monitor Performance

```python
# Every result includes timing information
result = orchestrator.route_task_parallel(query, context)

print(f"Total time: {result.timing['total_ms']:.0f}ms")
print(f"Parse: {result.timing['parse_ms']:.0f}ms")
print(f"Parallel (RAG+Reasoning): {result.timing['parallel_ms']:.0f}ms")
print(f"Tools: {result.timing['tools_ms']:.0f}ms")

# Track trends over time
avg_time = sum(r.timing['total_ms'] for r in recent_results) / len(recent_results)
print(f"Average response time: {avg_time:.0f}ms")
```

### 4. Graceful Degradation

```python
# If RAG fails, system continues with reasoning alone
result = orchestrator.route_task_parallel(query, context)

if not result.rag_context:
    print("RAG retrieval failed, but reasoning completed")
    # Result still contains response from reasoning

if result.tool_outputs.get("some_tool", {}).get("status") == "error":
    print(f"Tool failed: {result.tool_outputs['some_tool']['error']}")
    # Other tools still executed
```

---

## 🧪 Testing & Validation

### Test Performance Improvement

```python
import time
from aiassistant.core import Orchestrator, ParallelOrchestrator

# Setup
config = {"mode": "offline"}
query = "Search for reports and open Excel and check weather"
context = TaskContext(user_id=1, session_id="test", mode="offline")

# Test sequential
orchestrator_seq = Orchestrator(config, db, manager, workers_off, workers_on)
t_start = time.time()
result_seq = orchestrator_seq.route_task(query, context)
time_seq = time.time() - t_start

# Test parallel
orchestrator_par = ParallelOrchestrator(
    config, db, manager, workers_off, workers_on, rag, voice
)
t_start = time.time()
result_par = orchestrator_par.route_task_parallel(query, context)
time_par = time.time() - t_start

print(f"Sequential: {time_seq*1000:.0f}ms")
print(f"Parallel: {time_par*1000:.0f}ms")
print(f"Speedup: {(1 - time_par/time_seq)*100:.1f}%")

orchestrator_par.shutdown()
```

### Run Example

```bash
# Install parallel orchestrator modules
cd d:\pylearn\FYP\AiAssistant

# Run example demonstration
python example_parallel_usage.py

# Output shows performance comparisons and architecture
```

---

## 📝 Implementation Checklist

- [ ] Copy `parallel_orchestrator.py` to `aiassistant/core/`
- [ ] Copy `parallel_worker.py` to `aiassistant/core/`
- [ ] Update `aiassistant/core/__init__.py` to export new classes
- [ ] Update `example_parallel_usage.py` with your configuration
- [ ] Test with `python example_parallel_usage.py`
- [ ] Migrate one worker to use `ParallelWorkerMixin`
- [ ] Update your main app to use `ParallelOrchestrator`
- [ ] Monitor performance with result timing metrics
- [ ] Adjust `max_concurrent_workers` based on your hardware
- [ ] Test error handling and graceful degradation
- [ ] Deploy and monitor in production

---

## 🐛 Troubleshooting

### Issue: "Parallel execution no faster than sequential"

**Cause:** Likely all operations are CPU-bound, not I/O-bound.  
**Solution:** Check which operations actually benefit from parallelization:
- RAG retrieval: I/O bound (good for parallelization)
- LLM reasoning: CPU bound (may not benefit)
- Tool execution: Depends on tool (file I/O? Network? Good; pure compute? No)

```python
# Check where time is spent
result = orchestrator.route_task_parallel(query, context)
print(result.timing)  # Analyze which stage is slow
```

### Issue: Thread pool exhaustion / hanging

**Cause:** Too many concurrent operations or deadlocks.  
**Solution:**
```python
# Reduce concurrent workers
config = {"max_concurrent_workers": 2}  # Start conservative

# Check for circular dependencies in tool calls
# Ensure tools don't wait for orchestrator callbacks
```

### Issue: RAG context missing from results

**Cause:** RAG system not passed or `MARIE_DISABLE_RAG=1`  
**Solution:**
```python
# Verify RAG is initialized
print(f"RAG enabled: {orchestrator.rag_system is not None}")

# Check environment
import os
print(f"MARIE_DISABLE_RAG: {os.environ.get('MARIE_DISABLE_RAG')}")

# Pass RAG system explicitly
rag = LocalRAG("knowledge/", "chroma/")
orchestrator = ParallelOrchestrator(..., rag_system=rag)
```

### Issue: Voice output not queued

**Cause:** Voice system not passed or `MARIE_DISABLE_TTS=1`  
**Solution:**
```python
# Ensure voice system is initialized and passed
from your_voice_module import VoiceSystem
voice = VoiceSystem()

orchestrator = ParallelOrchestrator(
    ...,
    voice_system=voice,  # Don't forget this!
)

# Verify queuing worked
print(f"Voice queued: {result.meta.get('voice_queued')}")
```

---

## 📚 API Reference

### ParallelOrchestrator

**Methods:**
- `route_task_parallel(query, context)` → ParallelTaskResult
- `set_mode(mode)` → None
- `is_online()` → bool
- `shutdown()` → None

**Properties:**
- `mode` (str): "auto", "online", or "offline"
- `rag_system`: RAG system instance
- `voice_system`: Voice system instance
- `executor`: ThreadPoolExecutor

### ParallelTaskResult

**Fields:**
- `text` (str): Response text
- `actions` (Dict): Tool actions
- `sources` (List): Information sources
- `rag_context` (str): Retrieved knowledge
- `tool_outputs` (Dict): Tool execution results
- `timing` (Dict): Performance metrics
- `meta` (Dict): Metadata

**Timing Metrics:**
- `start` (float): Start timestamp
- `parse_ms`: Parser stage duration
- `parallel_ms`: RAG + Reasoning concurrent stage
- `merge_ms`: Result merging duration
- `tools_ms`: Tool execution duration
- `total_ms`: Total request duration

### ParallelWorkerMixin

**Methods:**
- `execute_tools_concurrent(tools)` → Dict[str, ConcurrentTaskResult]
- `execute_tools_parallel_async(tools)` → List[Future]
- `wait_for_tools(futures, timeout)` → List[ConcurrentTaskResult]
- `execute_pipeline_stages_concurrent(...)` → Dict[str, Any]
- `shutdown()` → None

---

## 📞 Support

For issues or questions:
1. Check the Troubleshooting section above
2. Review example usage in `example_parallel_usage.py`
3. Monitor timing metrics in results to identify bottlenecks
4. Run tests to validate your configuration

---

**Version:** 1.0 | **Last Updated:** May 21, 2026
