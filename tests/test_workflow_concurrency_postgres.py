from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from alembic.runtime.migration import MigrationContext
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.db.base import SessionLocal, engine, session_scope
from app.db.base import get_session
from app.db.schema import runtime_guard_differences
from app import cli
from app.cli import _runtime_trigger_security_differences
from app.db.models import (
    Document,
    Event,
    IntakeItem,
    IntakeOperation,
    IntakeSource,
    JobOutbox,
    LoginSession,
    Task,
    TaskStatusHistory,
    TaskStatus,
    User,
)
from app.api import routes_tasks
from app.api.deps import current_user
from app.api.routes_tasks import task_reject
from app.main import app
from app.services import auth, outbox, runner, tasks
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
            self.assertEqual(MigrationContext.configure(connection).get_current_revision(), "1fc64da92b1e")
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

    def create_task_with_status(self, status: TaskStatus) -> int:
        with session_scope() as db:
            task = Task(
                document_id=self.document_id,
                scenario="person",
                profile="default",
                status=status,
                recognized={},
                values={},
                case_number="",
                assignee_id=self.user_id,
            )
            db.add(task)
            db.flush()
            return task.id

    def test_authenticated_second_employee_can_review_shared_task_with_actor_audit(self) -> None:
        # The accepted office policy gives authenticated staff shared case
        # access. The task is assigned to the uploader, but a second employee
        # must still be able to review it and remain attributable in the audit.
        with session_scope() as db:
            task = Task(
                document_id=self.document_id,
                scenario="person_card",
                profile="default",
                status=TaskStatus.needs_review,
                recognized={},
                values={},
                case_number="А00-2/2026",
                assignee_id=self.user_id,
            )
            db.add(task)
            db.flush()
            shared_task_id = task.id

        password = "synthetic-shared-access-password"
        reviewer_login = f"synthetic-reviewer-{os.urandom(8).hex()}"
        with session_scope() as db:
            reviewer = auth.create_user(
                db,
                reviewer_login,
                password,
                full_name="Synthetic Shared Reviewer",
            )
            reviewer_id = reviewer.id

        with TestClient(app) as client:
            login = client.post(
                "/login",
                data={
                    "login": reviewer_login,
                    "password": password,
                },
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )
            if login.status_code != 303:
                self.fail("synthetic reviewer login did not establish a session")
            page = client.get(f"/tasks/{shared_task_id}")
            self.assertEqual(page.status_code, 200)
            saved = client.post(
                f"/tasks/{shared_task_id}/save",
                data={"last_name": "Synthetic Shared Reviewer Edit"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )

        self.assertEqual(saved.status_code, 303)
        with session_scope() as db:
            task = db.get(Task, shared_task_id)
            event = db.scalar(
                select(Event)
                .where(
                    Event.task_id == shared_task_id,
                    Event.kind == "fields_saved",
                    Event.user_id == reviewer_id,
                )
                .order_by(Event.id.desc())
            )
        self.assertEqual(task.values["last_name"], "Synthetic Shared Reviewer Edit")
        self.assertIsNotNone(event)
        self.assertEqual(event.user_id, reviewer_id)
        self.assertEqual(event.payload["request_id"], saved.headers["x-request-id"])

    def test_database_status_guard_matches_the_application_transition_graph(self) -> None:
        statuses = list(TaskStatus)
        with engine.connect() as connection:
            outer = connection.begin()
            try:
                for source in statuses:
                    for target in statuses:
                        savepoint = connection.begin_nested()
                        task_id = connection.scalar(
                            insert(Task)
                            .values(
                                document_id=self.document_id,
                                scenario="person",
                                profile="default",
                                status=source,
                                recognized={},
                                values={},
                                case_number="",
                                dry_run=True,
                                screenshots=[],
                                browser_decision="",
                            )
                            .returning(Task.id)
                        )
                        accepted = True
                        try:
                            connection.execute(
                                update(Task)
                                .where(Task.id == task_id)
                                .values(status=target)
                            )
                        except IntegrityError as exc:
                            accepted = False
                            self.assertEqual(getattr(exc.orig, "sqlstate", None), "23514")
                        finally:
                            savepoint.rollback()

                        expected = (
                            source == target
                            or target in tasks.TASK_STATUS_TRANSITIONS[source]
                        )
                        self.assertEqual(
                            accepted,
                            expected,
                            f"database graph mismatch for {source.value} -> {target.value}",
                        )
            finally:
                outer.rollback()

    def test_rejected_operator_transition_commits_actor_audit_without_task_change(self) -> None:
        self.task_id = self.create_task_with_status(TaskStatus.writing)

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

    def test_failed_login_audit_is_durable_and_contains_no_attempted_credentials(self) -> None:
        def disposable_session():
            with SessionLocal() as db:
                yield db

        prior_override = app.dependency_overrides.get(get_session)
        app.dependency_overrides[get_session] = disposable_session
        try:
            with patch("app.api.routes_auth.auth.authenticate", return_value=None):
                with TestClient(app) as client:
                    response = client.post(
                        "/login",
                        data={
                            "login": "synthetic-failed-login-identity",
                            "password": "synthetic-failed-login-password",
                        },
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
        finally:
            if prior_override is None:
                app.dependency_overrides.pop(get_session, None)
            else:
                app.dependency_overrides[get_session] = prior_override

        self.assertEqual(response.status_code, 401)
        request_id = response.headers["x-request-id"]
        with session_scope() as db:
            event = db.scalar(
                select(Event).where(
                    Event.kind == "session_login_failed",
                    Event.payload["request_id"].astext == request_id,
                )
            )
        self.assertIsNotNone(event)
        self.assertIsNone(event.task_id)
        self.assertIsNone(event.user_id)
        self.assertEqual(event.payload, {"request_id": request_id})
        self.assertNotIn("synthetic-failed-login-identity", event.message)
        self.assertNotIn("synthetic-failed-login-password", event.message)

    def test_upload_storage_error_rolls_back_reserved_intake_operation(self) -> None:
        idempotency_key = os.urandom(24).hex()
        with session_scope() as db:
            tasks_before = db.scalar(
                select(func.count()).select_from(Task).where(Task.assignee_id == self.user_id)
            )
            outbox_before = db.scalar(select(func.count()).select_from(JobOutbox))

        def disposable_session():
            with SessionLocal() as db:
                yield db

        prior_session_override = app.dependency_overrides.get(get_session)
        prior_user_override = app.dependency_overrides.get(current_user)
        app.dependency_overrides[get_session] = disposable_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=self.user_id)
        try:
            with patch.object(
                routes_tasks.tasks,
                "store_document",
                side_effect=OSError("synthetic private storage detail"),
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/upload",
                        files={
                            "files": (
                                "synthetic-storage-failure.pdf",
                                b"synthetic document",
                                "application/pdf",
                            )
                        },
                        data={"idempotency_key": idempotency_key},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
        finally:
            if prior_session_override is None:
                app.dependency_overrides.pop(get_session, None)
            else:
                app.dependency_overrides[get_session] = prior_session_override
            if prior_user_override is None:
                app.dependency_overrides.pop(current_user, None)
            else:
                app.dependency_overrides[current_user] = prior_user_override

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("synthetic private storage detail", response.text)
        with session_scope() as db:
            operation_count = db.scalar(
                select(func.count()).select_from(IntakeOperation).where(
                    IntakeOperation.user_id == self.user_id,
                    IntakeOperation.idempotency_key == idempotency_key,
                )
            )
            tasks_after = db.scalar(
                select(func.count()).select_from(Task).where(Task.assignee_id == self.user_id)
            )
            outbox_after = db.scalar(select(func.count()).select_from(JobOutbox))
        self.assertEqual(operation_count, 0)
        self.assertEqual(tasks_after, tasks_before)
        self.assertEqual(outbox_after, outbox_before)

    def test_batch_storage_failure_reuses_safe_file_on_same_key_retry(self) -> None:
        idempotency_key = os.urandom(24).hex()
        files = [
            ("first-synthetic.pdf", b"first synthetic batch document"),
            ("second-synthetic.pdf", b"second synthetic batch document"),
        ]

        def disposable_session():
            with SessionLocal() as db:
                yield db

        prior_session_override = app.dependency_overrides.get(get_session)
        prior_user_override = app.dependency_overrides.get(current_user)
        app.dependency_overrides[get_session] = disposable_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=self.user_id)
        with tempfile.TemporaryDirectory(prefix="sro-batch-storage-recovery-") as directory:
            storage_root = Path(directory)
            storage_root.chmod(0o700)
            real_store_document = routes_tasks.tasks.store_document
            calls = 0

            def fail_on_second_file(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic second-file storage failure")
                return real_store_document(*args, **kwargs)

            try:
                with patch.object(routes_tasks.settings, "upload_dir", storage_root):
                    with patch.object(
                        routes_tasks.tasks,
                        "store_document",
                        side_effect=fail_on_second_file,
                    ), patch.object(routes_tasks.outbox, "create_intake_intent", return_value=88001), patch.object(
                        routes_tasks.outbox, "dispatch_required"
                    ):
                        with TestClient(app) as client:
                            failed = client.post(
                                "/upload",
                                files=[
                                    ("files", (name, content, "application/pdf"))
                                    for name, content in files
                                ],
                                data={"idempotency_key": idempotency_key},
                                headers={"Origin": "http://testserver"},
                                follow_redirects=False,
                            )
                    self.assertEqual(failed.status_code, 503)
                    self.assertEqual(len(list(storage_root.iterdir())), 1)
                    with session_scope() as db:
                        failed_operation_count = db.scalar(
                            select(func.count()).select_from(IntakeOperation).where(
                                IntakeOperation.user_id == self.user_id,
                                IntakeOperation.idempotency_key == idempotency_key,
                            )
                        )
                    self.assertEqual(failed_operation_count, 0)

                    with patch.object(
                        routes_tasks.outbox,
                        "create_intake_intent",
                        return_value=88002,
                    ), patch.object(routes_tasks.outbox, "dispatch_required"):
                        with TestClient(app) as client:
                            retried = client.post(
                                "/upload",
                                files=[
                                    ("files", (name, content, "application/pdf"))
                                    for name, content in files
                                ],
                                data={"idempotency_key": idempotency_key},
                                headers={"Origin": "http://testserver"},
                                follow_redirects=False,
                            )
                    self.assertEqual(retried.status_code, 303)
                    self.assertEqual(len(list(storage_root.iterdir())), 2)
                    with session_scope() as db:
                        operation = db.scalar(
                            select(IntakeOperation).where(
                                IntakeOperation.user_id == self.user_id,
                                IntakeOperation.idempotency_key == idempotency_key,
                            )
                        )
                        items = list(
                            db.scalars(
                                select(IntakeItem)
                                .where(IntakeItem.operation_id == operation.id)
                                .order_by(IntakeItem.item_index)
                            )
                        )
                    self.assertEqual(
                        [(item.item_index, item.filename) for item in items],
                        [(0, files[0][0]), (1, files[1][0])],
                    )
            finally:
                if prior_session_override is None:
                    app.dependency_overrides.pop(get_session, None)
                else:
                    app.dependency_overrides[get_session] = prior_session_override
                if prior_user_override is None:
                    app.dependency_overrides.pop(current_user, None)
                else:
                    app.dependency_overrides[current_user] = prior_user_override

    def test_outbox_intent_is_transactional_and_intake_replay_deduplicates_pending_delivery(self) -> None:
        with session_scope() as db:
            baseline_pending = outbox.pending_intent_count(db)
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
            self.assertEqual(outbox.pending_intent_count(db), baseline_pending)
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
            self.assertEqual(outbox.pending_intent_count(db), baseline_pending + 1)

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

    def test_kad_intent_is_withheld_without_attempt_or_worker_enqueue(self) -> None:
        with session_scope() as db:
            before = outbox.status_summary(db)
            intent_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7010,
                job_kind="kad",
            )
            db.execute(
                update(JobOutbox)
                .where(JobOutbox.id == intent_id)
                .values(
                    available_at=func.clock_timestamp() + text("interval '30 seconds'"),
                    lease_token="synthetic-legacy-lease",
                    lease_until=func.clock_timestamp() + text("interval '30 seconds'"),
                )
            )

        with patch.object(outbox, "_enqueue") as enqueue:
            with SessionLocal() as db:
                self.assertFalse(outbox.dispatch_intent(db, intent_id))
                _confirmed, pending = outbox.dispatch_ready(db, limit=100)
                self.assertGreaterEqual(pending, 1)

        self.assertNotIn("kad", [call.args[0].job_kind for call in enqueue.call_args_list])
        with session_scope() as db:
            intent = db.get(JobOutbox, intent_id)
            after = outbox.status_summary(db)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.job_kind, "kad")
        self.assertEqual(intent.attempts, 0)
        self.assertEqual(intent.lease_token, "synthetic-legacy-lease")
        self.assertEqual(after.pending, before.pending + 1)
        self.assertGreaterEqual(after.withheld, 1)

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

        with SessionLocal() as rollback_session:
            rollback_task = rollback_session.get(Task, queued_task_id)
            self.assertTrue(tasks.claim_unleased_task(rollback_session, rollback_task))
            rollback_session.rollback()

        with session_scope() as db:
            rolled_back = db.get(Task, queued_task_id)
            self.assertEqual(rolled_back.status, TaskStatus.new)
            self.assertEqual(
                db.scalar(
                    select(func.count(Event.id)).where(
                        Event.task_id == queued_task_id,
                        Event.kind == "task_claimed",
                    )
                ),
                0,
            )

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
            claim_events = list(
                db.scalars(
                    select(Event).where(
                        Event.task_id == queued_task_id,
                        Event.kind == "task_claimed",
                    )
                )
            )
            self.assertEqual(len(claim_events), 1)
            self.assertIsNone(claim_events[0].user_id)
            self.assertEqual(
                claim_events[0].payload,
                {"claim_mode": "legacy_unleased"},
            )

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
        self.task_id = self.create_task_with_status(TaskStatus.recognizing)
        writer = SessionLocal()
        concurrent = SessionLocal()
        try:
            stale_task = writer.get(Task, self.task_id)
            self.assertIsNotNone(stale_task)
            stale_task.status = TaskStatus.recognizing
            writer.commit()

            terminal_task = concurrent.get(Task, self.task_id)
            self.assertIsNotNone(terminal_task)
            terminal_task.status = TaskStatus.failed
            terminal_task.error = ""
            concurrent.commit()

            self.assertFalse(tasks.fail(writer, stale_task, "synthetic stale parser failure"))
            writer.commit()
        finally:
            writer.close()
            concurrent.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.failed)
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
        self.task_id = self.create_task_with_status(TaskStatus.recognizing)
        setup = SessionLocal()
        stale_worker = SessionLocal()
        concurrent = SessionLocal()
        try:
            stale_task = stale_worker.get(Task, self.task_id)
            self.assertEqual(stale_task.status, TaskStatus.recognizing)

            terminal_task = concurrent.get(Task, self.task_id)
            terminal_task.status = TaskStatus.needs_review
            concurrent.commit()

            self.assertFalse(tasks.save_recognized(stale_worker, stale_task, DebtorCard()))
            stale_worker.commit()
        finally:
            setup.close()
            stale_worker.close()
            concurrent.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
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
                source_metadata_sha256=tasks.intake_source_metadata_manifest(["source.pdf"]),
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
                source_metadata_sha256=tasks.intake_source_metadata_manifest(["source.pdf"]),
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
                    source_metadata_sha256=tasks.intake_source_metadata_manifest(["source.pdf"]),
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

    def test_same_key_binds_new_filename_metadata_and_keeps_legacy_retry_compatibility(self) -> None:
        manifest = tasks.intake_manifest(
            "upload", [hashlib.sha256(b"synthetic filename-bound content").hexdigest()]
        )
        first_metadata = tasks.intake_source_metadata_manifest(["original.pdf"])
        changed_metadata = tasks.intake_source_metadata_manifest(["renamed.pdf"])
        key = os.urandom(24).hex()

        with session_scope() as db:
            operation, created = tasks.begin_intake_operation(
                db,
                user_id=self.user_id,
                idempotency_key=key,
                manifest_sha256=manifest,
                source_metadata_sha256=first_metadata,
            )
            self.assertTrue(created)
            self.assertEqual(operation.source_metadata_sha256, first_metadata)

        with session_scope() as db:
            with self.assertRaises(IntegrityError):
                db.execute(
                    text(
                        "UPDATE intake_operation "
                        "SET source_metadata_sha256 = 'not-a-sha256' WHERE id = :id"
                    ),
                    {"id": operation.id},
                )
            db.rollback()

        with session_scope() as db:
            with self.assertRaises(tasks.IntakeOperationConflict):
                tasks.begin_intake_operation(
                    db,
                    user_id=self.user_id,
                    idempotency_key=key,
                    manifest_sha256=manifest,
                    source_metadata_sha256=changed_metadata,
                )

        legacy_key = os.urandom(24).hex()
        with session_scope() as db:
            legacy_operation, created = tasks.begin_intake_operation(
                db,
                user_id=self.user_id,
                idempotency_key=legacy_key,
                manifest_sha256=manifest,
                source_metadata_sha256=None,
            )
            self.assertTrue(created)
            self.assertIsNone(legacy_operation.source_metadata_sha256)

        with session_scope() as db:
            replay, created = tasks.begin_intake_operation(
                db,
                user_id=self.user_id,
                idempotency_key=legacy_key,
                manifest_sha256=manifest,
                source_metadata_sha256=changed_metadata,
            )
            self.assertFalse(created)
            self.assertEqual(replay.id, legacy_operation.id)

    def test_distinct_operations_preserve_each_occurrence_of_shared_document_bytes(self) -> None:
        content_hash = hashlib.sha256(b"synthetic same bytes, independent intake").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            storage.chmod(0o700)
            with patch.object(tasks.settings, "upload_dir", storage):
                with session_scope() as db:
                    first_operation, first_created = tasks.begin_intake_operation(
                        db,
                        user_id=self.user_id,
                        idempotency_key=os.urandom(24).hex(),
                        manifest_sha256=tasks.intake_manifest(
                            "upload", [content_hash]
                        ),
                        source_metadata_sha256=tasks.intake_source_metadata_manifest(
                            ["first-source-name.pdf"]
                        ),
                    )
                    second_operation, second_created = tasks.begin_intake_operation(
                        db,
                        user_id=self.user_id,
                        idempotency_key=os.urandom(24).hex(),
                        manifest_sha256=tasks.intake_manifest(
                            "batch", [content_hash]
                        ),
                        source_metadata_sha256=tasks.intake_source_metadata_manifest(
                            ["second-source-name.pdf"]
                        ),
                    )
                    self.assertTrue(first_created and second_created)
                    first_document = tasks.store_document(
                        db,
                        "first-source-name.pdf",
                        b"synthetic same bytes, independent intake",
                        IntakeSource.upload,
                        self.user_id,
                    )
                    second_document = tasks.store_document(
                        db,
                        "second-source-name.pdf",
                        b"synthetic same bytes, independent intake",
                        IntakeSource.batch,
                        self.user_id,
                    )
                    self.assertEqual(first_document.id, second_document.id)
                    first_item = tasks.create_intake_item(
                        db,
                        operation_id=first_operation.id,
                        item_index=0,
                        document=first_document,
                        filename="first-source-name.pdf",
                        source=IntakeSource.upload,
                        received_by_id=self.user_id,
                    )
                    second_item = tasks.create_intake_item(
                        db,
                        operation_id=second_operation.id,
                        item_index=0,
                        document=second_document,
                        filename="second-source-name.pdf",
                        source=IntakeSource.batch,
                        received_by_id=self.user_id,
                    )
                    first_task = tasks.create_task(
                        db,
                        first_document,
                        user_id=self.user_id,
                        intake_operation_id=first_operation.id,
                        intake_item_id=first_item.id,
                    )
                    second_task = tasks.create_task(
                        db,
                        second_document,
                        user_id=self.user_id,
                        intake_operation_id=second_operation.id,
                        intake_item_id=second_item.id,
                    )
                    first_task_id, second_task_id = first_task.id, second_task.id

        with session_scope() as db:
            first_task = db.get(Task, first_task_id)
            second_task = db.get(Task, second_task_id)
            self.assertNotEqual(first_task.intake_operation_id, second_task.intake_operation_id)
            self.assertNotEqual(first_task.intake_item_id, second_task.intake_item_id)
            self.assertEqual(first_task.document_id, second_task.document_id)
            self.assertEqual(first_task.intake_item.filename, "first-source-name.pdf")
            self.assertEqual(second_task.intake_item.filename, "second-source-name.pdf")
            self.assertEqual(first_task.intake_item.source, IntakeSource.upload)
            self.assertEqual(second_task.intake_item.source, IntakeSource.batch)
            self.assertEqual(first_task.intake_item.received_by_id, self.user_id)
            self.assertEqual(second_task.intake_item.received_by_id, self.user_id)

            mismatched = Task(
                document_id=first_task.document_id,
                scenario="person",
                profile="default",
                status=TaskStatus.new,
                recognized={},
                values={},
                assignee_id=self.user_id,
                intake_operation_id=second_task.intake_operation_id,
                intake_item_id=first_task.intake_item_id,
            )
            db.add(mismatched)
            with self.assertRaises(IntegrityError):
                db.flush()
            db.rollback()

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
                        source_metadata_sha256=tasks.intake_source_metadata_manifest(
                            ["concurrent.pdf"]
                        ),
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
        self.task_id = self.create_task_with_status(TaskStatus.approved)
        setup = SessionLocal()
        stale_operator = SessionLocal()
        concurrent = SessionLocal()
        try:
            stale_task = stale_operator.get(Task, self.task_id)
            terminal_task = concurrent.get(Task, self.task_id)
            terminal_task.status = TaskStatus.failed
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
            self.assertEqual(task.status, TaskStatus.failed)
            rejected_events = db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id,
                    Event.kind == "rejected",
                )
            )
            self.assertEqual(rejected_events, 0)

    def test_rejecting_queued_task_fences_worker_claim(self) -> None:
        self.task_id = self.create_task_with_status(TaskStatus.new)

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

    def test_repeated_preview_decision_is_ignored_after_first_decision(self) -> None:
        self.task_id = self.create_task_with_status(TaskStatus.awaiting_preview)
        stale_operator = SessionLocal()
        concurrent = SessionLocal()
        try:
            stale_task = stale_operator.get(Task, self.task_id)
            first_task = concurrent.get(Task, self.task_id)
            self.assertTrue(tasks.decide_preview(concurrent, first_task, "close", self.user_id))
            concurrent.commit()

            self.assertFalse(
                tasks.decide_preview(stale_operator, stale_task, "close", self.user_id, "b" * 32)
            )
            stale_operator.commit()
        finally:
            stale_operator.close()
            concurrent.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.awaiting_preview)
            self.assertEqual(task.browser_decision, "close")
            decision_events = db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id,
                    Event.kind.in_(("decision_close", "decision_save", "live_save_blocked")),
                )
            )
            self.assertEqual(decision_events, 1)

    def test_stale_preview_timeout_cannot_repeat_expiration(self) -> None:
        self.task_id = self.create_task_with_status(TaskStatus.awaiting_preview)
        stale_timeout = SessionLocal()
        concurrent = SessionLocal()
        try:
            stale_task = stale_timeout.get(Task, self.task_id)
            terminal_task = concurrent.get(Task, self.task_id)
            self.assertTrue(tasks.expire_preview(concurrent, terminal_task))
            concurrent.commit()

            self.assertFalse(tasks.expire_preview(stale_timeout, stale_task))
            stale_timeout.commit()
        finally:
            stale_timeout.close()
            concurrent.close()

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.awaiting_preview)
            self.assertEqual(task.error, "Оператор не ответил, черновик закрыт")
            self.assertEqual(task.browser_decision, "expired")
            timeout_events = db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id,
                    Event.kind == "preview_timeout",
                )
            )
            self.assertEqual(timeout_events, 1)

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
                kind="recognized",
                message="original",
                payload={},
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
            self.assertEqual(saved.message, "Документ распознан")
            self.assertEqual(saved.payload, {})
            db.add(Event(
                task_id=self.task_id,
                user_id=self.user_id,
                kind="recognized",
                message="caller text is replaced",
                payload={},
            ))

    def test_audit_contract_trigger_rejects_direct_sql_bypass(self) -> None:
        with session_scope() as db:
            db.execute(
                text(
                    "INSERT INTO public.event (task_id, user_id, kind, message, payload) "
                    "VALUES (:task_id, :user_id, 'failed', :message, '{}'::jsonb)"
                ),
                {
                    "task_id": self.task_id,
                    "user_id": self.user_id,
                    "message": "Synthetic direct-SQL private text",
                },
            )
            safe_message = db.scalar(
                select(Event.message)
                .where(Event.task_id == self.task_id, Event.kind == "failed")
                .order_by(Event.id.desc())
                .limit(1)
            )
            self.assertEqual(safe_message, "Обработка завершилась ошибкой; требуется проверка")

        invalid_statements = (
            "INSERT INTO public.event (kind, message, payload) "
            "VALUES ('unreviewed_kind', 'private', '{}'::jsonb)",
            "INSERT INTO public.event (kind, message, payload) "
            "VALUES ('fields_saved', 'private', '{\"values\":{\"name\":\"Synthetic Person\"}}'::jsonb)",
            "INSERT INTO public.event (kind, message, payload) "
            "VALUES ('ocr_claimed', 'private', jsonb_build_object('attempt', true, 'generation', 1))",
        )
        for statement in invalid_statements:
            with self.subTest(statement=statement):
                try:
                    with session_scope() as db:
                        db.execute(text(statement))
                except DBAPIError as error:
                    self.assertEqual(error.orig.sqlstate, "23514")
                else:
                    self.fail("Direct SQL audit insert bypassed the privacy contract trigger")

    def test_runtime_guard_readback_detects_disabled_trigger(self) -> None:
        with engine.connect() as connection:
            transaction = connection.begin()
            self.assertEqual(runtime_guard_differences(connection), [])
            connection.execute(
                text("ALTER TABLE public.event DISABLE TRIGGER event_contract_validate")
            )
            differences = runtime_guard_differences(connection)
            self.assertIn(
                "critical runtime trigger shape differs: event_contract_validate",
                differences,
            )
            connection.execute(text("ALTER TABLE public.event ENABLE TRIGGER event_contract_validate"))
            connection.execute(
                text(
                    "CREATE OR REPLACE FUNCTION public.sro_validate_event_insert() "
                    "RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog "
                    "AS $$ BEGIN RETURN NEW; END; $$"
                )
            )
            differences = runtime_guard_differences(connection)
            self.assertIn(
                "critical runtime trigger function differs: sro_validate_event_insert",
                differences,
            )
            connection.execute(
                text(
                    "CREATE OR REPLACE FUNCTION public.sro_enforce_task_status_transition() "
                    "RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog "
                    "AS $$ BEGIN RETURN NEW; END; $$"
                )
            )
            differences = runtime_guard_differences(connection)
            self.assertIn(
                "critical runtime trigger function differs: sro_enforce_task_status_transition",
                differences,
            )
            connection.execute(
                text(
                    "ALTER FUNCTION public.sro_reject_event_mutation() SECURITY DEFINER"
                )
            )
            differences = runtime_guard_differences(connection)
            self.assertIn(
                "critical runtime trigger function differs: sro_reject_event_mutation",
                differences,
            )
            self.assertEqual(
                _runtime_trigger_security_differences(connection),
                [
                    "runtime trigger function unexpectedly SECURITY DEFINER: "
                    "sro_reject_event_mutation"
                ],
            )
            connection.execute(
                text(
                    "ALTER TABLE public.intake_operation "
                    "DROP CONSTRAINT ck_intake_operation_source_metadata_sha256"
                )
            )
            differences = runtime_guard_differences(connection)
            self.assertIn(
                "critical runtime constraint missing or invalid: "
                "ck_intake_operation_source_metadata_sha256",
                differences,
            )
            connection.execute(
                text(
                    "ALTER TABLE public.intake_operation "
                    "ADD CONSTRAINT ck_intake_operation_source_metadata_sha256 "
                    "CHECK (source_metadata_sha256 IS NULL OR "
                    "source_metadata_sha256 ~ '^[0-9a-f]{64}$' OR true)"
                )
            )
            differences = runtime_guard_differences(connection)
            self.assertIn(
                "critical runtime constraint missing or invalid: "
                "ck_intake_operation_source_metadata_sha256",
                differences,
            )
            connection.execute(
                text(
                    "ALTER TABLE public.intake_operation "
                    "DROP CONSTRAINT ck_intake_operation_source_metadata_sha256"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE public.intake_operation "
                    "ADD CONSTRAINT ck_intake_operation_source_metadata_sha256 "
                    "CHECK (source_metadata_sha256 IS NULL OR "
                    "source_metadata_sha256 ~ '^[0-9a-f]{64}$') NOT VALID"
                )
            )
            differences = runtime_guard_differences(connection)
            self.assertIn(
                "critical runtime constraint missing or invalid: "
                "ck_intake_operation_source_metadata_sha256",
                differences,
            )
            transaction.rollback()

    def test_direct_user_deactivation_update_revokes_active_sessions(self) -> None:
        token = os.urandom(32).hex()
        with session_scope() as db:
            auth.new_login_session(db, self.user_id, token)
        with session_scope() as db:
            db.execute(
                text('UPDATE public."user" SET is_active = false WHERE id = :user_id'),
                {"user_id": self.user_id},
            )
        with session_scope() as db:
            self.assertFalse(auth.login_session_is_active(db, self.user_id, token))
            rows = db.scalars(
                select(LoginSession).where(LoginSession.user_id == self.user_id)
            )
            by_digest = {row.token_sha256: row for row in rows}
            self.assertIsNotNone(by_digest[auth._session_token_sha256(token)].revoked_at)

    def test_database_rejects_direct_session_insert_for_inactive_account(self) -> None:
        token_digest = hashlib.sha256(os.urandom(32)).hexdigest()
        with session_scope() as db:
            db.execute(
                text('UPDATE public."user" SET is_active = false WHERE id = :user_id'),
                {"user_id": self.user_id},
            )

        with self.assertRaises(DBAPIError) as insert_error:
            with session_scope() as db:
                db.execute(
                    text(
                        "INSERT INTO public.login_session "
                        "(user_id, token_sha256, expires_at) "
                        "VALUES (:user_id, :digest, clock_timestamp() + interval '1 hour')"
                    ),
                    {"user_id": self.user_id, "digest": token_digest},
                )
        self.assertEqual(insert_error.exception.orig.sqlstate, "23514")
        with session_scope() as db:
            self.assertIsNone(
                db.scalar(
                    select(LoginSession.id).where(LoginSession.token_sha256 == token_digest)
                )
            )

    def test_database_session_updates_cannot_rewrite_identity_or_restore_revocation(self) -> None:
        token = f"immutable-session-{os.urandom(32).hex()}"
        digest = auth._session_token_sha256(token)
        with session_scope() as db:
            auth.new_login_session(db, self.user_id, token)

        with self.assertRaises(DBAPIError) as identity_error:
            with session_scope() as db:
                db.execute(
                    text(
                        "UPDATE public.login_session SET expires_at = "
                        "expires_at + interval '1 day' WHERE token_sha256 = :digest"
                    ),
                    {"digest": digest},
                )
        self.assertEqual(identity_error.exception.orig.sqlstate, "23514")

        with session_scope() as db:
            self.assertTrue(auth.revoke_login_session(db, self.user_id, token))

        with self.assertRaises(DBAPIError) as replay_error:
            with session_scope() as db:
                db.execute(
                    text(
                        "UPDATE public.login_session SET revoked_at = NULL "
                        "WHERE token_sha256 = :digest"
                    ),
                    {"digest": digest},
                )
        self.assertEqual(replay_error.exception.orig.sqlstate, "23514")
        with session_scope() as db:
            self.assertFalse(auth.login_session_is_active(db, self.user_id, token))

    def test_runtime_guard_readback_detects_deactivation_trigger_drift(self) -> None:
        with engine.connect() as connection:
            transaction = connection.begin()
            self.assertEqual(runtime_guard_differences(connection), [])
            connection.execute(
                text(
                    "ALTER TABLE public.\"user\" DISABLE TRIGGER "
                    "user_revoke_login_sessions_on_deactivation"
                )
            )
            self.assertIn(
                "critical runtime trigger shape differs: "
                "user_revoke_login_sessions_on_deactivation",
                runtime_guard_differences(connection),
            )
            connection.execute(
                text(
                    "ALTER TABLE public.\"user\" ENABLE TRIGGER "
                    "user_revoke_login_sessions_on_deactivation"
                )
            )
            connection.execute(
                text(
                    "CREATE OR REPLACE FUNCTION "
                    "public.sro_revoke_sessions_on_user_deactivation() "
                    "RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog "
                    "AS $$ BEGIN RETURN NEW; END; $$"
                )
            )
            self.assertIn(
                "critical runtime trigger function differs: "
                "sro_revoke_sessions_on_user_deactivation",
                runtime_guard_differences(connection),
            )
            transaction.rollback()

    def test_task_status_history_is_atomic_and_append_only(self) -> None:
        with session_scope() as db:
            db.execute(
                update(Task)
                .where(Task.id == self.task_id)
                .values(status=TaskStatus.approved)
            )
            entry = db.scalar(
                select(TaskStatusHistory).where(TaskStatusHistory.task_id == self.task_id)
            )
            self.assertEqual(entry.from_status, "needs_review")
            self.assertEqual(entry.to_status, "approved")
            self.assertIsNotNone(entry.changed_at)
            history_id = entry.id

        with self.assertRaises(DBAPIError) as update_error:
            with session_scope() as db:
                db.execute(
                    update(TaskStatusHistory)
                    .where(TaskStatusHistory.id == history_id)
                    .values(to_status="done")
                )
        self.assertEqual(update_error.exception.orig.sqlstate, "55000")

        with self.assertRaises(DBAPIError) as delete_error:
            with session_scope() as db:
                db.execute(
                    delete(TaskStatusHistory).where(TaskStatusHistory.id == history_id)
                )
        self.assertEqual(delete_error.exception.orig.sqlstate, "55000")

        with self.assertRaises(DBAPIError) as truncate_error:
            with session_scope() as db:
                db.execute(text("TRUNCATE TABLE public.task_status_history"))
        self.assertEqual(truncate_error.exception.orig.sqlstate, "55000")

        with session_scope() as db:
            saved = db.get(TaskStatusHistory, history_id)
            self.assertEqual(saved.from_status, "needs_review")
            self.assertEqual(saved.to_status, "approved")

    def test_task_status_history_rolls_back_with_the_task_update(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "synthetic rollback"):
            with session_scope() as db:
                db.execute(
                    update(Task)
                    .where(Task.id == self.task_id)
                    .values(status=TaskStatus.approved)
                )
                raise RuntimeError("synthetic rollback")

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(
                db.scalar(
                    select(func.count(TaskStatusHistory.id)).where(
                        TaskStatusHistory.task_id == self.task_id
                    )
                ),
                0,
            )

    def test_login_session_revocation_is_per_session_and_repeatable(self) -> None:
        first_token = f"first-{os.urandom(32).hex()}"
        second_token = f"second-{os.urandom(32).hex()}"
        with session_scope() as db:
            first = auth.new_login_session(db, self.user_id, first_token)
            second = auth.new_login_session(db, self.user_id, second_token)
            self.assertNotEqual(first.token_sha256, first_token)
            self.assertNotEqual(second.token_sha256, second_token)

        with session_scope() as db:
            self.assertTrue(auth.login_session_is_active(db, self.user_id, first_token))
            self.assertTrue(auth.login_session_is_active(db, self.user_id, second_token))
            self.assertTrue(auth.revoke_login_session(db, self.user_id, first_token))

        with session_scope() as db:
            self.assertFalse(auth.login_session_is_active(db, self.user_id, first_token))
            self.assertTrue(auth.login_session_is_active(db, self.user_id, second_token))
            self.assertFalse(auth.revoke_login_session(db, self.user_id, first_token))

    def test_global_login_session_revocation_prevents_old_key_cookie_reactivation(self) -> None:
        # Earlier HTTP tests may leave a different synthetic browser session in
        # this shared disposable schema; isolate the global operation's inputs.
        with session_scope() as db:
            auth.revoke_all_login_sessions(db)

        active_token = f"pre-rotation-{os.urandom(32).hex()}"
        expired_token = f"expired-pre-rotation-{os.urandom(32).hex()}"
        already_revoked_token = f"revoked-pre-rotation-{os.urandom(32).hex()}"
        with session_scope() as db:
            db.execute(
                text(
                    "INSERT INTO public.login_session "
                    "(user_id, token_sha256, expires_at) VALUES "
                    "(:user_id, :expired_digest, clock_timestamp() - interval '1 second')"
                ),
                {
                    "user_id": self.user_id,
                    "expired_digest": auth._session_token_sha256(expired_token),
                },
            )
            db.execute(
                text(
                    "INSERT INTO public.login_session "
                    "(user_id, token_sha256, expires_at, revoked_at) VALUES "
                    "(:user_id, :revoked_digest, clock_timestamp() + interval '1 hour', "
                    "clock_timestamp())"
                ),
                {
                    "user_id": self.user_id,
                    "revoked_digest": auth._session_token_sha256(already_revoked_token),
                },
            )
            auth.new_login_session(db, self.user_id, active_token)

        with session_scope() as db:
            self.assertEqual(auth.revoke_all_login_sessions(db), 1)

        with session_scope() as db:
            self.assertFalse(auth.login_session_is_active(db, self.user_id, active_token))
            self.assertFalse(auth.login_session_is_active(db, self.user_id, expired_token))
            self.assertFalse(auth.login_session_is_active(db, self.user_id, already_revoked_token))
            self.assertEqual(auth.revoke_all_login_sessions(db), 0)

    def test_user_creation_normalizes_login_before_case_insensitive_unique_check(self) -> None:
        login = f"synthetic-account-{os.urandom(8).hex()}"
        with session_scope() as db:
            auth.create_user(db, f"  {login.upper()}  ", "synthetic-password-one")

        with self.assertRaises(IntegrityError):
            with session_scope() as db:
                auth.create_user(db, login.lower(), "synthetic-password-two")

    def test_deactivation_revokes_sessions_and_prevents_old_cookie_reactivation(self) -> None:
        token = f"deactivate-session-{os.urandom(32).hex()}"
        with session_scope() as db:
            login = db.get(User, self.user_id).login
        with session_scope() as db:
            auth.new_login_session(db, self.user_id, token)

        with session_scope() as db:
            user, revoked = auth.deactivate_user(db, f"  {login.upper()}  ")
            self.assertIsNotNone(user)
            self.assertFalse(user.is_active)
            self.assertEqual(revoked, 1)

        with session_scope() as db:
            self.assertFalse(auth.login_session_is_active(db, self.user_id, token))
            with self.assertRaises(auth.InactiveUser):
                auth.new_login_session(
                    db, self.user_id, f"blocked-session-{os.urandom(32).hex()}"
                )

        # Even an authorized reactivation cannot restore the prior bearer token.
        with session_scope() as db:
            user = db.get(User, self.user_id)
            user.is_active = True

        with session_scope() as db:
            self.assertTrue(db.get(User, self.user_id).is_active)
            self.assertFalse(auth.login_session_is_active(db, self.user_id, token))

    def test_concurrent_login_session_issue_serializes_with_deactivation(self) -> None:
        token = f"deactivation-race-{os.urandom(32).hex()}"
        with session_scope() as db:
            login = db.get(User, self.user_id).login

        login_transaction = SessionLocal()
        auth.new_login_session(login_transaction, self.user_id, token)
        deactivation_ready = threading.Event()
        deactivation_finished = threading.Event()
        backend_pid: list[int] = []
        deactivation_result: list[int] = []
        deactivation_errors: list[BaseException] = []

        def deactivate() -> None:
            try:
                with session_scope() as db:
                    backend_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
                    deactivation_ready.set()
                    _user, revoked = auth.deactivate_user(db, login)
                    deactivation_result.append(revoked)
                deactivation_finished.set()
            except BaseException as exc:
                deactivation_errors.append(exc)
                deactivation_finished.set()

        thread = threading.Thread(target=deactivate)
        thread.start()
        try:
            self.assertTrue(deactivation_ready.wait(timeout=5))
            wait_deadline = time.monotonic() + 5
            observed_wait = None
            while time.monotonic() < wait_deadline:
                with engine.connect() as observer:
                    observed_wait = observer.execute(
                        text(
                            "SELECT wait_event_type FROM pg_stat_activity "
                            "WHERE pid = :pid"
                        ),
                        {"pid": backend_pid[0]},
                    ).scalar_one_or_none()
                if observed_wait == "Lock":
                    break
                if deactivation_finished.is_set():
                    break
                time.sleep(0.01)
            self.assertEqual(observed_wait, "Lock")
            login_transaction.commit()
            self.assertTrue(deactivation_finished.wait(timeout=5))
        finally:
            if login_transaction.in_transaction():
                login_transaction.rollback()
            login_transaction.close()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(deactivation_errors, [])
        self.assertEqual(deactivation_result, [1])
        with session_scope() as db:
            self.assertFalse(auth.login_session_is_active(db, self.user_id, token))
            self.assertFalse(db.get(User, self.user_id).is_active)

    def test_concurrent_login_session_issue_serializes_with_direct_sql_deactivation(self) -> None:
        token = f"direct-deactivation-race-{os.urandom(32).hex()}"
        login_transaction = SessionLocal()
        auth.new_login_session(login_transaction, self.user_id, token)
        deactivation_ready = threading.Event()
        deactivation_finished = threading.Event()
        backend_pid: list[int] = []
        deactivation_errors: list[BaseException] = []

        def deactivate_with_sql() -> None:
            try:
                with session_scope() as db:
                    backend_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
                    deactivation_ready.set()
                    db.execute(
                        text(
                            'UPDATE public."user" SET is_active = false WHERE id = :user_id'
                        ),
                        {"user_id": self.user_id},
                    )
                deactivation_finished.set()
            except BaseException as exc:
                deactivation_errors.append(exc)
                deactivation_finished.set()

        thread = threading.Thread(target=deactivate_with_sql)
        thread.start()
        try:
            self.assertTrue(deactivation_ready.wait(timeout=5))
            wait_deadline = time.monotonic() + 5
            observed_wait = None
            while time.monotonic() < wait_deadline:
                with engine.connect() as observer:
                    observed_wait = observer.execute(
                        text(
                            "SELECT wait_event_type FROM pg_stat_activity "
                            "WHERE pid = :pid"
                        ),
                        {"pid": backend_pid[0]},
                    ).scalar_one_or_none()
                if observed_wait == "Lock":
                    break
                if deactivation_finished.is_set():
                    break
                time.sleep(0.01)
            self.assertEqual(observed_wait, "Lock")
            login_transaction.commit()
            self.assertTrue(deactivation_finished.wait(timeout=5))
        finally:
            if login_transaction.in_transaction():
                login_transaction.rollback()
            login_transaction.close()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(deactivation_errors, [])
        with session_scope() as db:
            self.assertFalse(auth.login_session_is_active(db, self.user_id, token))
            self.assertFalse(db.get(User, self.user_id).is_active)

    def test_direct_session_insert_waits_for_deactivation_and_then_fails(self) -> None:
        token_digest = hashlib.sha256(os.urandom(32)).hexdigest()
        deactivation = SessionLocal()
        deactivation.execute(
            text('UPDATE public."user" SET is_active = false WHERE id = :user_id'),
            {"user_id": self.user_id},
        )
        insert_ready = threading.Event()
        insert_finished = threading.Event()
        backend_pid: list[int] = []
        insert_errors: list[BaseException] = []

        def insert_directly() -> None:
            try:
                with session_scope() as db:
                    backend_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
                    insert_ready.set()
                    db.execute(
                        text(
                            "INSERT INTO public.login_session "
                            "(user_id, token_sha256, expires_at) "
                            "VALUES (:user_id, :digest, clock_timestamp() + interval '1 hour')"
                        ),
                        {"user_id": self.user_id, "digest": token_digest},
                    )
            except BaseException as exc:
                insert_errors.append(exc)
            finally:
                insert_finished.set()

        thread = threading.Thread(target=insert_directly)
        thread.start()
        try:
            self.assertTrue(insert_ready.wait(timeout=5))
            wait_deadline = time.monotonic() + 5
            observed_wait = None
            while time.monotonic() < wait_deadline:
                with engine.connect() as observer:
                    observed_wait = observer.execute(
                        text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": backend_pid[0]},
                    ).scalar_one_or_none()
                if observed_wait == "Lock" or insert_finished.is_set():
                    break
                time.sleep(0.01)
            self.assertEqual(observed_wait, "Lock")
            deactivation.commit()
            self.assertTrue(insert_finished.wait(timeout=5))
        finally:
            if deactivation.in_transaction():
                deactivation.rollback()
            deactivation.close()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(insert_errors), 1)
        self.assertEqual(insert_errors[0].orig.sqlstate, "23514")
        with session_scope() as db:
            self.assertIsNone(
                db.scalar(
                    select(LoginSession.id).where(LoginSession.token_sha256 == token_digest)
                )
            )

    def test_expired_login_session_cannot_authenticate_or_be_revoked_as_active(self) -> None:
        token = f"expired-session-{os.urandom(32).hex()}"
        with session_scope() as db:
            db.execute(
                text(
                    "INSERT INTO public.login_session "
                    "(user_id, token_sha256, expires_at) VALUES "
                    "(:user_id, :digest, clock_timestamp() - interval '1 second')"
                ),
                {
                    "user_id": self.user_id,
                    "digest": auth._session_token_sha256(token),
                },
            )

        with session_scope() as db:
            self.assertFalse(auth.login_session_is_active(db, self.user_id, token))
            self.assertFalse(auth.revoke_login_session(db, self.user_id, token))

    def test_concurrent_login_session_revocation_has_one_winner(self) -> None:
        token = f"logout-race-{os.urandom(32).hex()}"
        with session_scope() as db:
            auth.new_login_session(db, self.user_id, token)

        barrier = threading.Barrier(2)

        def revoke() -> bool:
            barrier.wait(timeout=5)
            with session_scope() as db:
                return auth.revoke_login_session(db, self.user_id, token)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: revoke(), range(2)))

        self.assertCountEqual(results, [True, False])

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

    def test_duplicate_intake_fails_closed_for_existing_document_outside_private_root(self) -> None:
        content = b"synthetic existing-document path integrity probe"
        content_hash = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            root.mkdir(mode=0o700)
            outside = Path(directory) / "outside.pdf"
            tasks._write_document_file(outside, content)
            with patch.object(tasks.settings, "upload_dir", root):
                with session_scope() as db:
                    document = Document(
                        filename="synthetic-outside.pdf",
                        stored_path=str(outside),
                        content_hash=content_hash,
                        source=IntakeSource.upload,
                        uploaded_by_id=self.user_id,
                    )
                    db.add(document)
                    db.flush()
                    document_id = document.id

                with SessionLocal() as db:
                    with self.assertRaises(tasks.ExistingDocumentUnavailable):
                        tasks.store_document(
                            db,
                            "synthetic-duplicate.pdf",
                            content,
                            IntakeSource.upload,
                            self.user_id,
                        )
                    db.rollback()

                with session_scope() as db:
                    self.assertEqual(
                        db.scalar(select(func.count(Document.id)).where(Document.id == document_id)),
                        1,
                    )
                    self.assertEqual(
                        db.scalar(select(func.count(Task.id)).where(Task.document_id == document_id)),
                        0,
                    )
            self.assertEqual(outside.read_bytes(), content)

    def test_storage_database_audit_uses_postgresql_and_redacts_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            storage.chmod(0o700)
            output = StringIO()
            with (
                patch.object(cli.settings, "upload_dir", storage),
                redirect_stdout(output),
            ):
                result = cli.cmd_storage_check(Namespace(database=True))

        rendered = output.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("Document storage references:", rendered)
        self.assertRegex(rendered, r"database_documents=\d+")
        self.assertIn("paths_outside_storage=", rendered)
        self.assertNotIn("/tmp/synthetic-concurrency.pdf", rendered)


if __name__ == "__main__":
    unittest.main()
