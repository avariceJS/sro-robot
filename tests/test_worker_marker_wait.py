from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.dev.worker_marker_wait import wait_for_marker


class WorkerMarkerWaitTests(unittest.TestCase):
    def test_returns_when_child_writes_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "worker.log"
            with log_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [sys.executable, "-c", "print('completed', flush=True)"],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
                self.assertTrue(
                    wait_for_marker(process.pid, log_path, "completed", 2)
                )
                self.assertEqual(process.wait(timeout=2), 0)

    @unittest.skipUnless(os.name == "posix", "POSIX process signals are required")
    def test_timeout_terminates_and_allows_reaping_a_child_ignoring_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "worker.log"
            ready_path = Path(temporary_directory) / "worker.ready"
            script = (
                "import pathlib, signal, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).touch(); "
                "time.sleep(30)"
            )
            with log_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [sys.executable, "-c", script, str(ready_path)],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
                try:
                    ready_deadline = time.monotonic() + 2
                    while (
                        not ready_path.exists()
                        and time.monotonic() < ready_deadline
                    ):
                        if process.poll() is not None:
                            self.fail(
                                "synthetic child exited before installing SIGTERM handler"
                            )
                        time.sleep(0.01)
                    self.assertTrue(
                        ready_path.exists(), "synthetic child did not become ready"
                    )
                    self.assertFalse(
                        wait_for_marker(process.pid, log_path, "completed", 0.05)
                    )
                    self.assertEqual(process.wait(timeout=2), -signal.SIGKILL)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)

    def test_returns_false_when_child_exited_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "worker.log"
            process = subprocess.Popen(
                [sys.executable, "-c", "raise SystemExit(0)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.assertEqual(process.wait(timeout=2), 0)
            self.assertFalse(wait_for_marker(process.pid, log_path, "completed", 0.05))
            self.assertEqual(process.wait(timeout=2), 0)


if __name__ == "__main__":
    unittest.main()
