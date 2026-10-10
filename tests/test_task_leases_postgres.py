from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, select, text
from app.db.base import engine, session_scope
from app.db.models import Document, IntakeSource, Task, TaskLease, TaskStatus
from app.services.leases import (
    OCR_MAX_ATTEMPTS,
    claim_ocr,
    complete_ocr,
    fail_ocr,
    reconcilable_ocr_task_ids,
)
from app.workers import jobs
from sro_robot.core.models import DebtorCard


@unittest.skipUnless(
    os.environ.get("SRO_TEST_POSTGRES") == "1",
    "requires the disposable PostgreSQL migration drill",
)
class PostgreSQLTaskLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        with engine.connect() as connection:
            self.assertEqual(MigrationContext.configure(connection).get_current_revision(), "7af0f5c2d441")
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

    def _expire(self) -> None:
        with session_scope() as db:
            lease = db.get(TaskLease, self.task_id)
            assert lease is not None
            lease.lease_until = datetime.now(timezone.utc) - timedelta(minutes=1)

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
            self.assertLessEqual(lease.lease_until, db.scalar(select(func.now())))

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


if __name__ == "__main__":
    unittest.main()
