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
from app.db.migration_lock import MIGRATION_ADVISORY_LOCK_ID
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
        self.assertEqual(revision, "6d8f31a4c920")

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

    def test_concurrent_alembic_process_refuses_held_migration_advisory_lock(self) -> None:
        command = [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(REPO_ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ]
        with engine.connect() as lock_connection:
            acquired = lock_connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": MIGRATION_ADVISORY_LOCK_ID},
            ).scalar_one()
            self.assertTrue(acquired)
            try:
                started = time.monotonic()
                refused = subprocess.run(
                    command,
                    cwd=REPO_ROOT,
                    env=os.environ,
                    capture_output=True,
                    text=True,
                    timeout=4,
                    check=False,
                )
                elapsed = time.monotonic() - started

                self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
                self.assertLess(elapsed, 3)
                self.assertIn(
                    "Another SRO Robot schema migration is already running",
                    refused.stderr,
                )
                revision = lock_connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                self.assertEqual(revision, "6d8f31a4c920")
            finally:
                released = lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": MIGRATION_ADVISORY_LOCK_ID},
                ).scalar_one()
                lock_connection.commit()
                self.assertTrue(released)

        retried = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=os.environ,
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
        )
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self._assert_schema_head_unchanged()


if __name__ == "__main__":
    unittest.main()
