from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts/ops/launch-kad-firefox.sh"


class FirefoxLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sro-kad-launcher-")
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "operator-home"
        self.home.mkdir()
        self.profile = self.home / ".local/share/sro-robot/kad-firefox"
        self.args_path = self.root / "firefox-args.txt"
        self._stub("id", 'case "$1" in -un) echo nightly-operator;; -u) echo "${SRO_TEST_UID:-1000}";; *) exit 2;; esac')
        self._stub("getent", 'printf "nightly-operator:x:1000:1000:Synthetic:%s:/bin/bash\\n" "$SRO_TEST_HOME"')
        self._stub("stat", 'echo "${SRO_TEST_PROFILE_UID:-1000}"')
        self._stub("curl", 'exit "${SRO_TEST_CURL_EXIT:-0}"')
        self.firefox = self.bin / "firefox"
        self._stub("firefox", 'printf "%s\\n" "$@" > "$SRO_TEST_FIREFOX_ARGS"')

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o700)

    def _run(self, **updates: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.bin}:{env.get('PATH', '/usr/bin:/bin')}",
            "SRO_TEST_HOME": str(self.home),
            "SRO_TEST_FIREFOX_ARGS": str(self.args_path),
            "SRO_FIREFOX_BIN": str(self.firefox),
            "SRO_KAD_PROFILE_DIR": str(self.profile),
            "DISPLAY": ":synthetic",
            "WAYLAND_DISPLAY": "",
            "SRO_LOCAL_APP_URL": "http://127.0.0.1:8000",
        })
        env.update(updates)
        return subprocess.run(["/bin/bash", str(LAUNCHER)], cwd=ROOT, env=env,
                              text=True, capture_output=True, timeout=10, check=False)

    def test_refuses_root_before_health_or_browser_access(self) -> None:
        result = self._run(SRO_TEST_UID="0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not as root", result.stderr)
        self.assertFalse(self.args_path.exists())
        self.assertFalse(self.profile.exists())

    def test_refuses_missing_interactive_display(self) -> None:
        result = self._run(DISPLAY="", WAYLAND_DISPLAY="")
        self.assertEqual(result.returncode, 2)
        self.assertIn("interactive X11/Wayland", result.stderr)
        self.assertFalse(self.args_path.exists())

    def test_refuses_non_loopback_service_url(self) -> None:
        result = self._run(SRO_LOCAL_APP_URL="http://example.invalid:8000")
        self.assertEqual(result.returncode, 2)
        self.assertIn("loopback application", result.stderr)
        self.assertFalse(self.args_path.exists())

    def test_refuses_unavailable_local_health_endpoint(self) -> None:
        result = self._run(SRO_TEST_CURL_EXIT="7")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not responding", result.stderr)
        self.assertFalse(self.args_path.exists())
        self.assertFalse(self.profile.exists())

    def test_refuses_profile_owned_by_another_user(self) -> None:
        self.profile.mkdir(parents=True)
        result = self._run(SRO_TEST_PROFILE_UID="2000")
        self.assertEqual(result.returncode, 2)
        self.assertIn("belongs to another account", result.stderr)
        self.assertFalse(self.args_path.exists())

    def test_launches_only_with_a_private_operator_profile(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.profile.is_dir())
        self.assertEqual(stat.S_IMODE(self.profile.stat().st_mode), 0o700)
        args = self.args_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(args, ["--no-remote", "--profile", str(self.profile),
                                "http://127.0.0.1:8000/", "about:debugging#/runtime/this-firefox"])


if __name__ == "__main__":
    unittest.main()
