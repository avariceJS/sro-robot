"""Synthetic local transport contracts; no browser/source access."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.dev.quality import browser_response_capture as capture
from scripts.dev.quality import download_current_kad as native


class BrowserResponseCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "private"
        self.root.mkdir(mode=0o700)
        for name in ("discovery", "staging"):
            (self.root / name).mkdir(mode=0o700)
        self.profile = self.base / "owned-profile"
        self.profile.mkdir(mode=0o700)
        (self.profile / "DevToolsActivePort").write_text("1234\n/devtools/browser/synthetic\n")
        self.endpoint = "ws://127.0.0.1:1234/devtools/browser/synthetic"
        self.custody = self.base / "custody.json"
        self.qualification = {k: True for k in ("origin_ok", "root_path", "root_ready", "status_ok")}
        self.qualification.update({k: False for k in ("challenge", "auth_required", "access_block", "rate_block")})
        self.write_private(self.custody, {"pid": 123456789, "profile": str(self.profile),
            "endpoint": self.endpoint, "phase": "qualified", "qualification": self.qualification})
        self.config = self.root / "discovery" / capture.CONFIG_NAME
        self.config_value = {"endpoint": self.endpoint, "custody_path": str(self.custody)}
        self.write_private(self.config, self.config_value)

    def write_private(self, path, value):
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def test_config_binds_owned_process_flags_and_private_endpoint(self):
        argv = ["Chrome", "--user-data-dir=" + str(self.profile), "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"]
        with patch.object(capture.os, "kill"), patch.object(capture, "_process_argv", return_value=argv), patch.object(capture.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, "p123456789\nf42\nn127.0.0.1:1234\n", "")):
            self.assertEqual(capture.load_config(self.root), self.config_value)

    def test_private_rejects_relative_file_even_if_mode_private(self):
        previous = Path.cwd()
        os.chdir(self.base)
        self.addCleanup(os.chdir, previous)
        self.write_private(Path("relative.json"), {})
        with self.assertRaisesRegex(capture.BrowserCaptureError, "configuration_rejected"):
            capture._private(Path("relative.json"))

    def test_exact_owned_arguments_reject_profile_prefix_and_duplicate_flags(self):
        argv = ["Chrome", "--user-data-dir=" + str(self.profile), "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"]
        self.assertTrue(capture._owned_argv(argv, self.profile))
        for invalid in [
            [argv[0], argv[1] + "-other", *argv[2:]],
            [*argv, "--remote-debugging-address=0.0.0.0"],
            [*argv, "--user-data-dir=" + str(self.profile) + "-other"],
            [*argv, "--remote-debugging-pipe"],
        ]:
            with self.subTest(arguments=invalid):
                self.assertFalse(capture._owned_argv(invalid, self.profile))

    def test_procargs_parser_preserves_spaces_and_does_not_decode_environment(self):
        argv = ["Chrome", "--user-data-dir=/private/owned profile", "--remote-debugging-port=0"]
        data = struct.pack("=i", len(argv)) + b"/Applications/Chrome\0\0\0" + b"\0".join(os.fsencode(a) for a in argv) + b"\0SECRET_ENV=do-not-decode\0"
        self.assertEqual(capture._argv_from_procargs(data), argv)

    def test_listen_metadata_rejects_wildcard_other_pid_and_nonloopback(self):
        for output in ["p123456789\nf42\nn*:1234\n", "p123456789\nn0.0.0.0:1234\n", "p123456789\nn192.0.2.1:1234\n", "p123456788\nn127.0.0.1:1234\n", "p123456789\nn127.0.0.1:1234\nn*:1234\n", "p123456789\nfnot-a-fd\nn127.0.0.1:1234\n"]:
            with self.subTest(metadata=output), patch.object(capture.shutil, "which", return_value="/usr/sbin/lsof"), patch.object(capture.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
                with self.assertRaises(ValueError):
                    capture._loopback_listener(123456789, 1234)

    def test_rate_retry_after_is_respected(self):
        with patch.object(capture, "load_config", return_value=self.config_value), patch.object(capture.subprocess, "run", return_value=subprocess.CompletedProcess([], 1,
                b'{"ok":false,"code":"rate-limited","cooldown_seconds":3600}', b"")):
            with self.assertRaises(capture.BrowserCaptureError) as error:
                capture.capture("synthetic-source", self.root)
        self.assertEqual(error.exception.code, "browser_rate_limited")
        self.assertEqual(error.exception.cooldown_seconds, 3600)

    def test_relative_config_root_is_rejected_without_process_probe(self):
        with patch.object(capture, "_process_argv") as argv:
            with self.assertRaisesRegex(capture.BrowserCaptureError, "configuration_rejected"):
                capture.load_config(Path("."))
            argv.assert_not_called()

    def test_rejected_custody_does_not_probe_or_spawn(self):
        value = json.loads(self.custody.read_text()); value["qualification"]["challenge"] = True
        self.write_private(self.custody, value)
        with patch.object(capture.subprocess, "run") as run:
            with self.assertRaisesRegex(capture.BrowserCaptureError, "configuration_rejected"):
                capture.load_config(self.root)
            run.assert_not_called()

    def test_public_config_rejected(self):
        self.config.chmod(0o644)
        with patch.object(capture.subprocess, "run") as run:
            with self.assertRaisesRegex(capture.BrowserCaptureError, "configuration_rejected"):
                capture.load_config(self.root)
            run.assert_not_called()

    def test_original_hash_pdf_validation_and_temporary_cleanup(self):
        original = b"%PDF-1.7\nsynthetic only\n%%EOF\n"
        def run(argv, **kwargs):
            job = json.loads(Path(argv[2]).read_text())
            self.assertEqual(kwargs, {"capture_output": True, "timeout": 85})
            self.assertEqual(Path(argv[2]).stat().st_mode & 0o077, 0)
            output = Path(job["output_path"])
            output.write_bytes(original); output.chmod(0o600)
            self.write_private(Path(str(output) + ".json"), {"final_url": "synthetic-result", "size": len(original),
                               "sha256": hashlib.sha256(original).hexdigest()})
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "bytes": len(original)}).encode(), b"harmless private warning")
        with patch.object(capture, "load_config", return_value=self.config_value), patch.object(capture.subprocess, "run", side_effect=run), patch.object(native, "_validate_pdf_snapshot") as validate:
            self.assertEqual(capture.capture("synthetic-source", self.root), (original, "synthetic-result"))
            validate.assert_called_once()
        self.assertEqual(list((self.root / "staging").iterdir()), [])

    def test_source_restrictions(self):
        for source_code, expected, disable in [
            ("challenge", "browser_access_challenge", True),
            ("rate-limited", "browser_rate_limited", False),
            ("pdf_navigation_mismatch", "browser_navigation_rejected", True),
            ("browser_target_unresolved", "browser_target_unresolved", True),
        ]:
            with self.subTest(code=source_code), patch.object(capture, "load_config", return_value=self.config_value), patch.object(capture.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "code": source_code}).encode(), b"")):
                with self.assertRaises(capture.BrowserCaptureError) as error:
                    capture.capture("synthetic-source", self.root)
                self.assertEqual(error.exception.code, expected)
                self.assertIs(error.exception.disable, disable)
                self.assertEqual(list((self.root / "staging").iterdir()), [])

    def test_malformed_status_fixed_error(self):
        with patch.object(capture, "load_config", return_value=self.config_value), patch.object(capture.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"[]", b"")):
            with self.assertRaisesRegex(capture.BrowserCaptureError, "browser_response_readback_mismatch"):
                capture.capture("synthetic-source", self.root)


if __name__ == "__main__":
    unittest.main()
