#!/usr/bin/env python3
"""
AI Assistant Setup Script
Complete setup with dependency check and model download
"""

import os
import sys
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def print_header(text):
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}\n")


def check_python_version():
    """Check Python version (need 3.10+)"""
    print_header("Checking Python Version")
    version = sys.version_info
    if version.major < 3 or (version.major == 3 and version.minor < 10):
        print(f"❌ Python 3.10+ required (you have {version.major}.{version.minor})")
        return False
    print(f"✅ Python {version.major}.{version.minor} OK")
    return True


def check_pip():
    """Check if pip is available and version 24.0+"""
    print_header("Checking pip")
    try:
        result = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            output = result.stdout.strip()
            print(f"✅ pip available: {output}")
            
            # Extract version number (format: pip XX.X.X ...)
            try:
                version_str = output.split()[1]
                major_version = int(version_str.split('.')[0])
                if major_version < 24:
                    print(f"❌ pip 24.0+ required (you have {version_str})")
                    print(f"   Upgrade with: python -m pip install --upgrade pip")
                    return False
                print(f"✅ pip version {version_str} OK (24.0+ required)")
            except (IndexError, ValueError):
                print(f"⚠️  Could not parse pip version from: {output}")
                return True
            return True
    except Exception as e:
        print(f"❌ pip check failed: {e}")
    return False


def install_requirements():
    """Install Python packages from requirements.txt"""
    print_header("Installing Python Packages")
    
    req_file = BASE_DIR / "requirements.txt"
    if not req_file.exists():
        print(f"❌ requirements.txt not found at {req_file}")
        return False
    
    print(f"Installing from {req_file}...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
            cwd=str(BASE_DIR)
        )
        if result.returncode == 0:
            print("✅ Packages installed successfully")
            return True
        else:
            print("❌ Package installation failed")
            return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def check_ollama():
    """Check if Ollama is installed"""
    print_header("Checking Ollama")
    try:
        result = subprocess.run(["ollama", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ Ollama available: {result.stdout.strip()}")
            return True
    except FileNotFoundError:
        print("⚠️  Ollama not found in PATH")
        print("   Download from: https://ollama.ai")
        return False


def check_ollama_models():
    """Check which Ollama models are installed"""
    print_header("Checking Ollama Models")
    try:
        result = subprocess.run(["ollama", "list"], capture_output=True, text=True)
        if result.returncode == 0:
            models = result.stdout.strip()
            if models:
                print("Installed models:")
                for line in models.split('\n')[1:]:  # Skip header
                    print(f"  {line}")
                return True
            else:
                print("⚠️  No models installed")
                return False
    except FileNotFoundError:
        print("⚠️  Cannot list models (Ollama not in PATH)")
        return False


def suggest_ollama_models():
    """Suggest which models to download"""
    print_header("Suggested Ollama Models")
    
    print("Minimum (Essential):")
    print("  ollama pull qwen2.5-coder:7b")
    print("  ollama pull qwen2.5vl:7b")
    
    print("\nRecommended (with 3.4x faster responses):")
    print("  ollama pull qwen2.5-coder:7b")
    print("  ollama pull qwen2.5vl:7b")
    print("  ollama pull qwen2.5:3b")
    
    print("\nFull (all benefits):")
    print("  ollama pull qwen2.5-coder:7b")
    print("  ollama pull qwen2.5vl:7b")
    print("  ollama pull qwen2.5:3b")
    print("  ollama pull qwen2.5:7b")
    
    print("\nSee: ollama_models.txt")


def check_app_structure():
    """Check app directory structure"""
    print_header("Checking App Structure")
    
    required_dirs = [
        "aiassistant/core",
        "aiassistant/workers",
        "aiassistant/frontend",
        "aiassistant/infra",
        "models",
        "cache",
    ]
    
    all_exist = True
    for dir_path in required_dirs:
        full_path = BASE_DIR / dir_path
        if full_path.exists():
            print(f"✅ {dir_path}")
        else:
            print(f"❌ {dir_path} MISSING")
            all_exist = False
    
    return all_exist


def setup_complete():
    """Show setup complete message"""
    print_header("Setup Summary")
    
    print("✅ SETUP COMPLETE!")
    print("\nNext steps:")
    print("1. Download Ollama models (see above)")
    print("2. Start Ollama service")
    print("3. Run the app:")
    print("   python marie.bat")
    print("\nFor more details, see:")
    print("  - README.md (Main documentation)")
    print("  - OPTIMIZATION.md (Speed guide)")
    print("  - ollama_models.txt (Model download)")
    print("  - config.yaml (Configuration)")


def main():
    """Run complete setup"""
    print("\n" + "="*60)
    print("  AI ASSISTANT - SETUP SCRIPT")
    print("="*60)
    
    checks = [
        ("Python Version", check_python_version),
        ("pip", check_pip),
        ("App Structure", check_app_structure),
        ("Ollama", check_ollama),
    ]
    
    print("\n[STEP 1] System Checks\n")
    for name, check_func in checks:
        if not check_func():
            print(f"\n⚠️  Warning: {name} check failed")
    
    print("\n[STEP 2] Installing Packages\n")
    if install_requirements():
        print("\n[STEP 3] Checking Models\n")
        check_ollama_models()
        
        print("\n[STEP 4] Suggestions\n")
        suggest_ollama_models()
    
    setup_complete()


if __name__ == "__main__":
    main()
