import argparse
import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path

# Keep launcher behavior anchored to repository root where compatibility scripts live.
BASE_DIR = Path(__file__).resolve().parents[2]
# Running this file directly by absolute path does not always include repo root on sys.path.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

PYTHON_EXE = sys.executable
LAUNCH_MODES = {
    "assistant": ["aiassistant.frontend.main_gui"],
    "legacy": ["aiassistant.backend.server_reasoning", "aiassistant.backend.server_voice", "aiassistant.frontend.main_legacy"],
    "hybrid": ["aiassistant.backend.server_reasoning", "aiassistant.backend.server_voice", "aiassistant.frontend.main_gui"],
}
DEFAULT_MODE = "assistant"
CTRL_C_EXIT_CODES = {130, -1073741510, 3221225786}
TERMINATE_TIMEOUT_SECONDS = 5

failures = []
failures_lock = threading.Lock()
processes = {}
processes_lock = threading.Lock()
shutdown_requested = threading.Event()


def _build_child_env():
    env = os.environ.copy()
    env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
    env.setdefault("OLLAMA_KV_CACHE_TYPE", "q4_0")
    # Keep model residency low between requests to avoid VRAM accumulation.
    env.setdefault("OLLAMA_KEEP_ALIVE", "0")
    return env


def _creation_flags():
    if os.name == "nt":
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def _is_interrupt_exit_code(return_code):
    return return_code in CTRL_C_EXIT_CODES


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Local assistant launcher",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(LAUNCH_MODES.keys()),
        default=DEFAULT_MODE,
        help=(
            "assistant: launch new UI only (default), "
            "legacy: old server stack, hybrid: old servers + new UI"
        ),
    )
    return parser.parse_args()


def _resolve_modules(mode):
    modules = LAUNCH_MODES.get(mode, LAUNCH_MODES[DEFAULT_MODE])
    missing = [module for module in modules if importlib.util.find_spec(module) is None]

    if missing:
        print("Cannot start launcher. Missing Python modules:")
        for module in missing:
            print(f"- {module}")
        sys.exit(1)

    return modules


def _terminate_running_processes(reason, exclude_script=None):
    with processes_lock:
        running = list(processes.items())

    for script_name, process in running:
        if script_name == exclude_script:
            continue
        if process.poll() is None:
            print(f"--- Stopping {script_name} ({reason}) ---", flush=True)
            process.terminate()

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


def run_script(script_name):

    try:
        process = subprocess.Popen(
            [PYTHON_EXE, "-m", script_name],
            cwd=str(BASE_DIR),
            creationflags=_creation_flags(),
            env=_build_child_env(),
        )
        with processes_lock:
            processes[script_name] = process

        return_code = process.wait()

        if return_code == 0:
            print(f"--- Finished {script_name} ---\n", flush=True)
            return

        if shutdown_requested.is_set() or _is_interrupt_exit_code(return_code):
            print(f"--- Stopped {script_name} ---", flush=True)
            return

        with failures_lock:
            failures.append((script_name, return_code))
        print(f"ERROR: {script_name} failed with exit code {return_code}.", flush=True)

        if not shutdown_requested.is_set():
            shutdown_requested.set()
            _terminate_running_processes("another script failed", exclude_script=script_name)
    except OSError as exc:
        with failures_lock:
            failures.append((script_name, f"launch error: {exc}"))
        print(f"ERROR: {script_name} failed to launch ({exc}).", flush=True)
        if not shutdown_requested.is_set():
            shutdown_requested.set()
            _terminate_running_processes("another script failed", exclude_script=script_name)


def main():
    args = _parse_args()
    scripts = _resolve_modules(args.mode)

    print(f"Launch mode: {args.mode}")
    for script in scripts:
        print(f"--- Starting {script} ---", flush=True)
        thread = threading.Thread(target=run_script, args=(script,))
        threads.append(thread)
        thread.start()

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

    if failures:
        print("One or more scripts failed:")
        for script_name, code in failures:
            print(f"- {script_name}: exit code {code}")
        sys.exit(1)

    print("All scripts completed.")


threads = []


if __name__ == "__main__":
    main()
