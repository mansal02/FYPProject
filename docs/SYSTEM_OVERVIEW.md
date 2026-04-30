# MARIE System Overview

This document describes the code layout, runtime flow, and upgrade paths for the MARIE desktop assistant.

## Scope

- Codebase: aiassistant/ package and supporting infrastructure.
- Runtime modes: assistant, legacy, hybrid.
- Primary objective: local desktop assistant for personal and office work.

## Repository Layout

- aiassistant/launchers: start-up orchestration (runsys.py).
- aiassistant/frontend: PyQt5 desktop UI (main_gui.py) and legacy UI.
- aiassistant/backend: FastAPI services (reasoning, voice, streaming).
- aiassistant/core: agent logic, multi-agent orchestration, CrewAI adapter.
- aiassistant/infra: config, db, voice, vision, avatar integrations.
- aiassistant/tools: safe desktop action framework.
- aiassistant/workers: background threads for reasoning/streaming.
- knowledge/, cache/, models/, piper/, rvc_models/: local runtime assets.

## Runtime Flow (assistant mode)

1) Launcher starts aiassistant.frontend.main_gui.
2) UI creates OfflineAgentCore with config and stored user preferences.
3) User input enters UI, which sends the text to AgentWorker.
4) Agent core logs the user message to the DB session.
5) Optional context is gathered:
   - Live screen context (if enabled).
   - RAG context (if enabled).
   - CrewAI advisory notes (if enabled).
6) Agent calls local reasoning model via Ollama.
7) If tool actions are emitted, they execute via isolated tool runner.
8) Agent synthesizes final reply and logs it to DB.
9) UI prints reply and optionally reads it aloud.

## Core Components

### Agent Core (aiassistant/core/agent_core.py)

- OfflineAgentCore is the primary reasoning entry point.
- Uses Ollama local model calls with keep_alive=0 for VRAM safety.
- Optional external model fallback (Gemini) when online mode is enabled.
- Optional tool actions via JSON payloads.
- Optional RAG retrieval and screen context.
- Optional CrewAI advisory notes when enabled in config.

### Multi-Agent Orchestrator

- aiassistant/core/multi_agent_orchestrator.py
- Simple researcher -> coder -> synthesizer chain.
- Used as fallback if CrewAI is not installed or disabled.

### CrewAI Adapter

- aiassistant/core/crew_orchestrator.py
- Optional integration that can run CrewAI if installed.
- Fallback to multi-agent chain when CrewAI is unavailable.

### UI (aiassistant/frontend/main_gui.py)

- Multi-tab PyQt5 UI (Assistant, History, Settings, RAD, Help).
- Live2D avatar embedding (optional).
- Voice input, wake-word, and TTS pipeline (optional).
- Response-only mode for transparent, minimal UI output.

### Backend Services (legacy/hybrid)

- aiassistant/backend/server_reasoning.py: REST endpoints for reasoning.
- aiassistant/backend/server_voice.py: TTS stream and audio control.
- aiassistant/workers/reasoning_worker.py: streaming logic.

## Data Stores

- SQLite DB: sessions, chat logs, RAD memory.
- ChromaDB (optional): local RAG embeddings.
- cache/memory.json: lightweight preference and successful command memory.

## Configuration (config.yaml)

Key sections:

- ui.response_only_mode: only show assistant replies in chat.
- ui.response_only_opacity: opacity when response-only mode is enabled.
- crew.enabled: enable CrewAI assistance.
- crew.mode: assist (advisory notes) or replace (CrewAI final response).
- crew.router: complex_only or always (when to run CrewAI).
- crew.provider: crewai or fallback.
- crew.context_max_chars: limits advisory context length.

## CrewAI Enablement

1) Install CrewAI:

   pip install crewai

2) Set config:

   crew:
     enabled: true
     provider: crewai
     mode: assist

3) (Optional) Provide LLM credentials via environment (for CrewAI defaults).

If CrewAI is not installed, MARIE falls back to its local multi-agent chain.

## Response-Only Mode

- Filters chat display to show assistant replies only.
- Keeps error or warning system messages visible.
- Uses a more transparent chat background and window opacity.

## Suggested Upgrades

- Introduce more granular permission tiers for tool actions.
- Add a lightweight task planner for multi-step office workflows.
- Expand voice profiles with per-user presets and quick switching.
- Add an optional timeline view for action history and tool outcomes.
- Support per-project CrewAI workflows for coding tasks.

## Testing/Validation Checklist

- Launch assistant mode: python -m aiassistant.launchers.runsys --mode assistant
- Toggle response-only mode and ensure only assistant replies are visible.
- Enable screen context and verify preview and capture toggles.
- Enable CrewAI and confirm advisory notes are injected for complex requests.
- Verify TTS and voice input toggles function in stability profile 0.
