from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from alembic.runtime.migration import MigrationContext
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.db.base import SessionLocal, engine, session_scope
from app.db.models import (
    Document,
    Event,
    IntakeOperation,
    IntakeSource,
    JobOutbox,
    Task,
    TaskStatus,
    User,
)
from app.api.routes_tasks import task_reject
from app.services import outbox, runner, tasks
from app.workers.queues import QueueUnavailable
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
            self.assertEqual(MigrationContext.configure(connection).get_current_revision(), "f28c7a913bd4")
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

    def test_rejected_operator_transition_commits_actor_audit_without_task_change(self) -> None:
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            task.status = TaskStatus.writing

        request_id = "f" * 32
        request = SimpleNamespace(scope={"state": {"request_id": request_id}})
        with session_scope() as db:
            with self.assertRaises(HTTPException) as raised:
                task_reject(request, self.task_id, SimpleNamespace(id=self.user_id), db)
        self.assertEqual(raised.exception.status_code, 409)

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            event = db.scalar(
                select(Event).where(
                    Event.task_id == self.task_id,
                    Event.kind == "operator_action_not_applied",
                )
            )
        self.assertEqual(task.status, TaskStatus.writing)
        self.assertEqual(event.user_id, self.user_id)
        self.assertEqual(
            event.payload,
            {
                "action": "reject",
                "reason": "precondition_not_met",
                "request_id": request_id,
            },
        )

    def test_outbox_intent_is_transactional_and_intake_replay_deduplicates_pending_delivery(self) -> None:
        db = SessionLocal()
        try:
            intent_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7001,
                job_kind="ocr",
            )
            db.flush()
            db.rollback()
        finally:
            db.close()

        with session_scope() as db:
            self.assertEqual(outbox.pending_intent_count(db), 0)
            first_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7001,
                job_kind="ocr",
            )
            replay_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7001,
                job_kind="ocr",
            )
            self.assertNotEqual(intent_id, first_id)
            self.assertEqual(first_id, replay_id)

        with session_scope() as db:
            self.assertEqual(outbox.pending_intent_count(db), 1)

    def test_queue_failure_keeps_outbox_intent_with_bounded_backoff_then_retries(self) -> None:
        with session_scope() as db:
            intent_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7002,
                job_kind="ocr",
            )

        with patch.object(
            outbox,
            "_enqueue",
            side_effect=QueueUnavailable("ConnectionError"),
        ):
            with SessionLocal() as db, self.assertRaises(QueueUnavailable):
                outbox.dispatch_intent(db, intent_id)

        with session_scope() as db:
            intent = db.get(JobOutbox, intent_id)
            self.assertIsNotNone(intent)
            self.assertEqual(intent.attempts, 1)
            self.assertIsNone(intent.lease_token)
            self.assertGreater(intent.available_at, db.scalar(select(func.clock_timestamp())))
            db.execute(
                update(JobOutbox)
                .where(JobOutbox.id == intent_id)
                .values(available_at=func.clock_timestamp())
            )

        with patch.object(outbox, "_enqueue") as enqueue:
            with SessionLocal() as db:
                self.assertTrue(outbox.dispatch_intent(db, intent_id))
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0].job_kind, "ocr")
        with session_scope() as db:
            self.assertIsNone(db.get(JobOutbox, intent_id))

    def test_local_validation_dispatch_is_fenced_to_local_mode_and_actor(self) -> None:
        with session_scope() as db:
            intent_id = outbox.create_local_validation_intent(
                db,
                task_id=self.task_id,
                actor_id=self.user_id,
            )

        from app.workers import queues

        with patch.object(queues, "enqueue_write", return_value="synthetic-rq-id") as enqueue:
            with SessionLocal() as db:
                self.assertTrue(outbox.dispatch_intent(db, intent_id))

        enqueue.assert_called_once_with(
            self.task_id,
            live=False,
            user_id=self.user_id,
        )

    def test_crash_after_redis_acceptance_retries_at_least_once_after_lease_expiry(self) -> None:
        with session_scope() as db:
            intent_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7003,
                job_kind="ocr",
            )

        with patch.object(outbox, "_enqueue", side_effect=RuntimeError("synthetic process exit")) as enqueue:
            with SessionLocal() as db, self.assertRaisesRegex(RuntimeError, "synthetic process exit"):
                outbox.dispatch_intent(db, intent_id)
        enqueue.assert_called_once()

        with session_scope() as db:
            db.execute(
                update(JobOutbox)
                .where(JobOutbox.id == intent_id)
                .values(
                    available_at=func.clock_timestamp(),
                    lease_until=func.clock_timestamp() - text("interval '1 second'"),
                )
            )

        with patch.object(outbox, "_enqueue") as retry_enqueue:
            with SessionLocal() as db:
                self.assertTrue(outbox.dispatch_intent(db, intent_id))
        retry_enqueue.assert_called_once()

    def test_concurrent_outbox_dispatchers_do_not_enqueue_the_same_active_claim(self) -> None:
        with session_scope() as db:
            intent_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7004,
                job_kind="ocr",
            )

        enqueue_entered = threading.Event()
        allow_enqueue_to_finish = threading.Event()
        first_result: list[bool] = []
        first_errors: list[Exception] = []

        def blocked_enqueue(_intent) -> None:
            enqueue_entered.set()
            if not allow_enqueue_to_finish.wait(timeout=5):
                raise TimeoutError("synthetic outbox test did not release enqueue")

        def first_dispatcher() -> None:
            try:
                with SessionLocal() as db:
                    first_result.append(outbox.dispatch_intent(db, intent_id))
            except Exception as exc:
                first_errors.append(exc)

        with patch.object(outbox, "_enqueue", side_effect=blocked_enqueue) as enqueue:
            thread = threading.Thread(target=first_dispatcher, daemon=True)
            thread.start()
            self.assertTrue(enqueue_entered.wait(timeout=5))
            try:
                with SessionLocal() as db:
                    self.assertFalse(outbox.dispatch_intent(db, intent_id))
            finally:
                allow_enqueue_to_finish.set()
                thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(first_result, [True])
        enqueue.assert_called_once()

    def test_outbox_summary_distinguishes_eligible_leased_and_delayed_intents(self) -> None:
        with session_scope() as db:
            before = outbox.status_summary(db)

        with session_scope() as db:
            outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7005,
                job_kind="ocr",
            )
            leased_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7006,
                job_kind="ocr",
            )
            delayed_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7007,
                job_kind="ocr",
            )
            db.execute(
                update(JobOutbox)
                .where(JobOutbox.id == leased_id)
                .values(
                    lease_token="synthetic-outbox-lease",
                    lease_until=func.clock_timestamp() + text("interval '60 seconds'"),
                    attempts=1,
                    last_error_type="ConnectionError",
                )
            )
            db.execute(
                update(JobOutbox)
                .where(JobOutbox.id == delayed_id)
                .values(
                    available_at=func.clock_timestamp() + text("interval '30 seconds'"),
                    attempts=1,
                    last_error_type="TimeoutError",
                )
            )

        with session_scope() as db:
            summary = outbox.status_summary(db)

        self.assertEqual(summary.pending - before.pending, 3)
        self.assertEqual(summary.eligible - before.eligible, 1)
        self.assertEqual(summary.leased - before.leased, 1)
        self.assertEqual(summary.delayed - before.delayed, 1)
        self.assertIsNotNone(summary.oldest_age_seconds)
        self.assertEqual(
            dict(summary.last_error_counts),
            {"ConnectionError": 1, "TimeoutError": 1},
        )

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

    def test_stale_reject_cannot_overwrite_terminal_task_state(self) -> None:
        setup = SessionLocal()
        stale_operator = SessionLocal()
        concurrent = SessionLocal()
        try:
            task = setup.get(Task, self.task_id)
            task.status = TaskStatus.needs_review
            setup.commit()

            stale_task = stale_operator.get(Task, self.task_id)
            terminal_task = concurrent.get(Task, self.task_id)
            terminal_task.status = TaskStatus.done
            concurrent.commit()

            with self.assertRaises(tasks.InvalidTaskTransition):
                tasks.reject(stale_operator, stale_task, self.user_id)
            stale_operator.rollback()
        finally:
            setup.close()
            stale_operator.close()
            concurrent.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.done)
            rejected_events = db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id,
                    Event.kind == "rejected",
                )
            )
            self.assertEqual(rejected_events, 0)

    def test_rejecting_queued_task_fences_worker_claim(self) -> None:
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            task.status = TaskStatus.new

        operator = SessionLocal()
        worker = SessionLocal()
        try:
            operator_task = operator.get(Task, self.task_id)
            worker_task = worker.get(Task, self.task_id)
            tasks.reject(operator, operator_task, self.user_id)
            operator.commit()

            self.assertFalse(tasks.claim_unleased_task(worker, worker_task))
            worker.commit()
        finally:
            operator.close()
            worker.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.rejected)
            rejected_events = db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id,
                    Event.kind == "rejected",
                )
            )
            self.assertEqual(rejected_events, 1)

    def test_rejection_winning_race_blocks_stale_local_validation_admission(self) -> None:
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            task.status = TaskStatus.needs_review
            task.values = {"debtor_name": "Synthetic Debtor"}
            tasks.approve(db, task, self.user_id)

        stale_request = SessionLocal()
        operator = SessionLocal()
        try:
            stale_task = stale_request.get(Task, self.task_id)
            operator_task = operator.get(Task, self.task_id)
            tasks.reject(operator, operator_task, self.user_id)
            operator.commit()

            self.assertFalse(
                tasks.request_local_validation(
                    stale_request,
                    stale_task,
                    self.user_id,
                    "c" * 32,
                )
            )
            stale_request.commit()
        finally:
            stale_request.close()
            operator.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.rejected)
            local_requests = db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id,
                    Event.kind == "local_validation_requested",
                )
            )
            self.assertEqual(local_requests, 0)

    def test_stale_preview_decision_is_ignored_after_terminal_transition(self) -> None:
        setup = SessionLocal()
        stale_operator = SessionLocal()
        concurrent = SessionLocal()
        try:
            task = setup.get(Task, self.task_id)
            task.status = TaskStatus.awaiting_preview
            task.browser_decision = ""
            setup.commit()

            stale_task = stale_operator.get(Task, self.task_id)
            terminal_task = concurrent.get(Task, self.task_id)
            terminal_task.status = TaskStatus.done
            concurrent.commit()

            self.assertFalse(
                tasks.decide_preview(stale_operator, stale_task, "close", self.user_id, "b" * 32)
            )
            stale_operator.commit()
        finally:
            setup.close()
            stale_operator.close()
            concurrent.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.done)
            self.assertEqual(task.browser_decision, "")
            decision_events = db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id,
                    Event.kind.in_(("decision_close", "decision_save", "live_save_blocked")),
                )
            )
            self.assertEqual(decision_events, 0)

    def test_stale_preview_timeout_cannot_annotate_terminal_task(self) -> None:
        setup = SessionLocal()
        stale_timeout = SessionLocal()
        concurrent = SessionLocal()
        try:
            task = setup.get(Task, self.task_id)
            task.status = TaskStatus.awaiting_preview
            task.error = ""
            task.browser_decision = ""
            setup.commit()

            stale_task = stale_timeout.get(Task, self.task_id)
            terminal_task = concurrent.get(Task, self.task_id)
            terminal_task.status = TaskStatus.done
            terminal_task.error = ""
            concurrent.commit()

            self.assertFalse(tasks.expire_preview(stale_timeout, stale_task))
            stale_timeout.commit()
        finally:
            setup.close()
            stale_timeout.close()
            concurrent.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.done)
            self.assertEqual(task.error, "")
            timeout_events = db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id,
                    Event.kind == "preview_timeout",
                )
            )
            self.assertEqual(timeout_events, 0)

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
