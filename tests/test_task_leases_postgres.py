from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from alembic.runtime.migration import MigrationContext
from sqlalchemy import delete, func, select, text, update
from app.cli import compare_postgresql_objects
from app.db.base import SessionLocal, engine, session_scope
from app.db.models import Document, Event, IntakeSource, Task, TaskLease, TaskStatus
from app.services.leases import (
    OCR_MAX_ATTEMPTS,
    OCR_JOB_KIND,
    claim_ocr,
    complete_ocr,
    fail_ocr,
    reconcilable_ocr_task_ids,
)
from app.workers import jobs
from sro_robot.core.models import DebtorCard
from tests.postgres_safety import assert_disposable_postgres


@unittest.skipUnless(
    os.environ.get("SRO_TEST_POSTGRES") == "1",
    "requires the disposable PostgreSQL migration drill",
)
class PostgreSQLTaskLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_disposable_postgres()
        with engine.connect() as connection:
            self.assertEqual(MigrationContext.configure(connection).get_current_revision(), "f28c7a913bd4")
        with session_scope() as db:
            document = Document(
                filename="synthetic-lease.pdf",
                stored_path="/tmp/synthetic-lease.pdf",
                content_hash=os.urandom(32).hex(),
                source=IntakeSource.upload,
            )
            db.add(document)
            db.flush()
            task = Task(
                document_id=document.id,
                scenario="person",
                profile="default",
                status=TaskStatus.new,
                recognized={},
                values={},
                case_number="А00-1/2026",
            )
            db.add(task)
            db.flush()
            self.task_id = task.id

    def test_readiness_checks_migrated_lease_columns_in_postgres(self) -> None:
        from app import main as app_main

        redis_client = MagicMock()
        with patch.object(app_main, "settings") as settings, patch.object(
            app_main, "Redis"
        ) as redis_type, patch.object(
            app_main,
            "Queue",
            side_effect=[
                SimpleNamespace(name="ocr", count=0),
                SimpleNamespace(name="sro", count=0),
            ],
        ), patch.object(app_main, "Worker") as worker_type, patch.object(
            app_main,
            "StartedJobRegistry",
            side_effect=lambda _name, connection: SimpleNamespace(
                get_job_count=lambda cleanup=False: 0
            ),
        ) as started_registry_type:
            settings.ocr_task_leases_enabled = True
            settings.redis_url = "redis://synthetic"
            redis_type.from_url.return_value = redis_client
            worker_type.all.return_value = []
            response = app_main.readyz(None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(started_registry_type.call_count, 2)
        self.assertEqual(
            response.body,
            b'{"ok":true,"checks":{"database":"ok","database_schema":"ok","ocr_lease_schema":"ok","redis":"ok","queue_workers":"ok"}}',
        )
        redis_client.close.assert_called_once_with()

    def test_concurrent_migration_invocation_fails_closed_then_retries(self) -> None:
        # Keep this identifier in sync with migrations.env.MIGRATION_ADVISORY_LOCK_ID.
        lock_id = 74173021533708100
        with engine.connect() as lock_connection:
            acquired = lock_connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": lock_id}
            ).scalar_one()
            lock_connection.commit()
            self.assertTrue(acquired)

            blocked = subprocess.run(
                [sys.executable, "-m", "app.cli", "init-db"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            self.assertIn("миграции не выполнены (RuntimeError)", blocked.stdout)
            self.assertNotIn("Traceback", blocked.stderr)

            released = lock_connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id}
            ).scalar_one()
            lock_connection.commit()
            self.assertTrue(released)

        retried = subprocess.run(
            [sys.executable, "-m", "app.cli", "init-db"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertIn("Схема базы данных обновлена", retried.stdout)

    def test_schema_comparison_detects_enabled_forced_rls_and_policy_in_postgres(self) -> None:
        table = "synthetic_schema_rls_probe"
        function = "synthetic_schema_probe_function"
        with engine.begin() as connection:
            connection.exec_driver_sql(f"CREATE TABLE public.{table} (id integer NOT NULL)")
            try:
                connection.exec_driver_sql(
                    f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"
                )
                connection.exec_driver_sql(
                    f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY"
                )
                connection.exec_driver_sql(
                    f"CREATE POLICY synthetic_owner_policy ON public.{table} USING (id > 0)"
                )
                connection.exec_driver_sql(
                    f"CREATE FUNCTION public.{function}(value integer) "
                    "RETURNS integer LANGUAGE sql IMMUTABLE AS 'SELECT value'"
                )

                differences = compare_postgresql_objects(connection)
            finally:
                connection.exec_driver_sql(
                    f"DROP FUNCTION IF EXISTS public.{function}(integer)"
                )
                connection.exec_driver_sql(f"DROP TABLE public.{table}")

        self.assertIn(f"unexpected row-level security enabled on table {table}", differences)
        self.assertIn(f"unexpected row-level security forced on table {table}", differences)
        self.assertIn(
            f"unexpected row-level security policy {table}.synthetic_owner_policy",
            differences,
        )
        self.assertEqual(len(differences), 4)
        self.assertTrue(
            any(item.startswith(f"unexpected routine public.{function}(") for item in differences),
            differences,
        )

    def _expire(self) -> None:
        with session_scope() as db:
            lease = db.get(TaskLease, self.task_id)
            assert lease is not None
            lease.lease_until = datetime.now(timezone.utc) - timedelta(minutes=1)

    def _faulted_worker_session_scope(self, failed_call: int, *, commit_then_raise: bool):
        calls = 0

        def scope_factory():
            nonlocal calls
            calls += 1
            if calls != failed_call:
                return session_scope()

            @contextmanager
            def faulted_scope():
                if commit_then_raise:
                    with session_scope() as db:
                        yield db
                    raise RuntimeError("synthetic commit acknowledgement loss")
                with session_scope() as db:
                    yield db
                    raise RuntimeError("synthetic transaction rollback")

            return faulted_scope()

        return scope_factory, lambda: calls

    def test_concurrent_duplicate_delivery_has_exactly_one_claim(self) -> None:
        duplicate_deliveries = 20
        barrier = threading.Barrier(duplicate_deliveries)
        claims = []
        errors = []

        def attempt_claim() -> None:
            try:
                barrier.wait(timeout=5)
                with session_scope() as db:
                    claims.append(claim_ocr(db, self.task_id))
            except Exception as exc:  # captured to assert in the parent thread
                errors.append(exc)

        workers = [threading.Thread(target=attempt_claim) for _ in range(duplicate_deliveries)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_expired_claim_is_reclaimed_and_late_result_and_failure_are_fenced(self) -> None:
        with session_scope() as db:
            first = claim_ocr(db, self.task_id)
        self.assertIsNotNone(first)
        self._expire()
        with session_scope() as db:
            second = claim_ocr(db, self.task_id)
        self.assertIsNotNone(second)
        self.assertEqual(second.attempt, 2)
        self.assertGreater(second.generation, first.generation)

        with session_scope() as db:
            self.assertFalse(complete_ocr(db, first, DebtorCard()))
            self.assertFalse(fail_ocr(db, first, "ValueError"))
        with session_scope() as db:
            self.assertTrue(complete_ocr(db, second, DebtorCard()))
            self.assertFalse(complete_ocr(db, second, DebtorCard()))
            self.assertIsNone(claim_ocr(db, self.task_id))

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)

    def test_claim_refreshes_preloaded_task_after_waiting_for_another_worker(self) -> None:
        stale_session = SessionLocal()
        try:
            stale_task = stale_session.get(Task, self.task_id)
            self.assertEqual(stale_task.status, TaskStatus.new)

            with session_scope() as db:
                first = claim_ocr(db, self.task_id)
            self.assertIsNotNone(first)
            with session_scope() as db:
                self.assertTrue(complete_ocr(db, first, DebtorCard()))

            # READ COMMITTED sees the committed row, but the ORM identity map
            # would otherwise keep the pre-claim `new` status and reclaim it.
            self.assertIsNone(claim_ocr(stale_session, self.task_id))
            self.assertEqual(stale_task.status, TaskStatus.needs_review)
            stale_session.rollback()
        finally:
            stale_session.close()

    def test_deleting_task_cascades_its_lease_row(self) -> None:
        with session_scope() as db:
            now = db.scalar(select(func.clock_timestamp()))
            db.add(TaskLease(
                task_id=self.task_id,
                owner_token="synthetic-owner-token",
                job_kind=OCR_JOB_KIND,
                attempt=1,
                generation=1,
                lease_until=now + timedelta(minutes=1),
            ))
            db.flush()
            self.assertIsNotNone(db.get(TaskLease, self.task_id))
            db.execute(delete(Task).where(Task.id == self.task_id))

        with session_scope() as db:
            self.assertIsNone(db.get(TaskLease, self.task_id))

    def test_claim_refreshes_preloaded_lease_after_another_worker_reclaims(self) -> None:
        with session_scope() as db:
            first = claim_ocr(db, self.task_id)
        self.assertIsNotNone(first)
        self._expire()

        stale_session = SessionLocal()
        try:
            stale_task = stale_session.get(Task, self.task_id)
            stale_lease = stale_session.get(TaskLease, self.task_id)
            self.assertEqual(stale_task.status, TaskStatus.recognizing)
            self.assertLessEqual(
                stale_lease.lease_until,
                stale_session.scalar(select(func.clock_timestamp())),
            )

            with session_scope() as db:
                second = claim_ocr(db, self.task_id)
            self.assertIsNotNone(second)
            self.assertEqual(second.attempt, 2)

            # The database row is now actively leased to attempt 2. A stale
            # identity-map lease must not permit attempt 3 to start.
            self.assertIsNone(claim_ocr(stale_session, self.task_id))
            self.assertEqual(stale_task.status, TaskStatus.recognizing)
            self.assertEqual(stale_lease.attempt, 2)
            self.assertEqual(stale_lease.owner_token, second.owner_token)
            stale_session.rollback()
        finally:
            stale_session.close()

    def test_completion_uses_current_database_clock_not_transaction_start(self) -> None:
        with session_scope() as db:
            claim = claim_ocr(db, self.task_id)
        self.assertIsNotNone(claim)

        stale_session = SessionLocal()
        try:
            transaction_started = stale_session.scalar(select(func.now()))
            with session_scope() as db:
                lease_until = db.execute(
                    update(TaskLease)
                    .where(TaskLease.task_id == self.task_id)
                    .values(
                        lease_until=(
                            func.clock_timestamp() + text("interval '250 milliseconds'")
                        )
                    )
                    .returning(TaskLease.lease_until)
                ).scalar_one()

            self.assertLess(transaction_started, lease_until)
            time.sleep(0.4)
            self.assertFalse(complete_ocr(stale_session, claim, DebtorCard()))
            stale_session.rollback()

            with session_scope() as db:
                task = db.get(Task, self.task_id)
                self.assertEqual(task.status, TaskStatus.recognizing)
                self.assertEqual(task.recognized, {})
        finally:
            stale_session.close()

    def test_legacy_recognizing_task_without_lease_is_never_reclaimed(self) -> None:
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            task.status = TaskStatus.recognizing
        with session_scope() as db:
            self.assertIsNone(claim_ocr(db, self.task_id))
            self.assertNotIn(self.task_id, reconcilable_ocr_task_ids(db))

    def test_reconciler_selects_new_and_expired_but_not_active_leases(self) -> None:
        with session_scope() as db:
            self.assertIn(self.task_id, reconcilable_ocr_task_ids(db))
            claim = claim_ocr(db, self.task_id)
        self.assertIsNotNone(claim)

        with session_scope() as db:
            self.assertNotIn(self.task_id, reconcilable_ocr_task_ids(db))
        self._expire()
        with session_scope() as db:
            self.assertIn(self.task_id, reconcilable_ocr_task_ids(db))

    def test_crash_recovery_is_bounded_and_exhaustion_stops_in_failed(self) -> None:
        for expected_attempt in range(1, OCR_MAX_ATTEMPTS + 1):
            with session_scope() as db:
                claim = claim_ocr(db, self.task_id)
            self.assertIsNotNone(claim)
            self.assertEqual(claim.attempt, expected_attempt)
            self._expire()
            with session_scope() as db:
                self.assertIn(self.task_id, reconcilable_ocr_task_ids(db))

        with session_scope() as db:
            self.assertIsNone(claim_ocr(db, self.task_id))
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.failed)
            self.assertIn("attempts exhausted", task.error)

    def test_claim_survives_process_exit_and_completes_after_restart(self) -> None:
        claim_and_exit = """
import os
import sys
from app.db.base import session_scope
from app.services.leases import claim_ocr
with session_scope() as db:
    claim = claim_ocr(db, int(sys.argv[1]))
    assert claim is not None
os._exit(0)
"""
        crashed_worker = subprocess.run(
            [sys.executable, "-c", claim_and_exit, str(self.task_id)],
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(crashed_worker.returncode, 0, crashed_worker.stderr.decode())
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            lease = db.get(TaskLease, self.task_id)
            self.assertEqual(task.status, TaskStatus.recognizing)
            self.assertEqual(lease.attempt, 1)

        self._expire()
        restarted_worker = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import sys
from app.db.base import session_scope
from app.services.leases import claim_ocr, complete_ocr
from app.db.models import TaskStatus
from sro_robot.core.models import DebtorCard
with session_scope() as db:
    claim = claim_ocr(db, int(sys.argv[1]))
    assert claim is not None and claim.attempt == 2
    assert complete_ocr(db, claim, DebtorCard())
""",
                str(self.task_id),
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(restarted_worker.returncode, 0, restarted_worker.stderr.decode())

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            lease = db.get(TaskLease, self.task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(lease.attempt, 2)
            self.assertEqual(lease.generation, 2)

    def test_rq_recognition_handler_uses_fenced_result_and_failure_paths(self) -> None:
        profile = MagicMock()
        profile.parse.return_value = DebtorCard()
        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", True),
            patch.object(jobs, "get_profile", return_value=profile),
        ):
            jobs.recognize(self.task_id)

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            lease = db.get(TaskLease, self.task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(lease.attempt, 1)
            self.assertEqual(lease.generation, 1)
            self.assertLessEqual(lease.lease_until, db.scalar(select(func.clock_timestamp())))

    def test_rq_recognition_failure_uses_generic_fenced_terminal_state(self) -> None:
        profile = MagicMock()
        profile.parse.side_effect = ValueError("sensitive-parse-message")
        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", True),
            patch.object(jobs, "get_profile", return_value=profile),
        ):
            jobs.recognize(self.task_id)

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.failed)
            self.assertEqual(task.error, "Document recognition failed (ValueError)")
            self.assertNotIn("sensitive-parse-message", task.error)

    def test_result_persistence_rollback_leaves_lease_recoverable(self) -> None:
        profile = MagicMock()
        profile.parse.return_value = DebtorCard()
        injected_scope, scope_calls = self._faulted_worker_session_scope(
            2, commit_then_raise=False
        )

        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", True),
            patch.object(jobs, "get_profile", return_value=profile),
            patch.object(jobs, "session_scope", side_effect=injected_scope),
            self.assertLogs(jobs.logger, level="ERROR") as captured,
        ):
            jobs.recognize(self.task_id)

        self.assertEqual(scope_calls(), 2)
        self.assertNotIn("synthetic transaction rollback", "\n".join(captured.output))
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            lease = db.get(TaskLease, self.task_id)
            self.assertEqual(task.status, TaskStatus.recognizing)
            self.assertEqual(task.recognized, {})
            self.assertEqual(task.error, "")
            self.assertEqual(lease.attempt, 1)
            self.assertEqual(lease.generation, 1)
            first_owner = lease.owner_token
            self.assertEqual(
                set(db.scalars(select(Event.kind).where(Event.task_id == self.task_id))),
                {"ocr_claimed"},
            )
            self.assertNotIn(self.task_id, reconcilable_ocr_task_ids(db))

        self._expire()
        with session_scope() as db:
            self.assertIn(self.task_id, reconcilable_ocr_task_ids(db))

        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", True),
            patch.object(jobs, "get_profile", return_value=profile),
        ):
            jobs.recognize(self.task_id)

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            lease = db.get(TaskLease, self.task_id)
            kinds = set(db.scalars(select(Event.kind).where(Event.task_id == self.task_id)))
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(lease.attempt, 2)
            self.assertEqual(lease.generation, 2)
            self.assertNotEqual(lease.owner_token, first_owner)
            self.assertIn("ocr_claim_completed", kinds)
            self.assertNotIn("ocr_claim_failed", kinds)

    def test_result_commit_acknowledgement_loss_does_not_fail_saved_ocr(self) -> None:
        profile = MagicMock()
        profile.parse.return_value = DebtorCard()
        injected_scope, scope_calls = self._faulted_worker_session_scope(
            2, commit_then_raise=True
        )

        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", True),
            patch.object(jobs, "get_profile", return_value=profile),
            patch.object(jobs, "session_scope", side_effect=injected_scope),
            self.assertLogs(jobs.logger, level="ERROR") as captured,
        ):
            jobs.recognize(self.task_id)

        self.assertEqual(scope_calls(), 2)
        self.assertNotIn("synthetic commit acknowledgement loss", "\n".join(captured.output))
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            lease = db.get(TaskLease, self.task_id)
            kinds = set(db.scalars(select(Event.kind).where(Event.task_id == self.task_id)))
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(lease.attempt, 1)
            self.assertEqual(lease.generation, 1)
            self.assertIn("recognized", kinds)
            self.assertIn("ocr_claim_completed", kinds)
            self.assertNotIn("ocr_claim_failed", kinds)

    def test_parse_failure_persistence_rollback_leaves_lease_recoverable(self) -> None:
        profile = MagicMock()
        profile.parse.side_effect = [ValueError("synthetic private parser detail"), DebtorCard()]
        injected_scope, scope_calls = self._faulted_worker_session_scope(
            2, commit_then_raise=False
        )

        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", True),
            patch.object(jobs, "get_profile", return_value=profile),
            patch.object(jobs, "session_scope", side_effect=injected_scope),
            self.assertLogs(jobs.logger, level="ERROR") as captured,
        ):
            jobs.recognize(self.task_id)

        self.assertEqual(scope_calls(), 2)
        self.assertNotIn("synthetic private parser detail", "\n".join(captured.output))
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            lease = db.get(TaskLease, self.task_id)
            self.assertEqual(task.status, TaskStatus.recognizing)
            self.assertEqual(task.error, "")
            self.assertEqual(lease.attempt, 1)
            self.assertEqual(
                set(db.scalars(select(Event.kind).where(Event.task_id == self.task_id))),
                {"ocr_claimed"},
            )

        self._expire()
        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", True),
            patch.object(jobs, "get_profile", return_value=profile),
        ):
            jobs.recognize(self.task_id)

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            lease = db.get(TaskLease, self.task_id)
            kinds = set(db.scalars(select(Event.kind).where(Event.task_id == self.task_id)))
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(lease.attempt, 2)
            self.assertIn("ocr_claim_completed", kinds)
            self.assertNotIn("ocr_claim_failed", kinds)

    def test_parse_failure_commit_acknowledgement_loss_preserves_saved_failure(self) -> None:
        profile = MagicMock()
        profile.parse.side_effect = ValueError("synthetic private parser detail")
        injected_scope, scope_calls = self._faulted_worker_session_scope(
            2, commit_then_raise=True
        )

        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", True),
            patch.object(jobs, "get_profile", return_value=profile),
            patch.object(jobs, "session_scope", side_effect=injected_scope),
            self.assertLogs(jobs.logger, level="ERROR") as captured,
        ):
            jobs.recognize(self.task_id)

        self.assertEqual(scope_calls(), 2)
        self.assertNotIn("synthetic private parser detail", "\n".join(captured.output))
        self.assertNotIn("synthetic commit acknowledgement loss", "\n".join(captured.output))
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            lease = db.get(TaskLease, self.task_id)
            kinds = set(db.scalars(select(Event.kind).where(Event.task_id == self.task_id)))
            self.assertEqual(task.status, TaskStatus.failed)
            self.assertEqual(task.error, "Document recognition failed (ValueError)")
            self.assertEqual(lease.attempt, 1)
            self.assertIn("ocr_claim_failed", kinds)
            self.assertNotIn("ocr_claim_completed", kinds)

    def test_legacy_result_rollback_preserves_unknown_recognizing_state(self) -> None:
        profile = MagicMock()
        profile.parse.return_value = DebtorCard()
        injected_scope, scope_calls = self._faulted_worker_session_scope(
            2, commit_then_raise=False
        )

        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", False),
            patch.object(jobs, "get_profile", return_value=profile),
            patch.object(jobs, "session_scope", side_effect=injected_scope),
            self.assertLogs(jobs.logger, level="ERROR") as captured,
        ):
            jobs.recognize(self.task_id)

        self.assertEqual(scope_calls(), 2)
        self.assertNotIn("synthetic transaction rollback", "\n".join(captured.output))
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.recognizing)
            self.assertEqual(task.recognized, {})
            self.assertEqual(task.error, "")
            self.assertIsNone(db.get(TaskLease, self.task_id))
            self.assertNotIn(self.task_id, reconcilable_ocr_task_ids(db))

    def test_claim_commit_acknowledgement_loss_leaves_work_for_reconciliation(self) -> None:
        profile = MagicMock()
        profile.parse.return_value = DebtorCard()
        injected_scope, scope_calls = self._faulted_worker_session_scope(
            1, commit_then_raise=True
        )

        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", True),
            patch.object(jobs, "get_profile", return_value=profile),
            patch.object(jobs, "session_scope", side_effect=injected_scope),
            self.assertLogs(jobs.logger, level="ERROR"),
        ):
            jobs.recognize(self.task_id)

        self.assertEqual(scope_calls(), 1)
        profile.parse.assert_not_called()
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            lease = db.get(TaskLease, self.task_id)
            first_owner = lease.owner_token
            self.assertEqual(task.status, TaskStatus.recognizing)
            self.assertEqual(lease.attempt, 1)
            self.assertNotIn(self.task_id, reconcilable_ocr_task_ids(db))

        self._expire()
        with session_scope() as db:
            self.assertIn(self.task_id, reconcilable_ocr_task_ids(db))

        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", True),
            patch.object(jobs, "get_profile", return_value=profile),
        ):
            jobs.recognize(self.task_id)

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            lease = db.get(TaskLease, self.task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(lease.attempt, 2)
            self.assertNotEqual(lease.owner_token, first_owner)


if __name__ == "__main__":
    unittest.main()
