#!/usr/bin/env python3
"""
Check AI Assistant Setup Status
Verify all components are properly installed
"""

import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def check(name, condition, details=""):
    """Print check result"""
    symbol = "✅" if condition else "❌"
    status = "OK" if condition else "MISSING"
    print(f"{symbol} {name:30} {status:10} {details}")
    return condition


def main():
    print("\n" + "="*70)
    print("  AI ASSISTANT - SETUP CHECK")
    print("="*70 + "\n")
    
    all_ok = True
    
    # Python & pip
    print("[PYTHON & PACKAGES]")
    try:
        version = sys.version_info
        check("Python", version.major >= 3 and version.minor >= 8, f"v{version.major}.{version.minor}")
    except:
        all_ok = False
    
    # Check pip packages
    try:
        import PyQt5
        check("PyQt5", True)
    except ImportError:
        all_ok = check("PyQt5", False)
    
    try:
        import requests
        check("requests", True)
    except ImportError:
        all_ok = check("requests", False)
    
    try:
        import ollama
        check("ollama", True)
    except ImportError:
        all_ok = check("ollama", False)
    
    try:
        import crewai
        check("crewai", True)
    except ImportError:
        all_ok = check("crewai", False)
    
    # Directory structure
    print("\n[DIRECTORIES]")
    dirs = [
        ("aiassistant/core", "Core logic"),
        ("aiassistant/workers", "Worker modules"),
        ("aiassistant/frontend", "GUI"),
        ("models", "Avatar models"),
        ("piper", "Voice models"),
        ("cache", "Cache directory"),
    ]
    for dir_name, desc in dirs:
        path = BASE_DIR / dir_name
        all_ok = check(f"{dir_name}", path.exists(), desc) and all_ok
    
    # Key files
    print("\n[FILES]")
    files = [
        ("config.yaml", "Configuration"),
        ("requirements.txt", "Dependencies"),
        ("ollama_models.txt", "Model list"),
        ("OPTIMIZATION.md", "Speed guide"),
        ("marie.bat", "Main launcher"),
    ]
    for file_name, desc in files:
        path = BASE_DIR / file_name
        all_ok = check(f"{file_name}", path.exists(), desc) and all_ok
    
    # Ollama
    print("\n[OLLAMA]")
    try:
        result = subprocess.run(["ollama", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            check("Ollama CLI", True, result.stdout.strip())
        else:
            all_ok = check("Ollama CLI", False, "Not responding") and all_ok
    except FileNotFoundError:
        all_ok = check("Ollama CLI", False, "Not in PATH") and all_ok
    except subprocess.TimeoutExpired:
        all_ok = check("Ollama CLI", False, "Timeout") and all_ok
    
    # Check models
    try:
        result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            models_text = result.stdout.strip().split('\n')[1:]  # Skip header
            model_count = len([m for m in models_text if m.strip()])
            check("Ollama Models", model_count > 0, f"{model_count} model(s) installed")
            if model_count > 0:
                for model_line in models_text[:3]:  # Show first 3
                    if model_line.strip():
                        print(f"    • {model_line.split()[0]}")
                if model_count > 3:
                    print(f"    ... and {model_count - 3} more")
        else:
            all_ok = check("Ollama Models", False, "None installed") and all_ok
    except Exception as e:
        all_ok = check("Ollama Models", False, str(e)) and all_ok
    
    # Fast orchestrator
    print("\n[OPTIMIZATION]")
    try:
        from aiassistant.core import UltraFastOrchestrator
        from aiassistant.workers import FastOfflineWorker
        check("Fast Orchestrator", True, "Installed")
        check("Fast Worker", True, "Available")
    except ImportError as e:
        all_ok = check("Fast Orchestrator", False, str(e)) and all_ok
    
    # Summary
    print("\n" + "="*70)
    if all_ok:
        print("✅ SETUP OK - Ready to run!")
        print("\nRun the app with:")
        print("  python marie.bat")
    else:
        print("⚠️  SETUP INCOMPLETE - Some components missing")
        print("\nRun setup with:")
        print("  python setup.py")
        print("  or")
        print("  setup.bat")
    
    print("\nDocumentation:")
    print("  - README.md (Overview)")
    print("  - OPTIMIZATION.md (Speed guide)")
    print("  - ollama_models.txt (Model downloads)")
    print("  - config.yaml (Settings)")
    print("\n" + "="*70 + "\n")


if __name__ == "__main__":
    main()
