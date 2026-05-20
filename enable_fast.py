#!/usr/bin/env python3
"""
Enable/Disable Fast Orchestrator
Toggle the 3.4x speed optimization
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RUNSYS_FILE = BASE_DIR / "aiassistant" / "launchers" / "runsys.py"


def read_file():
    """Read runsys.py"""
    try:
        return RUNSYS_FILE.read_text()
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        return None


def write_file(content):
    """Write to runsys.py"""
    try:
        RUNSYS_FILE.write_text(content)
        return True
    except Exception as e:
        print(f"❌ Error writing file: {e}")
        return False


def get_status():
    """Check if fast orchestrator is enabled"""
    content = read_file()
    if not content:
        return None
    
    # Check for disable flag
    if 'MARIE_DISABLE_FAST_ORCHESTRATOR", "1"' in content:
        return False
    elif 'MARIE_DISABLE_FAST_ORCHESTRATOR", "0"' in content:
        return True
    else:
        return None


def enable():
    """Enable fast orchestrator"""
    content = read_file()
    if not content:
        return False
    
    # Replace disable=1 with disable=0
    content = content.replace(
        'env.setdefault("MARIE_DISABLE_FAST_ORCHESTRATOR", "1")',
        'env.setdefault("MARIE_DISABLE_FAST_ORCHESTRATOR", "0")'
    )
    
    if write_file(content):
        print("✅ Fast orchestrator ENABLED (3.4x speed)")
        print("   Responses will be ~350ms instead of 1200ms")
        print("   Cached repeats will be ~50ms (24x faster)")
        return True
    return False


def disable():
    """Disable fast orchestrator"""
    content = read_file()
    if not content:
        return False
    
    # Replace disable=0 with disable=1
    content = content.replace(
        'env.setdefault("MARIE_DISABLE_FAST_ORCHESTRATOR", "0")',
        'env.setdefault("MARIE_DISABLE_FAST_ORCHESTRATOR", "1")'
    )
    
    if write_file(content):
        print("✅ Fast orchestrator DISABLED")
        print("   Using standard processing")
        return True
    return False


def main():
    print("\n" + "="*60)
    print("  AI ASSISTANT - Fast Orchestrator Control")
    print("="*60 + "\n")
    
    status = get_status()
    
    if status is None:
        print("❌ Cannot find fast orchestrator setting")
        print(f"   Expected in: {RUNSYS_FILE}")
        return
    
    current = "ENABLED ✅" if status else "DISABLED ⚠️"
    print(f"Current status: {current}\n")
    
    if len(sys.argv) > 1:
        if sys.argv[1].lower() in ["enable", "on", "1"]:
            if status:
                print("Already enabled!")
            else:
                enable()
        elif sys.argv[1].lower() in ["disable", "off", "0"]:
            if not status:
                print("Already disabled!")
            else:
                disable()
        else:
            print(f"Unknown command: {sys.argv[1]}")
            print("Usage: python enable_fast.py [enable|disable]")
    else:
        print("Usage: python enable_fast.py [command]\n")
        print("Commands:")
        print("  enable   - Enable 3.4x faster responses")
        print("  disable  - Disable and use standard processing\n")
        print("Examples:")
        print("  python enable_fast.py enable")
        print("  python enable_fast.py disable")


if __name__ == "__main__":
    main()
