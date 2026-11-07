"""Builder serialization and failed-upgrade custody, with synthetic subprocesses."""
import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.dev import tesseract_native as tool


class NativeToolTests(unittest.TestCase):
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
                class Process:
                    returncode = 0
                    def wait(self, **kwargs): return 0
                    def poll(self): return 0
                return Process()
            with (patch.object(tool, "ROOT", root), patch.object(tool.native, "RUNTIME", runtime),
                  patch.object(tool.native, "host_identity", return_value=identity),
                  patch.object(tool, "resources", return_value=(50, 0, 0)),
                  patch.object(tool, "admit"), patch.object(tool.subprocess, "Popen", side_effect=child),
                  patch.object(tool.native, "qualified_command", return_value=None), patch("builtins.print")):
                with self.assertRaisesRegex(ValueError, "Final relocation"):
                    tool.qualify(argparse.Namespace())
            self.assertEqual(marker.read_bytes(), b"previous-accepted")
            self.assertFalse((runtime / "candidate").exists())
            self.assertEqual(list(runtime.iterdir()), [marker])


if __name__ == "__main__":
    unittest.main()
