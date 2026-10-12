from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from app.db.base import engine
from tests.postgres_safety import assert_disposable_postgres


REPO_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("SRO_TEST_POSTGRES") == "1",
    "requires the disposable PostgreSQL migration drill",
)
class PostgreSQLMigrationLockTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_disposable_postgres()

    def _run_blocked_command(self, command: list[str]) -> tuple[subprocess.CompletedProcess, float]:
        with engine.connect() as lock_connection:
            lock_connection.execute(
                text("LOCK TABLE public.alembic_version IN ACCESS EXCLUSIVE MODE")
            )
            environment = {
                **os.environ,
                "DB_LOCK_TIMEOUT_MS": "500",
            }
            started = time.monotonic()
            result = subprocess.run(
                command,
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            elapsed = time.monotonic() - started
        return result, elapsed

    def _assert_bounded_lock_failure(
        self, result: subprocess.CompletedProcess, elapsed: float
    ) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 3)

    def _assert_schema_head_unchanged(self) -> None:
        with engine.connect() as connection:
            revision = MigrationContext.configure(connection).get_current_revision()
        self.assertEqual(revision, "d92f41b7a6c3")

    def test_init_db_preflight_fails_closed_on_a_held_schema_lock(self) -> None:
        result, elapsed = self._run_blocked_command(
            [sys.executable, "-m", "app.cli", "init-db"]
        )

        self._assert_bounded_lock_failure(result, elapsed)
        self.assertIn("не удалось безопасно проверить состояние схемы", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self._assert_schema_head_unchanged()

    def test_alembic_migration_transaction_fails_bounded_on_a_held_schema_lock(self) -> None:
        result, elapsed = self._run_blocked_command(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(REPO_ROOT / "alembic.ini"),
                "upgrade",
                "head",
            ]
        )

        self._assert_bounded_lock_failure(result, elapsed)
        self.assertIn("lock timeout", result.stderr.lower())
        self._assert_schema_head_unchanged()


if __name__ == "__main__":
    unittest.main()
