"""Builder serialization and failed-upgrade custody, with synthetic subprocesses."""
import argparse
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts.dev import tesseract_native as tool


class NativeToolTests(unittest.TestCase):
    def test_normal_exited_leader_without_descendants_is_reaped(self):
        tool.run_owned([sys.executable, "-c", "pass"], timeout=2)

    def test_darwin_zombie_only_group_does_not_hide_live_permission_failure(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        for live in (False, True):
            process = MagicMock(pid=456789, returncode=0)
            states = "456789 Z\n" + ("456789 R\n" if live else "")
            with (self.subTest(live=live),
                  patch.object(tool.sys, "platform", "darwin"),
                  patch.object(tool.subprocess, "Popen", return_value=process),
                  patch.object(os, "waitid", return_value=SimpleNamespace(si_pid=456789)),
                  patch.object(os, "killpg", side_effect=PermissionError),
                  patch.object(tool.subprocess, "check_output", return_value=states)):
                if live:
                    with self.assertRaises(PermissionError):
                        tool.run_owned(["synthetic"], timeout=1)
                else:
                    tool.run_owned(["synthetic"], timeout=1)
                process.wait.assert_called_once_with(timeout=5)

    def assert_stopped(self, pid):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
            if not status or status.startswith("Z"):
                return
            time.sleep(.02)
        self.fail("Owned native descendant survived its leader cleanup")

    def test_exited_and_timed_out_leaders_leave_no_running_native_descendant(self):
        # Short-lived synthetic children only. Even a broken implementation
        # cannot leave a long-running test process behind.
        code = """
import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])
Path(sys.argv[1]).write_text(str(child.pid))
if sys.argv[2] == 'sleep': time.sleep(2)
"""
        with tempfile.TemporaryDirectory() as directory:
            for mode in ("exit", "sleep"):
                marker = Path(directory) / mode
                command = [sys.executable, "-c", code, str(marker), mode]
                with self.subTest(mode=mode):
                    if mode == "exit":
                        tool.run_owned(command, timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        with self.assertRaises(TimeoutError):
                            tool.run_owned(command, timeout=.5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.assert_stopped(int(marker.read_text()))

    def test_leader_is_not_reaped_before_group_cleanup(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        process = MagicMock(pid=456789, returncode=0)
        order = []
        process.wait.side_effect = lambda **kw: order.append("reap")
        with (patch.object(tool.subprocess, "Popen", return_value=process),
              patch.object(os, "waitid", return_value=SimpleNamespace(si_pid=456789)),
              patch.object(os, "killpg", side_effect=lambda *a: order.append("group-cleanup"))):
            tool.run_owned(["synthetic"], timeout=1)
        self.assertEqual(order, ["group-cleanup", "reap"])
        process.poll.assert_not_called()

    def test_lost_child_custody_never_signals_a_potentially_reused_group(self):
        from unittest.mock import MagicMock
        process = MagicMock(pid=456789, returncode=0)
        original = signal.getsignal(signal.SIGTERM)
        with (patch.object(tool.subprocess, "Popen", return_value=process),
              patch.object(os, "waitid", side_effect=ChildProcessError),
              patch.object(os, "killpg") as kill):
            with self.assertRaisesRegex(RuntimeError, "ownership lost"):
                tool.run_owned(["synthetic"], timeout=1)
        kill.assert_not_called()
        process.wait.assert_not_called()
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_sigterm_stops_owned_descendants_and_restores_handlers(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-pid"
            leader = "import subprocess,sys,time; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(3)']); Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(3)"
            supervisor = "from scripts.dev.tesseract_native import run_owned; import sys; run_owned([sys.executable,'-c',sys.argv[1],sys.argv[2]],timeout=5)"
            process = subprocess.Popen([sys.executable, "-c", supervisor, leader, str(marker)], cwd=tool.ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 2
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=3), 128 + signal.SIGTERM)
                self.assert_stopped(int(marker.read_text()))
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()

    def test_exclusive_operation_lock_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tool.native, "RUNTIME", Path(directory)):
            with self.assertRaisesRegex(ValueError, "synthetic"):
                with tool.runtime_lock():
                    with self.assertRaisesRegex(RuntimeError, "Another native"):
                        with tool.runtime_lock():
                            self.fail("Concurrent build was admitted")
                    raise ValueError("synthetic")
            with tool.runtime_lock():
                pass

    def test_atomic_write_failure_keeps_prior_marker_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); marker = root / "qualified.json"; marker.write_bytes(b"accepted")
            with patch.object(Path, "replace", side_effect=OSError("synthetic")):
                with self.assertRaises(OSError):
                    tool.write_json(marker, {"new": True})
            self.assertEqual(marker.read_bytes(), b"accepted")
            self.assertEqual(list(root.iterdir()), [marker])

    def test_failed_relocation_probe_preserves_previous_accepted_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); runtime = root / "runtime"; runtime.mkdir()
            marker = runtime / "qualified.json"; marker.write_bytes(b"previous-accepted")
            candidate = runtime / "candidate"; candidate.mkdir()
            executable = candidate / "tesseract"; executable.write_bytes(b"synthetic-native")
            identity = {"tessdata": "/synthetic/tessdata", "leptonica": "/synthetic/leptonica"}
            tool.write_json(candidate / "candidate.json", dict(policy=tool.native.POLICY, identity=identity, binary_sha256=tool.native.sha256(executable)))
            for collection, count, extension in (("img1", 21, "HEIC"), ("img2", 4, "png")):
                folder = root / "samples" / collection; folder.mkdir(parents=True)
                for index in range(count):
                    (folder / f"{index}.{extension}").write_bytes(b"synthetic-input")
            def child(command, **kwargs):
                # A completed synthetic child; no OCR/application services.
                output = Path(command[command.index("--output") + 1])
                input_path = Path(command[command.index("--input") + 1])
                is_system = command[command.index("--command") + 1] == "tesseract"
                tool.write_json(output, dict(seconds=2 if is_system else 1, calls=1, payload={"synthetic": True}, input_sha256=tool.native.sha256(input_path)))
            with (patch.object(tool, "ROOT", root), patch.object(tool.native, "RUNTIME", runtime),
                  patch.object(tool.native, "host_identity", return_value=identity),
                  patch.object(tool, "resources", return_value=(50, 0, 0)),
                  patch.object(tool, "admit"), patch.object(tool, "run_owned", side_effect=child),
                  patch.object(tool.native, "qualified_command", return_value=None), patch("builtins.print")):
                with self.assertRaisesRegex(ValueError, "Final relocation"):
                    tool.qualify(argparse.Namespace())
            self.assertEqual(marker.read_bytes(), b"previous-accepted")
            self.assertFalse((runtime / "candidate").exists())
            self.assertEqual(list(runtime.iterdir()), [marker])


if __name__ == "__main__":
    unittest.main()
