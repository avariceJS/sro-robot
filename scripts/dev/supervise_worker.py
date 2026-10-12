#!/usr/bin/env python3
"""Run one worker command with bounded restart and signal-forwarding behavior.

This is a local process wrapper. Production deployments should also configure
their operating-system service manager and its independent start-rate limits.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence


def _process_group_exists(process: subprocess.Popen) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process_group(process: subprocess.Popen, signum: int) -> None:
    if not _process_group_exists(process):
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def _stop_process_group(process: subprocess.Popen, grace_seconds: float) -> None:
    _signal_process_group(process, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while _process_group_exists(process):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if process.poll() is None:
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(min(0.05, remaining))
    if _process_group_exists(process):
        _signal_process_group(process, signal.SIGKILL)
    if process.poll() is None:
        process.wait()


def run_supervised(
    command: Sequence[str],
    *,
    restart_budget: int = 5,
    initial_backoff: float = 1.0,
    max_backoff: float = 30.0,
    shutdown_grace: float = 10.0,
) -> int:
    """Run a command until clean exit, shutdown signal, or exhausted restarts."""
    if not command or any(not argument for argument in command):
        raise ValueError("A non-empty worker command is required")
    if restart_budget < 0:
        raise ValueError("restart_budget must be nonnegative")
    if initial_backoff <= 0 or max_backoff < initial_backoff:
        raise ValueError("backoff values must be positive and max_backoff >= initial_backoff")
    if shutdown_grace <= 0:
        raise ValueError("shutdown_grace must be positive")
    if os.name != "posix":
        raise OSError("Worker process-group supervision requires POSIX")

    shutdown = threading.Event()
    received_signal: list[int] = []
    current: list[subprocess.Popen | None] = [None]

    def request_shutdown(signum, _frame) -> None:
        received_signal.append(signum)
        shutdown.set()
        process = current[0]
        if process is not None:
            _signal_process_group(process, signal.SIGTERM)

    previous_handlers = {
        signum: signal.signal(signum, request_shutdown)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    restarts = 0
    try:
        while not shutdown.is_set():
            process = subprocess.Popen(command, start_new_session=True)
            current[0] = process
            while process.poll() is None and not shutdown.wait(0.1):
                pass

            if shutdown.is_set():
                _stop_process_group(process, shutdown_grace)
                current[0] = None
                signum = received_signal[-1] if received_signal else signal.SIGTERM
                print(f"worker_shutdown signal={signum}", flush=True)
                return 128 + signum

            exit_code = process.wait()
            # A worker may have exited while leaving children in its private
            # process group; clean them before reporting success or restarting.
            _stop_process_group(process, shutdown_grace)
            current[0] = None
            if exit_code == 0:
                print("worker_exit status=clean", flush=True)
                return 0
            if restarts >= restart_budget:
                print(
                    f"worker_restart_budget_exhausted restarts={restarts} "
                    f"exit_code={exit_code}",
                    flush=True,
                )
                return exit_code if exit_code > 0 else 128 + abs(exit_code)

            restarts += 1
            delay = min(initial_backoff * (2 ** (restarts - 1)), max_backoff)
            print(
                f"worker_restart_scheduled restart={restarts}/{restart_budget} "
                f"exit_code={exit_code} delay_seconds={delay:g}",
                flush=True,
            )
            if shutdown.wait(delay):
                signum = received_signal[-1] if received_signal else signal.SIGTERM
                print(f"worker_shutdown signal={signum}", flush=True)
                return 128 + signum
        return 128 + (received_signal[-1] if received_signal else signal.SIGTERM)
    finally:
        process = current[0]
        if process is not None:
            _stop_process_group(process, shutdown_grace)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart-budget", type=int, default=5)
    parser.add_argument("--initial-backoff", type=float, default=1.0)
    parser.add_argument("--max-backoff", type=float, default=30.0)
    parser.add_argument("--shutdown-grace", type=float, default=10.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        return run_supervised(
            command,
            restart_budget=args.restart_budget,
            initial_backoff=args.initial_backoff,
            max_backoff=args.max_backoff,
            shutdown_grace=args.shutdown_grace,
        )
    except (OSError, ValueError) as exc:
        print(f"worker_supervisor_failed error_type={type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
