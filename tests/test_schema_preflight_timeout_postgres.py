from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app import main
from app.db.base import engine
from app.db import schema
from app.db.schema import configure_read_only_diagnostic_transaction
from app.workers import jobs
from scripts.dev import run_worker
from tests.postgres_safety import assert_disposable_postgres


@unittest.skipUnless(
    os.environ.get("SRO_TEST_POSTGRES") == "1",
    "requires the disposable PostgreSQL restore drill",
)
class PostgreSQLSchemaPreflightTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_disposable_postgres()

    def test_readiness_and_both_worker_schema_preflights_fail_bounded_on_ddl_lock(self) -> None:
        redis_client = MagicMock()
        queues = [SimpleNamespace(name="ocr", count=0), SimpleNamespace(name="sro", count=0)]
        started_registry = MagicMock()
        started_registry.get_job_count.return_value = 0
        with engine.connect() as lock_connection:
            lock_connection.execute(
                text("LOCK TABLE public.alembic_version IN ACCESS EXCLUSIVE MODE")
            )
            started = time.monotonic()
            with (
                patch.object(schema.settings, "database_lock_timeout_ms", 500),
                patch.object(schema.settings, "database_statement_timeout_ms", 2000),
                patch.object(main.Redis, "from_url", return_value=redis_client),
                patch.object(main, "Queue", side_effect=queues),
                patch.object(main, "StartedJobRegistry", return_value=started_registry),
                patch.object(main.Worker, "all", return_value=[]),
            ):
                response = main.readyz(None)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    response.body.count(b'"database_schema":"unavailable"'),
                    1,
                )
                redis_client.close.assert_called_once_with()

                with self.assertRaises(run_worker.WorkerSchemaUnavailable):
                    run_worker.require_current_schema()

                with self.assertLogs(jobs.logger, level="ERROR") as captured:
                    with self.assertRaises(jobs.WorkerSchemaUnavailable):
                        jobs._require_current_worker_schema(918273)
                self.assertIn("task_id=918273", captured.output[0])
                self.assertNotIn("alembic_version", captured.output[0])
            elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 1.2)
        self.assertLess(elapsed, 4)

    def test_diagnostic_statement_timeout_cancels_a_slow_query(self) -> None:
        with (
            patch.object(schema.settings, "database_lock_timeout_ms", 500),
            patch.object(schema.settings, "database_statement_timeout_ms", 250),
            engine.connect() as connection,
        ):
            configure_read_only_diagnostic_transaction(connection)
            started = time.monotonic()
            with self.assertRaises(OperationalError):
                connection.execute(text("SELECT pg_sleep(2)"))
            elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.5)


if __name__ == "__main__":
    unittest.main()
