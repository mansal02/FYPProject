"""Isolated tool action runner.

Executes one run_tool_action payload in a subprocess so tool-side failures
cannot terminate the main assistant process.
"""

from __future__ import annotations

import argparse
import json
import sys

from aiassistant.tools.tools_os import run_tool_action


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one tool action JSON payload")
    parser.add_argument("--action-json", default="", help="Tool action payload as JSON object")
    return parser.parse_args()


def _read_payload(args: argparse.Namespace) -> str:
    from_arg = str(getattr(args, "action_json", "") or "").strip()
    if from_arg:
        return from_arg

    try:
        if sys.stdin is not None and not sys.stdin.closed:
            return sys.stdin.read().strip()
    except Exception:
        return ""
    return ""


def _emit(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=True))


def main() -> int:
    args = _parse_args()
    raw_payload = _read_payload(args)
    if not raw_payload:
        _emit(
            {
                "success": False,
                "message": "Tool action payload was empty.",
                "error": "empty_action_payload",
            }
        )
        return 1

    try:
        parsed = json.loads(raw_payload)
    except Exception as exc:
        _emit(
            {
                "success": False,
                "message": "Tool action payload was not valid JSON.",
                "error": str(exc),
            }
        )
        return 1

    if not isinstance(parsed, dict):
        _emit(
            {
                "success": False,
                "message": "Tool action payload must be a JSON object.",
                "error": "invalid_action_payload",
            }
        )
        return 1

    try:
        result = run_tool_action(parsed)
    except Exception as exc:
        _emit(
            {
                "success": False,
                "message": "Tool action runner crashed while executing payload.",
                "error": str(exc),
            }
        )
        return 1

    if not isinstance(result, dict):
        result = {
            "success": False,
            "message": "Tool action returned an invalid result payload.",
            "error": str(result),
        }

    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
