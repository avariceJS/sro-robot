from __future__ import annotations

from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SUPERVISOR = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "supervise_worker.py"


class WorkerSupervisorTests(unittest.TestCase):
    def test_restarts_nonzero_exit_with_bounded_budget_then_stops_on_clean_exit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sro-worker-supervisor-") as directory:
            marker = Path(directory) / "starts"
            child = (
                "from pathlib import Path; import sys; "
                "p=Path(sys.argv[1]); n=int(p.read_text())+1 if p.exists() else 1; "
                "p.write_text(str(n)); sys.exit(0 if n == 3 else 17)"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SUPERVISOR),
                    "--restart-budget",
                    "2",
                    "--initial-backoff",
                    "0.01",
                    "--max-backoff",
                    "0.02",
                    "--",
                    sys.executable,
                    "-c",
                    child,
                    str(marker),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            start_count = marker.read_text()

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(start_count, "3")
        self.assertIn("worker_restart_scheduled restart=1/2 exit_code=17", result.stdout)
        self.assertIn("worker_restart_scheduled restart=2/2 exit_code=17", result.stdout)
        self.assertIn("worker_exit status=clean", result.stdout)
        self.assertNotIn("sys.executable", result.stdout)

    def test_restart_budget_exhaustion_returns_nonzero_without_another_restart(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SUPERVISOR),
                "--restart-budget",
                "1",
                "--initial-backoff",
                "0.01",
                "--max-backoff",
                "0.01",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(9)",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stdout.count("worker_restart_scheduled"), 1)
        self.assertIn("worker_restart_budget_exhausted restarts=1 exit_code=9", result.stdout)

    def test_shutdown_signal_reaches_child_process_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sro-worker-supervisor-") as directory:
            marker = Path(directory) / "child-signal"
            child = (
                "import signal,sys,time; from pathlib import Path; "
                "p=Path(sys.argv[1]); "
                "signal.signal(signal.SIGTERM, lambda *_: (p.write_text('term'), sys.exit(0))); "
                "p.write_text('ready'); "
                "time.sleep(30)"
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(SUPERVISOR),
                    "--shutdown-grace",
                    "1",
                    "--",
                    sys.executable,
                    "-c",
                    child,
                    str(marker),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if marker.exists() and marker.read_text() == "ready":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("synthetic child did not start")

                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                child_signal = marker.read_text()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)

        self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr + stdout)
        self.assertEqual(child_signal, "term")
        self.assertIn(f"worker_shutdown signal={signal.SIGTERM}", stdout)
        self.assertNotIn("Traceback", stderr)

    def test_shutdown_escalates_when_child_ignores_termination(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sro-worker-supervisor-") as directory:
            marker = Path(directory) / "child-ready"
            child = (
                "import signal,sys,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "Path(sys.argv[1]).write_text('ready'); time.sleep(30)"
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(SUPERVISOR),
                    "--shutdown-grace",
                    "0.1",
                    "--",
                    sys.executable,
                    "-c",
                    child,
                    str(marker),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if marker.exists():
                        break
                    time.sleep(0.01)
                else:
                    self.fail("synthetic child did not start")

                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)

        self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr + stdout)
        self.assertIn(f"worker_shutdown signal={signal.SIGTERM}", stdout)
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
