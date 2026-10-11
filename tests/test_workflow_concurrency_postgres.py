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
from app.db.models import Document, Event, IntakeOperation, IntakeSource, Task, TaskStatus, User
from app.services import runner, tasks
from app.services.tasks import approval_matches
from tests.postgres_safety import assert_disposable_postgres
from sro_robot.core.models import DebtorCard


@unittest.skipUnless(
    os.environ.get("SRO_TEST_POSTGRES") == "1",
    "requires the disposable PostgreSQL migration drill",
)
class WorkflowConcurrencyPostgreSQLTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_disposable_postgres()
        with engine.connect() as connection:
            self.assertEqual(MigrationContext.configure(connection).get_current_revision(), "c73b8e4a19d2")
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

    def test_stale_ocr_result_cannot_overwrite_newer_terminal_task_state(self) -> None:
        setup = SessionLocal()
        stale_worker = SessionLocal()
        concurrent = SessionLocal()
        try:
            task = setup.get(Task, self.task_id)
            task.status = TaskStatus.recognizing
            setup.commit()

            stale_task = stale_worker.get(Task, self.task_id)
            self.assertEqual(stale_task.status, TaskStatus.recognizing)

            terminal_task = concurrent.get(Task, self.task_id)
            terminal_task.status = TaskStatus.done
            concurrent.commit()

            self.assertFalse(tasks.save_recognized(stale_worker, stale_task, DebtorCard()))
            stale_worker.commit()
        finally:
            setup.close()
            stale_worker.close()
            concurrent.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.done)
            self.assertEqual(task.recognized, {})
            recognized_events = db.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.task_id == self.task_id,
                    Event.kind == "recognized",
                )
            )
            self.assertEqual(recognized_events, 0)

    def test_intake_idempotency_reuses_tasks_and_rejects_changed_manifest(self) -> None:
        key = os.urandom(24).hex()
        manifest = tasks.intake_manifest(
            "upload", [hashlib.sha256(b"synthetic intake content").hexdigest()]
        )
        with session_scope() as db:
            operation, created = tasks.begin_intake_operation(
                db,
                user_id=self.user_id,
                idempotency_key=key,
                manifest_sha256=manifest,
            )
            self.assertTrue(created)
            document = db.get(Document, self.document_id)
            task = tasks.create_task(
                db,
                document,
                user_id=self.user_id,
                intake_operation_id=operation.id,
            )
            operation_id = operation.id
            task_id = task.id

        with session_scope() as db:
            replay, created = tasks.begin_intake_operation(
                db,
                user_id=self.user_id,
                idempotency_key=key,
                manifest_sha256=manifest,
            )
            self.assertFalse(created)
            self.assertEqual(replay.id, operation_id)
            linked_tasks = tasks.operation_tasks(db, replay.id)
            self.assertEqual([row.id for row in linked_tasks], [task_id])
            tasks.log_intake_replay(db, linked_tasks, self.user_id, "a" * 32)

        with session_scope() as db:
            with self.assertRaises(tasks.IntakeOperationConflict):
                tasks.begin_intake_operation(
                    db,
                    user_id=self.user_id,
                    idempotency_key=key,
                    manifest_sha256=tasks.intake_manifest(
                        "upload", [hashlib.sha256(b"different input").hexdigest()]
                    ),
                )

        with session_scope() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(IntakeOperation).where(
                        IntakeOperation.id == operation_id
                    )
                ),
                1,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(Task).where(
                        Task.intake_operation_id == operation_id
                    )
                ),
                1,
            )
            replay_events = db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id,
                    Event.kind == "intake_replayed",
                    Event.user_id == self.user_id,
                )
            )
            self.assertEqual(replay_events, 1)

    def test_concurrent_same_key_admission_creates_exactly_one_operation_and_task(self) -> None:
        key = os.urandom(24).hex()
        manifest = tasks.intake_manifest(
            "upload", [hashlib.sha256(b"synthetic concurrent intake").hexdigest()]
        )
        barrier = threading.Barrier(2)
        results: list[tuple[bool, int, int]] = []
        errors: list[Exception] = []

        def admit() -> None:
            try:
                with SessionLocal() as db:
                    barrier.wait(timeout=5)
                    operation, created = tasks.begin_intake_operation(
                        db,
                        user_id=self.user_id,
                        idempotency_key=key,
                        manifest_sha256=manifest,
                    )
                    if created:
                        task = tasks.create_task(
                            db,
                            db.get(Document, self.document_id),
                            user_id=self.user_id,
                            intake_operation_id=operation.id,
                        )
                        task_id = task.id
                    else:
                        linked = tasks.operation_tasks(db, operation.id)
                        if len(linked) != 1:
                            raise AssertionError("concurrent replay did not read the committed task")
                        task_id = linked[0].id
                    results.append((created, operation.id, task_id))
                    db.commit()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=admit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(created for created, _, _ in results), 1)
        self.assertEqual(len({operation_id for _, operation_id, _ in results}), 1)
        self.assertEqual(len({task_id for _, _, task_id in results}), 1)

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

        with self.assertRaises(DBAPIError) as update_error:
            with session_scope() as db:
                db.execute(
                    update(Event).where(Event.id == event_id).values(message="tampered")
                )
        self.assertEqual(update_error.exception.orig.sqlstate, "55000")

        with self.assertRaises(DBAPIError) as delete_error:
            with session_scope() as db:
                db.execute(delete(Event).where(Event.id == event_id))
        self.assertEqual(delete_error.exception.orig.sqlstate, "55000")

        with self.assertRaises(DBAPIError) as truncate_error:
            with session_scope() as db:
                db.execute(text("TRUNCATE TABLE public.event"))
        self.assertEqual(truncate_error.exception.orig.sqlstate, "55000")

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

    def test_document_intake_recovers_matching_file_after_database_rollback(self) -> None:
        content = b"%PDF-1.7 synthetic rollback recovery document"
        content_hash = hashlib.sha256(content).hexdigest()

        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            storage.chmod(0o700)
            with patch.object(tasks.settings, "upload_dir", storage):
                with SessionLocal() as db:
                    stranded = tasks.store_document(
                        db, "synthetic-rollback.pdf", content, IntakeSource.upload
                    )
                    stranded_path = Path(stranded.stored_path)
                    db.rollback()

                self.assertTrue(stranded_path.is_file())
                with session_scope() as db:
                    self.assertEqual(
                        db.scalar(
                            select(func.count(Document.id)).where(
                                Document.content_hash == content_hash
                            )
                        ),
                        0,
                    )

                with SessionLocal() as db:
                    recovered = tasks.store_document(
                        db, "synthetic-rollback.pdf", content, IntakeSource.upload
                    )
                    recovered_path = Path(recovered.stored_path)
                    db.commit()

                self.assertEqual(recovered_path, stranded_path)
                self.assertEqual(stranded_path.read_bytes(), content)
                with session_scope() as db:
                    saved = db.scalar(
                        select(Document).where(Document.content_hash == content_hash)
                    )
                    self.assertIsNotNone(saved)
                    self.assertEqual(Path(saved.stored_path), stranded_path)

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
