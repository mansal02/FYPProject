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
ACCESS_VIOLATION_EXIT_CODES = {3221225477, -1073741819}
TERMINATE_TIMEOUT_SECONDS = 5
GUI_STABILITY_MAX_LEVEL = 2

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


def _is_access_violation_exit_code(return_code):
    return return_code in ACCESS_VIOLATION_EXIT_CODES


def _apply_main_gui_stability_profile(env, level):
    level_int = max(0, int(level))
    env["MARIE_STABILITY_MODE_LEVEL"] = str(level_int)
    env.setdefault("MARIE_FAULTHANDLER", "1")
    env.setdefault("MARIE_GUI_BOOT_LOG", str((BASE_DIR / "cache" / "main_gui_boot.log").resolve()))

    if level_int >= 1:
        env["MARIE_DISABLE_LIVE2D"] = "1"
        env["MARIE_ENABLE_LIVE2D"] = "0"

    if level_int >= 2:
        env["MARIE_DISABLE_VOICE_INPUT"] = "1"
        env["MARIE_DISABLE_TTS"] = "1"
        env["MARIE_DISABLE_SCREEN_CAPTURE"] = "1"
        env["MARIE_DISABLE_SCREEN_PREVIEW"] = "1"
        env["MARIE_DISABLE_LEGACY_ACTIONS"] = "1"
        env["MARIE_DISABLE_RAG"] = "1"
        env["MARIE_SAFE_MINIMAL"] = "1"
        # Force Qt software rendering for driver-sensitive systems.
        env["QT_OPENGL"] = "software"
        env["QT_ANGLE_PLATFORM"] = "software"
        env["QT_QUICK_BACKEND"] = "software"
        env["QTWEBENGINE_DISABLE_GPU"] = "1"


def _stability_profile_summary(level):
    if level <= 0:
        return "normal profile"
    if level == 1:
        return "stability profile 1 (Live2D disabled)"
    return "stability profile 2 (minimal runtime: Live2D/voice/TTS/screen/RAG disabled)"


def _main_gui_boot_log_path() -> str:
    return str((BASE_DIR / "cache" / "main_gui_boot.log").resolve())


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
    stability_level = 0

    while True:
        try:
            env = _build_child_env()
            if script_name == "aiassistant.frontend.main_gui":
                _apply_main_gui_stability_profile(env, stability_level)

            process = subprocess.Popen(
                [PYTHON_EXE, "-m", script_name],
                cwd=str(BASE_DIR),
                creationflags=_creation_flags(),
                env=env,
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

            if (
                script_name == "aiassistant.frontend.main_gui"
                and stability_level < GUI_STABILITY_MAX_LEVEL
            ):
                if _is_access_violation_exit_code(return_code):
                    # Native access violations should skip intermediate profile 1.
                    next_level = GUI_STABILITY_MAX_LEVEL
                else:
                    next_level = min(stability_level + 1, GUI_STABILITY_MAX_LEVEL)
                stability_level = next_level
                crash_kind = (
                    "native GUI crash"
                    if _is_access_violation_exit_code(return_code)
                    else "unexpected GUI exit"
                )
                print(
                    f"--- Detected {crash_kind} (exit code {return_code}). Restarting main_gui with {_stability_profile_summary(stability_level)}... ---",
                    flush=True,
                )
                if stability_level >= 1:
                    print(
                        f"--- Diagnostic boot log: {_main_gui_boot_log_path()} ---",
                        flush=True,
                    )
                continue

            with failures_lock:
                failures.append((script_name, return_code))
            print(f"ERROR: {script_name} failed with exit code {return_code}.", flush=True)
            if script_name == "aiassistant.frontend.main_gui":
                print(f"Diagnostic boot log: {_main_gui_boot_log_path()}", flush=True)

            if not shutdown_requested.is_set():
                shutdown_requested.set()
                _terminate_running_processes("another script failed", exclude_script=script_name)
            return
        except OSError as exc:
            with failures_lock:
                failures.append((script_name, f"launch error: {exc}"))
            print(f"ERROR: {script_name} failed to launch ({exc}).", flush=True)
            if not shutdown_requested.is_set():
                shutdown_requested.set()
                _terminate_running_processes("another script failed", exclude_script=script_name)
            return


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
