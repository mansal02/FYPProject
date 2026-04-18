"""Isolated action runner.

Executes a single ActionHandler command in a subprocess so native crashes in
third-party automation/tooling cannot terminate the main GUI process.
"""

from __future__ import annotations

import argparse
import sys

from aiassistant.tools.action import ActionHandler


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one automation action command")
    parser.add_argument("--text", required=True, help="Action command text")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    text = str(args.text or "").strip()
    if not text:
        return 0

    try:
        handler = ActionHandler()
        output = handler.execute_and_collect(text)
        if output:
            print(output)
        return 0
    except Exception as exc:
        print(f"[ACTION RUNNER ERROR] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
