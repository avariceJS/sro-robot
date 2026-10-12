from __future__ import annotations

import os
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

    def test_clean_worker_exit_reaps_orphaned_process_group_children(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sro-worker-supervisor-") as directory:
            pid_file = Path(directory) / "orphan-pid"
            grandchild = (
                "import signal,sys,time,os; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
            )
            child = (
                "import subprocess,sys,time; from pathlib import Path; "
                f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, sys.argv[1]]); "
                "deadline=time.monotonic()+3; p=Path(sys.argv[1]);\n"
                "while not p.exists() and time.monotonic()<deadline:\n"
                "    time.sleep(0.01)\n"
                "sys.exit(0 if p.exists() else 4)"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SUPERVISOR),
                    "--shutdown-grace",
                    "0.1",
                    "--",
                    sys.executable,
                    "-c",
                    child,
                    str(pid_file),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertTrue(pid_file.exists(), result.stderr + result.stdout)
            child_pid = int(pid_file.read_text())

            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    probe = subprocess.run(
                        ["ps", "-o", "stat=", "-p", str(child_pid)],
                        capture_output=True,
                        text=True,
                        timeout=1,
                        check=False,
                    )
                    state = probe.stdout.strip()
                    if probe.returncode != 0 or not state or state.startswith("Z"):
                        break
                    time.sleep(0.02)
                else:
                    self.fail("orphaned worker child survived supervisor exit")
            finally:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("worker_exit status=clean", result.stdout)

    def test_failed_worker_descendants_are_stopped_before_restart(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sro-worker-supervisor-") as directory:
            starts = Path(directory) / "starts"
            pid_file = Path(directory) / "orphan-pid"
            restarted = Path(directory) / "replacement-started"
            grandchild = (
                "import signal,sys,time,os; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
            )
            child = (
                "import subprocess,sys,time; from pathlib import Path; "
                "starts=Path(sys.argv[1]); n=int(starts.read_text())+1 if starts.exists() else 1; "
                "starts.write_text(str(n)); pid_file=Path(sys.argv[2]);\n"
                "if n == 1:\n"
                f"    subprocess.Popen([sys.executable, '-c', {grandchild!r}, str(pid_file)])\n"
                "    deadline=time.monotonic()+3\n"
                "    while not pid_file.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
                "    sys.exit(17 if pid_file.exists() else 4)\n"
                "deadline=time.monotonic()+3\n"
                "while time.monotonic()<deadline:\n"
                "    probe=subprocess.run(['ps','-o','stat=','-p',pid_file.read_text()], capture_output=True, text=True)\n"
                "    state=probe.stdout.strip()\n"
                "    if probe.returncode != 0 or not state or state.startswith('Z'):\n"
                f"        Path(sys.argv[3]).write_text('clean')\n"
                "        sys.exit(0)\n"
                "    time.sleep(0.02)\n"
                "sys.exit(8)"
            )
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
                    "--shutdown-grace",
                    "0.1",
                    "--",
                    sys.executable,
                    "-c",
                    child,
                    str(starts),
                    str(pid_file),
                    str(restarted),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

            self.assertTrue(pid_file.exists(), result.stderr + result.stdout)
            child_pid = int(pid_file.read_text())
            try:
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertEqual(starts.read_text(), "2")
                self.assertEqual(restarted.read_text(), "clean")
                self.assertIn("worker_restart_scheduled restart=1/1 exit_code=17", result.stdout)
            finally:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

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
