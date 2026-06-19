MARIE - Multi-Agent Reasoning & Intelligent Environment

MARIE is a privacy-first desktop AI assistant that runs entirely locally. It features:

Qwen-2.5 for fast local reasoning and task execution.

Piper for offline Text-to-Speech (TTS).

Live2D avatar rendering in a PyQt5 desktop UI.

Local RAG & Memory Agent for continuous background learning.

Safe, JSON-based desktop tool execution (file management, system commands, etc.).

📥 1. How to Get Other Components (Runtime Assets)

To keep this repository lightweight, the large model files required for MARIE's advanced features are hosted externally. You must download these before setting up the application.

Access the required assets from our [Google Drive Link Here] (Note: Insert actual link).

Once downloaded, extract and place them into the following directories in your project root:

./piper/ : Contains piper.exe, your chosen *.onnx voice models, their matching .json metadata, and the espeak-ng-data/ folder.

./models/ : Contains your Live2D character models (e.g., ./models/kei/runtime/kei_vowels_pro.model3.json).

./rvc/ : (Optional) Contains Voice Conversion .pth and .index models.

⚙️ 2. How to Set It Up

Follow these steps to configure your local environment:

Step A: Prerequisites

Install Python 3.10+.

Install Ollama (https://ollama.com).

Pull the required base reasoning models via your terminal:

ollama pull qwen2.5-coder:7b
ollama pull qwen2.5:3b


Step B: Virtual Environment & Dependencies

Open your terminal in the project folder and run:

Windows PowerShell:

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
python -m spacy download en_core_web_sm


Bash / Linux / Mac:

python -m venv .venv
source .venv/Scripts/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m spacy download en_core_web_sm


Step C: Environment Configuration

Copy the example environment file to create your own local settings:

# Windows
copy .env.example .env

# Bash
cp .env.example .env


Open .env in a text editor to verify paths, wake words, and optional API keys (like GOOGLE_API_KEY for online fallback mode).

🚀 3. How to Run It

Once your assets are in place and your environment is configured, ensure your virtual environment is activated and run:

The Easy Way (Wrapper):

python marie.bat


The Module Way (Recommended for debugging):

python -m aiassistant.launchers.runsys --mode assistant


Available launch modes:

assistant: Launches the new offline GUI only (Default).

hybrid: Launches backend reasoning/voice servers alongside the GUI.

🧠 MARIE Memory Agent (Background Learning)

MARIE features a local memory watcher that learns from your notes and stores vectors in ChromaDB, accessible via the knowledge/memory_agent/ folder.

Ensure required packages are installed: pip install chromadb watchdog ollama

Build the memory index once:

python -m aiassistant.infra.memory_agent --once


Run the watcher in the background to learn as you work:

python -m aiassistant.infra.memory_agent


📁 Systematic Project Layout

The core application logic is strictly organized under the aiassistant/ package:

aiassistant/
    frontend/    # PyQt5 GUI applications
    backend/     # FastAPI services (Voice, Reasoning streams)
    core/        # Agent logic, orchestrators, LLM routing
    infra/       # Databases, RAG, Embeddings, Config, Voice DB
    tools/       # Desktop & OS automation tool scripts
    workers/     # Multi-threaded background task handlers
    launchers/   # System boot and process management
    models/      # 
    piper/       #
    rvc/         #


🛠️ Troubleshooting

Models / Assets missing error: Ensure you downloaded the folders from the Google Drive link and placed them exactly in the root directory (/piper, /models).

Reasoning/Voice server unreachable: Ensure Ollama is running in the background. If running manually, start python -m aiassistant.backend.server_reasoning and server_voice in separate terminals.

No speech detected: Verify your microphone input device in OS settings and check VAD thresholds in config.yaml.

Database locked errors: MARIE uses SQLite WAL mode. Ensure multiple instances of the app aren't fighting for write access outside the main orchestrator.

📜 License

This project is licensed under the MIT License. See LICENSE for details.