from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from alembic.runtime.migration import MigrationContext
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.db.base import SessionLocal, engine, session_scope
from app.db.models import Document, Event, IntakeSource, Task, TaskStatus, User
from app.services import runner, tasks
from app.services.tasks import approval_matches
from tests.postgres_safety import assert_disposable_postgres


@unittest.skipUnless(
    os.environ.get("SRO_TEST_POSTGRES") == "1",
    "requires the disposable PostgreSQL migration drill",
)
class WorkflowConcurrencyPostgreSQLTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_disposable_postgres()
        with engine.connect() as connection:
            self.assertEqual(MigrationContext.configure(connection).get_current_revision(), "a41c72d93e10")
        with session_scope() as db:
            user = User(
                login=f"synthetic-concurrency-{os.urandom(8).hex()}",
                full_name="Synthetic Concurrency User",
                password_hash="synthetic-only",
                sro_login="synthetic-concurrency",
                is_active=True,
            )
            db.add(user)
            db.flush()
            document = Document(
                filename="synthetic-concurrency.pdf",
                stored_path="/tmp/synthetic-concurrency.pdf",
                content_hash=os.urandom(32).hex(),
                source=IntakeSource.upload,
                uploaded_by_id=user.id,
            )
            db.add(document)
            db.flush()
            self.document_id = document.id
            task = Task(
                document_id=document.id,
                scenario="person",
                profile="default",
                status=TaskStatus.needs_review,
                recognized={},
                values={},
                case_number="А00-2/2026",
                assignee_id=user.id,
            )
            db.add(task)
            db.flush()
            self.task_id = task.id
            self.user_id = user.id

    def test_legacy_ocr_claim_is_atomic_and_never_reclaims_review_state(self) -> None:
        with session_scope() as db:
            queued = Task(
                document_id=self.document_id,
                scenario="person",
                profile="default",
                status=TaskStatus.new,
                recognized={},
                values={},
                assignee_id=self.user_id,
            )
            db.add(queued)
            db.flush()
            queued_task_id = queued.id

        worker_count = 8
        barrier = threading.Barrier(worker_count)
        claims: list[bool] = []
        errors: list[Exception] = []

        def claim() -> None:
            try:
                barrier.wait(timeout=5)
                with SessionLocal() as db:
                    task = db.get(Task, queued_task_id)
                    claims.append(tasks.claim_unleased_task(db, task))
                    db.commit()
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=claim) for _ in range(worker_count)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)

        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sum(claims), 1)
        with session_scope() as db:
            queued = db.get(Task, queued_task_id)
            self.assertEqual(queued.status, TaskStatus.recognizing)

            reviewed = db.get(Task, self.task_id)
            self.assertEqual(reviewed.status, TaskStatus.needs_review)
            self.assertFalse(tasks.claim_unleased_task(db, reviewed))
            self.assertEqual(reviewed.status, TaskStatus.needs_review)

    def test_stale_concurrent_edit_invalidates_approval_at_commit(self) -> None:
        approving_session = SessionLocal()
        editing_session = SessionLocal()
        try:
            approving_task = approving_session.get(Task, self.task_id)
            editing_task = editing_session.get(Task, self.task_id)
            self.assertEqual(approving_task.status, TaskStatus.needs_review)
            self.assertEqual(editing_task.status, TaskStatus.needs_review)

            tasks.save_values(approving_session, approving_task, {"debtor_name": "Synthetic A"}, self.user_id)
            tasks.approve(approving_session, approving_task, self.user_id)
            approving_session.commit()

            # The edit session read the old state before approval committed. The
            # service must refresh under a row lock before deciding the transition.
            tasks.save_values(editing_session, editing_task, {"debtor_name": "Synthetic B"}, self.user_id)
            editing_session.commit()
        finally:
            approving_session.close()
            editing_session.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(task.values, {"debtor_name": "Synthetic B"})
            self.assertFalse(approval_matches(db, task))
            self.assertIsNone(task.approved_by_id)

            with self.assertRaises(runner.NotApproved):
                runner.run_task(db, task, live=False, user_id=self.user_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertIsNone(task.approved_by_id)

    def test_stale_failure_cannot_overwrite_newer_terminal_task_state(self) -> None:
        writer = SessionLocal()
        concurrent = SessionLocal()
        try:
            stale_task = writer.get(Task, self.task_id)
            self.assertIsNotNone(stale_task)
            stale_task.status = TaskStatus.recognizing
            writer.commit()

            terminal_task = concurrent.get(Task, self.task_id)
            self.assertIsNotNone(terminal_task)
            terminal_task.status = TaskStatus.done
            terminal_task.error = ""
            concurrent.commit()

            self.assertFalse(tasks.fail(writer, stale_task, "synthetic stale parser failure"))
            writer.commit()
        finally:
            writer.close()
            concurrent.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.done)
            self.assertEqual(task.error, "")
            failed_events = db.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.task_id == self.task_id,
                    Event.kind == "failed",
                )
            )
            self.assertEqual(failed_events, 0)

    def test_edit_waits_for_local_runner_and_cannot_overwrite_completion(self) -> None:
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            tasks.save_values(db, task, {"debtor_name": "Synthetic A"}, self.user_id)
            tasks.approve(db, task, self.user_id)

        runner_session = SessionLocal()
        edit_session = SessionLocal()
        entered_validation = threading.Event()
        release_validation = threading.Event()
        edit_finished = threading.Event()
        errors: list[BaseException] = []
        running_task = runner_session.get(Task, self.task_id)
        editing_task = edit_session.get(Task, self.task_id)

        class BlockingScenario:
            def validate(self, _card):
                entered_validation.set()
                if not release_validation.wait(timeout=5):
                    raise TimeoutError("synthetic validation barrier timed out")
                return []

        def run() -> None:
            try:
                runner.run_task(runner_session, running_task, user_id=self.user_id)
                runner_session.commit()
            except BaseException as exc:
                errors.append(exc)
                runner_session.rollback()

        def edit() -> None:
            try:
                tasks.save_values(
                    edit_session,
                    editing_task,
                    {"debtor_name": "Synthetic B"},
                    self.user_id,
                )
                edit_session.commit()
            except BaseException as exc:
                errors.append(exc)
                edit_session.rollback()
            finally:
                edit_finished.set()

        try:
            with (
                patch.object(runner, "scenario_of", return_value=BlockingScenario()),
                patch.object(runner, "card_of", return_value=object()),
            ):
                runner_thread = threading.Thread(target=run)
                runner_thread.start()
                self.assertTrue(entered_validation.wait(timeout=5))

                edit_thread = threading.Thread(target=edit)
                edit_thread.start()
                self.assertFalse(edit_finished.wait(timeout=0.15))
                release_validation.set()
                runner_thread.join(timeout=5)
                edit_thread.join(timeout=5)

            self.assertFalse(runner_thread.is_alive())
            self.assertFalse(edit_thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ValueError)
            with session_scope() as db:
                task = db.get(Task, self.task_id)
                self.assertEqual(task.status, TaskStatus.done)
                self.assertEqual(task.values, {"debtor_name": "Synthetic A"})
        finally:
            release_validation.set()
            runner_session.close()
            edit_session.close()

    def test_operator_row_lock_wait_is_bounded_and_leaves_task_unchanged(self) -> None:
        blocker = SessionLocal()
        contender = SessionLocal()
        try:
            blocker.scalar(select(Task).where(Task.id == self.task_id).with_for_update())
            stale_task = contender.get(Task, self.task_id)
            with patch.object(tasks.settings, "database_lock_timeout_ms", 300):
                with self.assertRaises(OperationalError):
                    tasks.save_values(
                        contender,
                        stale_task,
                        {"debtor_name": "Synthetic blocked edit"},
                        self.user_id,
                    )
            contender.rollback()
        finally:
            blocker.rollback()
            blocker.close()
            contender.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(task.values, {})

    def test_database_rejects_login_that_differs_only_by_case(self) -> None:
        login = f"Synthetic-Identity-{os.urandom(8).hex()}"
        with session_scope() as db:
            db.add(User(
                login=login,
                full_name="Synthetic Identity User",
                password_hash="synthetic-only",
                sro_login="synthetic-identity",
                is_active=True,
            ))

        with self.assertRaises(IntegrityError):
            with session_scope() as db:
                db.add(User(
                    login=login.swapcase(),
                    full_name="Synthetic Case Variant",
                    password_hash="synthetic-only",
                    sro_login="synthetic-identity-case",
                    is_active=True,
                ))
                db.flush()

        with session_scope() as db:
            self.assertEqual(
                db.scalar(select(func.count(User.id)).where(func.lower(User.login) == login.lower())),
                1,
            )

    def test_audit_events_reject_update_delete_and_truncate_but_allow_insert(self) -> None:
        with session_scope() as db:
            event = Event(
                task_id=self.task_id,
                user_id=self.user_id,
                kind="synthetic_audit_guard",
                message="original",
                payload={"marker": "preserve"},
            )
            db.add(event)
            db.flush()
            event_id = event.id

        with self.assertRaises(DBAPIError):
            with session_scope() as db:
                db.execute(
                    update(Event).where(Event.id == event_id).values(message="tampered")
                )

        with self.assertRaises(DBAPIError):
            with session_scope() as db:
                db.execute(delete(Event).where(Event.id == event_id))

        with self.assertRaises(DBAPIError):
            with session_scope() as db:
                db.execute(text("TRUNCATE TABLE public.event"))

        with session_scope() as db:
            saved = db.get(Event, event_id)
            self.assertEqual(saved.message, "original")
            self.assertEqual(saved.payload, {"marker": "preserve"})
            db.add(Event(
                task_id=self.task_id,
                user_id=self.user_id,
                kind="synthetic_audit_guard_append",
                message="append remains allowed",
                payload={},
            ))

    def test_concurrent_same_content_intake_creates_one_document_row(self) -> None:
        worker_count = 8
        barrier = threading.Barrier(worker_count)
        document_ids: list[int] = []
        errors: list[Exception] = []
        content = b"%PDF-1.7 synthetic document admission concurrency probe"
        content_hash = hashlib.sha256(content).hexdigest()

        def intake(worker_id: int) -> None:
            try:
                barrier.wait(timeout=5)
                with SessionLocal() as db:
                    document = tasks.store_document(
                        db,
                        f"synthetic-intake-{worker_id}.pdf",
                        content,
                        IntakeSource.upload,
                    )
                    db.commit()
                    document_ids.append(document.id)
            except Exception as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            storage.chmod(0o700)
            with patch.object(tasks.settings, "upload_dir", storage):
                workers = [threading.Thread(target=intake, args=(i,)) for i in range(worker_count)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=15)

        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(document_ids), worker_count)
        self.assertEqual(len(set(document_ids)), 1)
        with session_scope() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count(Document.id)).where(Document.content_hash == content_hash)
                ),
                1,
            )

    def test_database_rejects_duplicate_document_hash_without_service_lock(self) -> None:
        content_hash = hashlib.sha256(b"synthetic unique document hash probe").hexdigest()
        with session_scope() as db:
            db.add(
                Document(
                    filename="synthetic-first.pdf",
                    stored_path="/tmp/synthetic-first.pdf",
                    content_hash=content_hash,
                    source=IntakeSource.upload,
                )
            )
        with SessionLocal() as db:
            db.add(
                Document(
                    filename="synthetic-duplicate.pdf",
                    stored_path="/tmp/synthetic-duplicate.pdf",
                    content_hash=content_hash,
                    source=IntakeSource.upload,
                )
            )
            with self.assertRaises(IntegrityError):
                db.flush()
            db.rollback()


if __name__ == "__main__":
    unittest.main()
