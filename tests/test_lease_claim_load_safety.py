from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "lease_claim_load_smoke.py"


class LeaseClaimLoadSafetyTests(unittest.TestCase):
    def run_helper(self, *, disposable_marker: bool, database_url: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as upload_dir:
            environment = {
                **os.environ,
                "APP_DEBUG": "1",
                "DATABASE_URL": database_url,
                "SRO_UPLOAD_DIR": upload_dir,
            }
            if disposable_marker:
                environment["SRO_DISPOSABLE_DRILL"] = "1"
            else:
                environment.pop("SRO_DISPOSABLE_DRILL", None)
            return subprocess.run(
                [sys.executable, str(SCRIPT)],
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
                check=False,
            )

    def test_refuses_synthetic_writes_without_explicit_drill_marker(self) -> None:
        result = self.run_helper(
            disposable_marker=False,
            database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:5432/sro",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("disposable-drill marker", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_refuses_non_loopback_postgres_even_with_drill_marker(self) -> None:
        result = self.run_helper(
            disposable_marker=True,
            database_url="postgresql+psycopg://synthetic:synthetic@db.invalid/sro",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("via loopback", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
