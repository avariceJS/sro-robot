from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RestoreDrillCleanupTests(unittest.TestCase):
    def test_both_drills_wait_for_final_server_before_sql(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        scripts = (
            "scripts/dev/postgres-restore-drill.sh",
            "scripts/dev/postgres-runtime-role-smoke.sh",
        )
        for script in scripts:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as sandbox:
                sandbox_path = Path(sandbox)
                fake_bin = sandbox_path / "bin"
                fake_bin.mkdir()
                state = sandbox_path / "probes"
                ready = sandbox_path / "final-ready"
                upload_dir = Path("/tmp") / f"sro-runtime-role.readiness-{os.getpid()}"
                if "restore" in script:
                    upload_dir = Path("/tmp") / f"sro-restore-drill.readiness-{os.getpid()}"
                upload_dir.mkdir(mode=0o700)
                (fake_bin / "mktemp").write_text(f"#!/bin/sh\nprintf '%s\\n' '{upload_dir}'\n")
                (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n")
                # Model the image's socket-only bootstrap server: a socket probe
                # succeeds early, but TCP accepts connections only on attempt 3.
                docker_source = f"""#!{sys.executable}
import sys
from pathlib import Path
args = sys.argv[1:]
state = Path({str(state)!r})
ready = Path({str(ready)!r})
if args[0] in ('run', 'rm'):
    sys.exit(0)
if args[0] == 'port':
    print('127.0.0.1:54321')
    sys.exit(0)
if args[0] == 'inspect':
    print('postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea')
    sys.exit(0)
if args[0] == 'exec' and args[2] == 'pg_isready':
    probes = int(state.read_text()) + 1 if state.exists() else 1
    state.write_text(str(probes))
    if '-h' not in args or args[args.index('-h') + 1] != '127.0.0.1':
        sys.exit(0)
    if probes < 3:
        sys.exit(1)
    ready.touch()
    sys.exit(0)
if args[0] == 'exec' and args[2] in ('psql', 'createdb'):
    sys.exit(79 if ready.exists() else 80)
sys.exit(81)
"""
                (fake_bin / "docker").write_text(docker_source)
                for executable in fake_bin.iterdir():
                    executable.chmod(0o700)
                environment = os.environ.copy()
                environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
                result = subprocess.run(
                    ["/bin/bash", script], cwd=repo_root, env=environment,
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 79, result.stderr)
                self.assertEqual(state.read_text(), "3")
                self.assertFalse(upload_dir.exists())

    def _run_failure_cleanup(
        self,
        upload_dir: Path,
        script: str = "scripts/dev/postgres-restore-drill.sh",
        signal_name: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="sro-drill-cleanup-") as sandbox:
            sandbox_path = Path(sandbox)
            fake_bin = sandbox_path / "bin"
            fake_bin.mkdir()

            mktemp = fake_bin / "mktemp"
            mktemp.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' '{upload_dir}'\n"
            )
            mktemp.chmod(0o700)
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = run ]; then\n"
                "  if [ -n \"${SRO_TEST_SIGNAL:-}\" ]; then\n"
                "    kill -\"$SRO_TEST_SIGNAL\" \"$PPID\"\n"
                "    sleep 1\n"
                "  fi\n"
                "  exit 23\n"
                "fi\n"
                "exit 0\n"
            )
            docker.chmod(0o700)

            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            if signal_name:
                environment["SRO_TEST_SIGNAL"] = signal_name
            return subprocess.run(
                ["/bin/bash", script],
                cwd=repo_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )


    def test_failure_removes_private_upload_directory_contents(self) -> None:
        upload_dir = Path("/tmp") / f"sro-restore-drill.test-{os.getpid()}"
        upload_dir.mkdir(mode=0o700)
        (upload_dir / "synthetic-private-document").write_text("synthetic")

        result = self._run_failure_cleanup(upload_dir)

        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertFalse(upload_dir.exists())

    def test_cleanup_unlinks_replaced_symlink_without_removing_its_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sro-drill-target-") as target:
            target_dir = Path(target)
            marker = target_dir / "preserve-me"
            marker.write_text("synthetic")
            upload_dir = Path("/tmp") / f"sro-restore-drill.test-link-{os.getpid()}"
            upload_dir.symlink_to(target_dir, target_is_directory=True)

            result = self._run_failure_cleanup(upload_dir)

            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertFalse(upload_dir.exists())
            self.assertFalse(upload_dir.is_symlink())
            self.assertEqual(marker.read_text(), "synthetic")

    def test_interrupt_and_sigterm_exit_and_clean_private_directories_for_both_drills(self) -> None:
        cases = (
            ("scripts/dev/postgres-restore-drill.sh", "sro-restore-drill"),
            ("scripts/dev/postgres-runtime-role-smoke.sh", "sro-runtime-role"),
        )
        signals = (("INT", 130), ("TERM", 143))
        for script, prefix in cases:
            for signal_name, expected_code in signals:
                with self.subTest(script=script, signal=signal_name):
                    upload_dir = Path("/tmp") / f"{prefix}.test-{signal_name.lower()}-{os.getpid()}"
                    upload_dir.mkdir(mode=0o700)
                    (upload_dir / "synthetic-private-document").write_text("synthetic")

                    result = self._run_failure_cleanup(
                        upload_dir,
                        script=script,
                        signal_name=signal_name,
                    )

                    self.assertEqual(result.returncode, expected_code, result.stderr)
                    self.assertFalse(upload_dir.exists())


if __name__ == "__main__":
    unittest.main()
