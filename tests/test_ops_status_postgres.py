from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from sqlalchemy import text

from app.db.base import engine
from tests.postgres_safety import assert_disposable_postgres


REPO_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("SRO_TEST_POSTGRES") == "1",
    "requires the disposable PostgreSQL restore drill",
)
class PostgreSQLOpsStatusTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_disposable_postgres()

    def test_ops_status_fails_bounded_when_task_snapshot_is_blocked(self) -> None:
        with engine.connect() as lock_connection:
            lock_connection.execute(
                text("LOCK TABLE public.task IN ACCESS EXCLUSIVE MODE")
            )
            environment = {
                **os.environ,
                "DB_LOCK_TIMEOUT_MS": "500",
                "DB_STATEMENT_TIMEOUT_MS": "2000",
            }
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, "-m", "app.cli", "ops-status"],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Проверка задач недоступна: OperationalError", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 3)


if __name__ == "__main__":
    unittest.main()
