import argparse
import importlib.util
import os, subprocess, sys, threading, pyautogui, openpyxl
import argparse, contextlib, csv, difflib, fnmatch, importlib, io, json, mimetypes, os, platform, random, re, shutil, smtplib, subprocess, sys, threading, time, webbrowser
from pathlib import Path
from typing import Dict, List, Tuple

# ==========================================
# 1. PYINSTALLER SUBPROCESS INTERCEPTOR (CRITICAL)
# ==========================================
# Intercepts child processes so MARIE.exe doesn't infinite-loop itself.
if getattr(sys, 'frozen', False) and len(sys.argv) > 2 and sys.argv[1] == "-m":
    import runpy
    module_name = sys.argv[2]
    # Clean up sys.argv so the child module doesn't get confused by the '-m' flag
    sys.argv = [sys.argv[0]] + sys.argv[3:]
    # Run the target submodule natively and exit immediately when it finishes
    runpy.run_module(module_name, run_name="__main__")
    sys.exit(0)

# ==========================================
# 2. DYNAMIC PATH HELPER
# ==========================================
def get_runtime_path(relative_path: str, external: bool = False) -> Path:
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        if external:
            # External: Points to the folder containing MARIE.exe (Writable)
            return Path(sys.executable).parent / relative_path
        else:
            # Internal: Points to the temporary PyInstaller bundle (Read-Only)
            return Path(sys._MEIPASS) / relative_path
            
    # Dev mode: Resolves to the root d:\pylearn\FYP\AiAssistant\
    return Path(__file__).resolve().parents[2] / relative_path


# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
# Keep launcher behavior anchored correctly
BASE_DIR = get_runtime_path("", external=False)  # For Internal Code
APP_ROOT = get_runtime_path("", external=True)   # For External Logs/DB

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

PYTHON_EXE = sys.executable
# Notice we use APP_ROOT here so it writes logs safely outside the .exe
LOG_DIR = APP_ROOT / "cache" / "launch_logs" 

LAUNCH_MODES = {
    "assistant": ["aiassistant.frontend.main_gui"],
    "hybrid": [
        "aiassistant.backend.server_reasoning",
        "aiassistant.backend.server_voice",
        "aiassistant.frontend.main_gui"
    ],
}
DEFAULT_MODE = "hybrid"

CTRL_C_EXIT_CODES = {130, -1073741510, 3221225786}
ACCESS_VIOLATION_EXIT_CODES = {3221225477, -1073741819}
TERMINATE_TIMEOUT_SECONDS = 5
GUI_STABILITY_MAX_LEVEL = 2


# ==========================================
# GLOBAL STATE
# ==========================================
failures: List[Tuple[str, int | str]] = []
failures_lock = threading.Lock()

processes: Dict[str, subprocess.Popen] = {}
processes_lock = threading.Lock()

shutdown_requested = threading.Event()


# ==========================================
# ENVIRONMENT & HELPER FUNCTIONS
# ==========================================
def _build_child_env() -> Dict[str, str]:
    """Prepare the environment variables for child processes."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BASE_DIR.resolve())
    env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
    env.setdefault("OLLAMA_KV_CACHE_TYPE", "q4_0")
    env.setdefault("OLLAMA_KEEP_ALIVE", "5m")
    return env


def _apply_main_gui_stability_profile(env: Dict[str, str], level: int) -> None:
    """Apply specific environment variables for GUI stability mode."""
    env["MARIE_STABILITY_MODE_LEVEL"] = str(level)
    env.setdefault("MARIE_FAULTHANDLER", "1")
    env.setdefault("MARIE_GUI_BOOT_LOG", _main_gui_boot_log_path())
    env["MARIE_DISABLE_VOICE_INPUT"] = "1" if level > 0 else "0"
    env["MARIE_SAFE_MINIMAL"] = "1" if level > 0 else "0"


def _main_gui_boot_log_path() -> str:
    return str((BASE_DIR / "cache" / "main_gui_boot.log").resolve())


def _creation_flags() -> int:
    return subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0


def _is_interrupt(return_code: int) -> bool:
    return return_code in CTRL_C_EXIT_CODES


def _is_access_violation(return_code: int) -> bool:
    return return_code in ACCESS_VIOLATION_EXIT_CODES


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MARIE Local Assistant Launcher")
    parser.add_argument(
        "--mode",
        choices=sorted(LAUNCH_MODES.keys()),
        default=DEFAULT_MODE,
        help="assistant: launch new UI only (default), hybrid: backend servers + new UI",
    )
    return parser.parse_args()


def _resolve_modules(mode: str) -> List[str]:
    """Verify all requested modules exist before attempting to launch them."""
    modules = LAUNCH_MODES.get(mode, LAUNCH_MODES[DEFAULT_MODE])
    missing = [mod for mod in modules if importlib.util.find_spec(mod) is None]

    if missing:
        print("Cannot start launcher. Missing Python modules:")
        for mod in missing:
            print(f"  - {mod}")
        sys.exit(1)
        
    return modules


# ==========================================
# PROCESS MANAGEMENT
# ==========================================
def _terminate_running_processes(reason: str, exclude_script: str = None) -> None:
    """Safely terminate or force kill all running child processes."""
    with processes_lock:
        running = list(processes.items())

    # Phase 1: Attempt graceful termination
    for script_name, process in running:
        if script_name == exclude_script:
            continue
        if process.poll() is None:
            print(f"--- Stopping {script_name} ({reason}) ---", flush=True)
            process.terminate()

    # Phase 2: Force kill if they hang
    for script_name, process in running:
        if script_name == exclude_script:
            continue
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            print(f"--- Force killing {script_name} ---", flush=True)
            process.kill()


def _record_failure(script_name: str, error_detail: int | str) -> None:
    """Thread-safe recording of script failures and triggering global shutdown."""
    with failures_lock:
        failures.append((script_name, error_detail))
        
    print(f"ERROR: {script_name} failed with: {error_detail}", flush=True)
    
    if not shutdown_requested.is_set():
        shutdown_requested.set()
        _terminate_running_processes("another script failed", exclude_script=script_name)


def run_script(script_name: str) -> None:
    """Launch and monitor a specific Python script, handling its lifecycle and restarts."""
    stability_level = 0
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{script_name.replace('.', '_')}.log"

    while True:
        try:
            env = _build_child_env()
            if script_name == "aiassistant.frontend.main_gui":
                _apply_main_gui_stability_profile(env, stability_level)

            # Context manager ensures file is closed properly regardless of errors
            with open(log_path, "a", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [PYTHON_EXE, "-m", script_name],
                    cwd=str(APP_ROOT),
                    creationflags=_creation_flags(),
                    env=env,
                    stdout=log_file,
                    stderr=log_file,
                )
                
                with processes_lock:
                    processes[script_name] = process

                # Wait for process to complete
                return_code = process.wait()
                log_file.flush()

            # --- Evaluate Exit State ---
            if return_code == 0:
                print(f"--- Finished {script_name} ---\n", flush=True)
                return

            if shutdown_requested.is_set() or _is_interrupt(return_code):
                print(f"--- Stopped {script_name} ---", flush=True)
                return

            # --- Auto-Restart Logic (GUI Only) ---
            if script_name == "aiassistant.frontend.main_gui":
                stability_level += 1
                if stability_level > GUI_STABILITY_MAX_LEVEL:
                    print(f"--- Main GUI failed {stability_level} times. Giving up. ---", flush=True)
                else:
                    crash_kind = (
                        "native GUI crash" if _is_access_violation(return_code) else "unexpected GUI exit"
                    )
                    print(
                        f"--- Detected {crash_kind} (exit code {return_code}). Restarting main_gui (attempt {stability_level})... ---",
                        flush=True,
                    )
                    continue

            # --- Unrecoverable Failure Handling ---
            if script_name == "aiassistant.frontend.main_gui":
                print(f"Diagnostic boot log: {_main_gui_boot_log_path()}", flush=True)
                print(f"Process log: {log_path}", flush=True)
                
            _record_failure(script_name, f"exit code {return_code}")
            return

        except OSError as exc:
            _record_failure(script_name, f"launch error: {exc}")
            return


# ==========================================
# MAIN ENTRY POINT
# ==========================================
def main() -> None:
    args = _parse_args()
    scripts = _resolve_modules(args.mode)
    threads: List[threading.Thread] = []

    print(f"Launch mode: {args.mode}")
    
    # Start all threads
    for script in scripts:
        print(f"--- Starting {script} ---", flush=True)
        thread = threading.Thread(target=run_script, args=(script,))
        threads.append(thread)
        thread.start()

    # Block until all threads finish or user interrupts
    try:
        for thread in threads:
            thread.join()
            
    except KeyboardInterrupt:
        shutdown_requested.set()
        print("\n--- Interrupt received. Stopping all scripts... ---", flush=True)
        _terminate_running_processes("user interrupt")
        
        for thread in threads:
            thread.join()
            
        print("Launcher interrupted by user.", flush=True)
        sys.exit(130)

    # Report final status
    if failures:
        print("One or more scripts failed:")
        for script_name, code in failures:
            print(f"  - {script_name}: {code}")
        sys.exit(1)

    print("All scripts completed.")


if __name__ == "__main__":
    main()