from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db.base import SessionLocal, engine
from app.settings import settings
from tests.postgres_safety import assert_disposable_postgres


@unittest.skipUnless(
    os.environ.get("SRO_TEST_POSTGRES") == "1",
    "requires the disposable PostgreSQL restore drill",
)
class PostgreSQLSessionTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_disposable_postgres()

    def test_each_orm_transaction_receives_bounded_timeouts_and_can_write(self) -> None:
        with (
            patch.object(settings, "database_lock_timeout_ms", 321),
            patch.object(settings, "database_statement_timeout_ms", 4321),
            SessionLocal() as db,
        ):
            values = db.execute(
                text(
                    "SELECT current_setting('lock_timeout'), "
                    "current_setting('statement_timeout'), "
                    "current_setting('transaction_read_only')"
                )
            ).one()
            self.assertEqual(values, ("321ms", "4321ms", "off"))
            db.commit()

            # A transaction-local override must not leak across the pooled session.
            db.execute(text("SELECT set_config('statement_timeout', '1ms', true)"))
            db.commit()
            restored = db.execute(
                text("SELECT current_setting('statement_timeout')")
            ).scalar_one()
            self.assertEqual(restored, "4321ms")

    def test_orm_statement_timeout_cancels_slow_sql(self) -> None:
        with (
            patch.object(settings, "database_statement_timeout_ms", 250),
            SessionLocal() as db,
        ):
            started = time.monotonic()
            with self.assertRaises(OperationalError):
                db.execute(text("SELECT pg_sleep(2)"))
            elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.5)

    def test_orm_lock_timeout_bounds_table_lock_wait(self) -> None:
        with engine.connect() as lock_connection:
            lock_connection.execute(
                text("LOCK TABLE public.task IN ACCESS EXCLUSIVE MODE")
            )
            with (
                patch.object(settings, "database_lock_timeout_ms", 250),
                SessionLocal() as db,
            ):
                started = time.monotonic()
                with self.assertRaises(OperationalError):
                    db.execute(text("SELECT count(*) FROM public.task"))
                elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.5)


if __name__ == "__main__":
    unittest.main()
