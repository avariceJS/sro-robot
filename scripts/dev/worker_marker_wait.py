"""Wait for a child process to write a completion marker, with a hard timeout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import time


def wait_for_marker(
    process_id: int,
    log_path: Path,
    marker: str,
    timeout_seconds: float,
) -> bool:
    if process_id <= 0:
        raise ValueError("process_id must be positive")
    if not marker or timeout_seconds <= 0:
        raise ValueError("marker and a positive timeout are required")

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            if marker in log_path.read_text(encoding="utf-8").splitlines():
                return True
        except FileNotFoundError:
            pass

        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                os.kill(process_id, signal.SIGTERM)
            except ProcessLookupError:
                return False
            # The caller owns and reaps this child. Give it a short cleanup
            # window, then force termination so its subsequent wait is bounded.
            time.sleep(0.2)
            try:
                os.kill(process_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return False
        time.sleep(min(0.05, remaining))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("process_id", type=int)
    parser.add_argument("log_path", type=Path)
    parser.add_argument("marker")
    parser.add_argument("timeout_seconds", type=float)
    args = parser.parse_args()
    return 0 if wait_for_marker(
        args.process_id,
        args.log_path,
        args.marker,
        args.timeout_seconds,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
