# Worker/Orchestrator Integration Guide

## What Was Implemented

I've successfully integrated the worker/orchestrator pattern from the Marie_02 project into your AiAssistant project. Here's what was added:

### New Files Created

1. **`aiassistant/workers/offline_worker.py`** (282 lines)
   - `OfflineLLMClient`: Communicates with local Ollama LLMs
   - `BaseWorker`: Base class with 3-stage pipeline execution
   - `OfflineWorker`: Full offline implementation using only local models
   - Helper functions: Intent parsing, email extraction, confirmation parsing

2. **`aiassistant/workers/online_worker.py`** (67 lines)
   - `OnlineLLMClient`: Calls Google Gemini API
   - `OnlineWorker`: Hybrid pipeline supporting both Gemini and Ollama per stage

3. **`aiassistant/core/agent_factory.py`** (245 lines)
   - `AgentFactory`: Creates workers and manager
   - `Manager`: Classifies queries into intents using keywords
   - `WorkerSpec`: Dataclass defining worker configurations
   - Pre-configured specs for: OS, Office, Web, Files, General intents

4. **`aiassistant/core/orchestrator_new.py`** (172 lines)
   - `Orchestrator`: Main routing engine for tasks
   - `TaskContext`: Task metadata container
   - `TaskResult`: Response container with actions and metadata
   - Auto-detection of online/offline mode

5. **`example_worker_usage.py`** (Example file)
   - Shows how to use the new system
   - Includes classification, routing, and direct worker examples

## Architecture Overview

### Pipeline Stages

```
User Query
    ↓
[Parser] - Extract core intent (qwen2.5:0.5b)
    ↓
[Reasoner] - Analyze & determine steps (llama3.1:8b or Gemini)
    ↓
[Formatter] - Output JSON with tool/args (qwen2.5:0.5b or llama3.2:3b)
    ↓
Tool Execution / Response
```

### Intent Classification

| Intent | Keywords | Use Case |
|--------|----------|----------|
| `os` | volume, brightness, open, launch, settings | System control |
| `office` | excel, word, outlook, teams, sheet | Productivity apps |
| `web` | gmail, calendar, drive, youtube, discord | Web applications |
| `files` | find, search, file, folder, pdf | File retrieval & RAG |
| `general` | (default) | Conversational fallback |

### Execution Modes

- **Offline Mode**: All local LLMs via Ollama (privacy-first, no API keys needed)
- **Online Mode**: Hybrid setup - uses Gemini for reasoning, Ollama for parsing/formatting
- **Auto Mode** (default): Auto-detects internet and switches modes accordingly

## How to Use

### Basic Usage (Copy-Paste Ready)

```python
from aiassistant.infra.config.app_config import CONFIG
from aiassistant.core.agent_factory import AgentFactory
from aiassistant.core.orchestrator_new import Orchestrator, TaskContext

# 1. Initialize
factory = AgentFactory(CONFIG, db=None)
manager = factory.create_manager()
offline_workers = factory.create_workers("offline")
online_workers = factory.create_workers("online")

# 2. Create orchestrator
orchestrator = Orchestrator(CONFIG, None, manager, offline_workers, online_workers)

# 3. Route task
context = TaskContext(user_id=1, session_id="session_1", mode="auto")
result = orchestrator.route_task("open chrome browser", context)

# 4. Get response
print(result.text)         # Response text
print(result.actions)      # Tool actions taken
print(result.meta)         # Metadata (intent, mode, etc.)
```

### Integration into Existing Code

To integrate with `main_gui.py` or your reasoning server:

```python
# In your main entry point:
from aiassistant.core import AgentFactory, Orchestrator, TaskContext

# During app initialization:
self.factory = AgentFactory(self.config, db=self.db)
self.orchestrator = Orchestrator(
    self.config,
    self.db,
    self.factory.create_manager(),
    self.factory.create_workers("offline"),
    self.factory.create_workers("online")
)

# When processing user input:
result = self.orchestrator.route_task(user_query, context)
```

## Configuration

Add to your `config.yaml`:

```yaml
llm:
  offline:
    base_url: "http://localhost:11434"
    model: "llama3.1:8b"
    timeout: 120
  online:
    api_key: "${GEMINI_API_KEY}"
    model: "gemini-1.5-flash"
    temperature: 0.2
  manager:
    provider: "ollama"
    model: "qwen2.5:0.5b"

crewai:
  enabled: false
  llm: null
```

## Key Features

✅ **Intent-based routing** - Automatically classifies queries  
✅ **Pipeline architecture** - Parser → Reasoner → Formatter  
✅ **Hybrid execution** - Mix local and cloud LLMs  
✅ **Online/offline detection** - Auto-switches based on connectivity  
✅ **Tool integration** - Structured JSON output for tool calling  
✅ **Confirmation flow** - Asks for approval before risky actions  
✅ **Extensible** - Easy to add new intents and workers  

## Next Steps

1. **Test the system** - Run `python example_worker_usage.py`
2. **Update config** - Add LLM settings to your `config.yaml`
3. **Integrate into main app** - Replace routing logic in `main_gui.py` or reasoning server
4. **Customize workers** - Modify specs in `AgentFactory._build_worker_specs()` for your use case
5. **Add tools** - Pass tool dicts to `WorkerSpec.tools` for each intent

## Backward Compatibility

✅ Existing files NOT modified - All new code in separate files  
✅ Imports organized in `__init__.py` for clean namespace  
✅ Can run alongside existing orchestrators  
✅ Optional - integrate at your own pace  

## Questions?

- Check `example_worker_usage.py` for runnable examples
- See docstrings in each class for detailed documentation
- Refer to repo memory at `/memories/repo/aiassistant-notes.md`
