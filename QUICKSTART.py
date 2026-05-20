#!/usr/bin/env python3
"""
AI Assistant - Quick Start Guide
Display setup information and quick commands
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def main():
    print("\n" + "="*70)
    print("  AI ASSISTANT - QUICK START GUIDE")
    print("="*70 + "\n")
    
    print("[1] FIRST TIME SETUP\n")
    print("  1. Run: python setup.py")
    print("     or: setup.bat (Windows)")
    print("  2. Install Ollama from: https://ollama.ai")
    print("  3. Download models:")
    print("     ollama pull qwen2.5-coder:7b")
    print("     ollama pull qwen2.5vl:7b")
    print("     ollama pull qwen2.5:3b  (optional: for 3.4x speed)")
    
    print("\n[2] STARTING THE APP\n")
    print("  Run: python marie.bat")
    print("     or: python aiassistant/launchers/runsys.py --mode assistant")
    
    print("\n[3] CHECK SETUP STATUS\n")
    print("  Run: python check_setup.py")
    print("  Shows installed packages, models, and directories")
    
    print("\n[4] PERFORMANCE OPTIMIZATION\n")
    print("  Enable 3.4x faster responses:")
    print("    python enable_fast.py enable")
    print("  Disable and use standard processing:")
    print("    python enable_fast.py disable")
    print("  Check status:")
    print("    python enable_fast.py")
    
    print("\n[5] TEST & DEMO\n")
    print("  Test integration:")
    print("    python test_fast_integration.py")
    print("  Demo ultra-fast responses:")
    print("    python example_ultra_fast_usage.py")
    print("  Test worker usage:")
    print("    python example_worker_usage.py")
    
    print("\n[6] DOCUMENTATION\n")
    print("  README.md               - Main overview")
    print("  OPTIMIZATION.md         - Speed optimization guide")
    print("  config.yaml             - Configuration settings")
    print("  ollama_models.txt       - Available models to download")
    print("  requirements.txt        - Python dependencies")
    print("  CLEANUP_GUIDE.md        - Cleanup old files")
    
    print("\n[7] TROUBLESHOOTING\n")
    print("  App hangs on startup:")
    print("    → python enable_fast.py disable")
    print("  Models not found:")
    print("    → ollama pull qwen2.5-coder:7b")
    print("    → ollama pull qwen2.5vl:7b")
    print("  Package errors:")
    print("    → pip install -r requirements.txt")
    print("  Check status:")
    print("    → python check_setup.py")
    
    print("\n[8] PERFORMANCE TIPS\n")
    print("  • Enable fast orchestrator: python enable_fast.py enable")
    print("  • Use smaller model for speed: qwen2.5:3b")
    print("  • Keep OLLAMA_KEEP_ALIVE=0 to save VRAM")
    print("  • Run with stable internet (for web features)")
    print("  • Close other apps to free up RAM")
    
    print("\n[9] WHAT'S INCLUDED\n")
    print("  ✅ AI Assistant GUI (PyQt5)")
    print("  ✅ Local LLM support (Ollama)")
    print("  ✅ 3D Avatar (Live2D)")
    print("  ✅ Voice I/O (Whisper, TTS)")
    print("  ✅ Office automation (Excel, Word, PDF)")
    print("  ✅ System control (Mouse, keyboard, apps)")
    print("  ✅ Web search and automation")
    print("  ✅ Performance optimization (3.4x faster)")
    
    print("\n[10] RECOMMENDED SETUP\n")
    print("  Models to download (minimum):")
    print("    ollama pull qwen2.5-coder:7b")
    print("    ollama pull qwen2.5vl:7b")
    print("  With optional speed boost:")
    print("    ollama pull qwen2.5:3b")
    print("  System requirements:")
    print("    • RAM: 8GB+ (minimum 6GB)")
    print("    • VRAM: 4GB+ (for fast inference)")
    print("    • Disk: 30GB (for all models)")
    
    print("\n" + "="*70)
    print("  Ready to start? Run: python marie.bat")
    print("="*70 + "\n")


if __name__ == "__main__":
    main()
