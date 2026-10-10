from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class RestoreDrillCleanupTests(unittest.TestCase):
    def test_failure_removes_private_upload_directory_contents(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="sro-drill-cleanup-") as sandbox:
            sandbox_path = Path(sandbox)
            fake_bin = sandbox_path / "bin"
            fake_bin.mkdir()

            upload_dir = Path("/tmp") / f"sro-restore-drill.test-{os.getpid()}"
            upload_dir.mkdir(mode=0o700)
            (upload_dir / "synthetic-private-document").write_text("synthetic")

            mktemp = fake_bin / "mktemp"
            mktemp.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' '{upload_dir}'\n"
            )
            mktemp.chmod(0o700)
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = run ]; then exit 23; fi\n"
                "exit 0\n"
            )
            docker.chmod(0o700)

            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            result = subprocess.run(
                ["/bin/bash", "scripts/dev/postgres-restore-drill.sh"],
                cwd=repo_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertFalse(upload_dir.exists())


if __name__ == "__main__":
    unittest.main()
