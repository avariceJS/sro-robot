from __future__ import annotations

import hashlib
import json
import os
import re
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
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.db.base import SessionLocal, engine, session_scope
from app.db.base import get_session
from app.db.schema import runtime_guard_differences
from app import cli
from app.cli import _runtime_trigger_security_differences
from app.db.models import (
    AssignmentDraft,
    AssignmentDraftRevision,
    DebtorCardDraft,
    DebtorCardDraftRevision,
    Document,
    Event,
    IntakeItem,
    IntakeOperation,
    IntakeSource,
    JobOutbox,
    LoginSession,
    OcrRetryRun,
    Task,
    TaskStatusHistory,
    TaskStatus,
    UploadTransportBatch,
    UploadTransportBatchItem,
    UploadTransportChunkReceipt,
    User,
)
from app.api import routes_tasks, routes_upload_batches
from app.api.deps import current_user
from app.api.routes_tasks import task_reject
from app.main import app
from app.services import assignments, auth, bulk, leases, ocr_retries, outbox, runner, tasks
from app.workers.queues import QueueUnavailable
from app.services.tasks import approval_matches
from app.settings import settings
from tests.postgres_safety import assert_disposable_postgres
from sro_robot.core.models import DebtorCard
from sro_robot.platform.authentication import SroLoginError, SroLoginResult


@unittest.skipUnless(
    os.environ.get("SRO_TEST_POSTGRES") == "1",
    "requires the disposable PostgreSQL migration drill",
)
class WorkflowConcurrencyPostgreSQLTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_disposable_postgres()
        with engine.connect() as connection:
            self.assertEqual(MigrationContext.configure(connection).get_current_revision(), "20261021_reproc")
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

    def create_failed_upload_receipt(
        self, operation_id: int | None = None, document_id: int | None = None,
    ) -> tuple[int, int]:
        content = b"%PDF-1.4\nsynthetic OCR retry source\n%%EOF\n" + os.urandom(12)
        with session_scope() as db:
            document = db.get(Document, document_id) if document_id is not None else None
            if document_id is not None:
                self.assertIsNotNone(document)
            content_hash = document.content_hash if document is not None else hashlib.sha256(content).hexdigest()
            if operation_id is None:
                operation = IntakeOperation(
                    user_id=self.user_id,
                    idempotency_key=os.urandom(24).hex(),
                    manifest_sha256=tasks.intake_manifest("upload", [content_hash]),
                    source_metadata_sha256=tasks.intake_source_metadata_manifest(
                        ["synthetic-retry-source.pdf"]
                    ),
                )
                db.add(operation)
                db.flush()
            else:
                operation = db.get(IntakeOperation, operation_id)
                self.assertIsNotNone(operation)
            max_item_index = db.scalar(
                select(func.max(IntakeItem.item_index)).where(
                    IntakeItem.operation_id == operation.id,
                )
            )
            item_index = 0 if max_item_index is None else int(max_item_index) + 1
            if document is None:
                document = tasks.store_document(
                    db,
                    "synthetic-retry-source.pdf",
                    content,
                    IntakeSource.upload,
                    self.user_id,
                )
            item = tasks.create_intake_item(
                db,
                operation_id=operation.id,
                item_index=item_index,
                document=document,
                filename="synthetic-retry-source.pdf",
                source=IntakeSource.upload,
                received_by_id=self.user_id,
            )
            parent = tasks.create_task(
                db,
                document,
                user_id=self.user_id,
                scenario="person_card",
                intake_operation_id=operation.id,
                intake_item_id=item.id,
            )
            tasks.transition_task_status(parent, TaskStatus.recognizing)
            db.flush()
            self.assertTrue(tasks.fail(db, parent, "synthetic OCR failure"))
            witness_id = db.scalar(
                select(TaskStatusHistory.id)
                .where(TaskStatusHistory.task_id == parent.id)
                .order_by(TaskStatusHistory.id.desc())
                .limit(1)
            )
            return parent.id, witness_id

    @staticmethod
    def issue_test_login_session(db, user_id: int, token: str):
        row = auth.new_login_session(db, user_id, token)
        auth.attach_external_session(
            db,
            row,
            json.dumps(
                {"cookies": [{"domain": "sroarbitr.sro-service.ru"}], "origins": []}
            ).encode(),
            "synthetic-session-encryption-key-material-000000000000",
            auth.SESSION_MAX_AGE_SECONDS,
        )
        return row

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

        external = SroLoginResult(
            json.dumps({"cookies": [{"domain": "sroarbitr.sro-service.ru"}], "origins": []}).encode(),
            reviewer_login,
        )
        with patch("app.api.routes_auth.authenticate_sro_credentials", return_value=external), TestClient(app) as client:
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
            review_token = re.search(r'name="review_token" value="([0-9a-f]{64})"', page.text).group(1)
            saved = client.post(
                f"/tasks/{shared_task_id}/save",
                data={"last_name": "Synthetic Shared Reviewer Edit", "review_token": review_token},
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

    def test_parallel_explicit_ocr_retry_requests_share_one_clean_child(self) -> None:
        parent_id, failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            second_user = User(
                login=f"synthetic-ocr-retry-{os.urandom(8).hex()}",
                full_name="Synthetic OCR Retry Reviewer",
                password_hash="synthetic-only",
                sro_login="synthetic-ocr-retry",
                is_active=True,
            )
            db.add(second_user)
            db.flush()
            second_user_id = second_user.id

        barrier = threading.Barrier(2)

        def request_retry(actor_id: int):
            barrier.wait(timeout=5)
            with session_scope() as db:
                result = ocr_retries.request_retry(
                    db,
                    parent_task_id=parent_id,
                    failure_history_id=failure_id,
                    user_id=actor_id,
                    request_id=os.urandom(16).hex(),
                )
                db.commit()
                run, child, intent_id, created = result
                return run.id, child.id, intent_id, created

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(request_retry, (self.user_id, second_user_id)))
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(sum(bool(result[3]) for result in results), 1)

        with session_scope() as db:
            run = db.scalar(select(OcrRetryRun).where(OcrRetryRun.parent_task_id == parent_id))
            parent = db.get(Task, parent_id)
            child = db.get(Task, run.child_task_id)
            item = db.get(IntakeItem, run.source_intake_item_id)
            pending = int(db.scalar(
                select(func.count()).select_from(JobOutbox).where(JobOutbox.task_id == child.id)
            ) or 0)
            history = list(db.scalars(
                select(TaskStatusHistory)
                .where(TaskStatusHistory.task_id == parent_id)
                .order_by(TaskStatusHistory.id)
            ))
        self.assertEqual(parent.status, TaskStatus.failed)
        self.assertEqual(len(history), 2)
        self.assertEqual(child.document_id, parent.document_id)
        self.assertIsNone(child.intake_item_id)
        self.assertIsNone(child.intake_operation_id)
        self.assertEqual(child.status, TaskStatus.new)
        self.assertEqual(child.recognized, {})
        self.assertEqual(child.values, {})
        self.assertIsNone(child.approved_by_id)
        self.assertEqual(child.record_url, "")
        self.assertEqual(child.screenshots, [])
        self.assertEqual(item.filename, "synthetic-retry-source.pdf")
        self.assertEqual(pending, 1)

        with session_scope() as db:
            with self.assertRaises(ocr_retries.OcrRetryConflict):
                ocr_retries.request_retry(
                    db,
                    parent_task_id=parent_id,
                    failure_history_id=failure_id + 100,
                    user_id=self.user_id,
                )
            db.rollback()

        with session_scope() as db:
            with self.assertRaises(DBAPIError) as raised:
                db.execute(
                    update(OcrRetryRun)
                    .where(OcrRetryRun.parent_task_id == parent_id)
                    .values(requested_by_id=second_user_id)
                )
                db.flush()
            self.assertEqual(getattr(raised.exception.orig, "sqlstate", None), "55000")
            db.rollback()

    def test_intake_ocr_attempt_view_follows_exact_receipt_retry_chain(self) -> None:
        parent_id, failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            parent = db.get(Task, parent_id)
            operation_id = parent.intake_operation_id
            item_id = parent.intake_item_id
            initial = bulk.intake_ocr_attempts(db, operation_id)
            self.assertEqual(len(initial["receipts"]), 1)
            self.assertEqual(initial["receipts"][0]["item"].id, item_id)
            self.assertEqual(initial["receipts"][0]["root"].id, parent_id)
            first_page = bulk.intake_ocr_attempt_chain(
                db, operation_id=operation_id, item_id=item_id
            )
            self.assertEqual([attempt["task"].id for attempt in first_page["attempts"]], [parent_id])
            self.assertTrue(first_page["attempts"][0]["retry_context"].eligible)
            run, child, _intent, created = ocr_retries.request_retry(
                db,
                parent_task_id=parent_id,
                failure_history_id=failure_id,
                user_id=self.user_id,
                request_id="a" * 32,
            )
            self.assertTrue(created)
            child_id = child.id
            run_id = run.id
            db.commit()

        with session_scope() as db:
            child = db.get(Task, child_id)
            tasks.transition_task_status(child, TaskStatus.recognizing)
            self.assertTrue(tasks.fail(db, child, "synthetic second OCR failure"))
            child_failure_id = db.scalar(
                select(TaskStatusHistory.id)
                .where(TaskStatusHistory.task_id == child_id)
                .order_by(TaskStatusHistory.id.desc())
                .limit(1)
            )
            db.commit()
        with session_scope() as db:
            second_run, grandchild, _intent, created = ocr_retries.request_retry(
                db,
                parent_task_id=child_id,
                failure_history_id=child_failure_id,
                user_id=self.user_id,
                request_id="b" * 32,
            )
            self.assertTrue(created)
            grandchild_id = grandchild.id
            db.commit()

        latest_task_id = grandchild_id
        for attempt_index in range(20):
            with session_scope() as db:
                parent = db.get(Task, latest_task_id)
                tasks.transition_task_status(parent, TaskStatus.recognizing)
                self.assertTrue(tasks.fail(db, parent, f"synthetic bounded-page failure {attempt_index}"))
                failure_id = db.scalar(
                    select(TaskStatusHistory.id)
                    .where(TaskStatusHistory.task_id == latest_task_id)
                    .order_by(TaskStatusHistory.id.desc())
                    .limit(1)
                )
                run, child, _intent, created = ocr_retries.request_retry(
                    db,
                    parent_task_id=latest_task_id,
                    failure_history_id=failure_id,
                    user_id=self.user_id,
                    request_id=f"{attempt_index + 3:032x}",
                )
                self.assertTrue(created)
                latest_task_id = child.id
                db.commit()

        with session_scope() as db:
            chain = bulk.intake_ocr_attempts(db, operation_id)
            self.assertEqual(chain["receipts"][0]["root"].id, parent_id)
            first_page = bulk.intake_ocr_attempt_chain(
                db, operation_id=operation_id, item_id=item_id
            )
            attempts = first_page["attempts"]
            self.assertEqual(len(attempts), bulk.OCR_ATTEMPTS_PER_PAGE)
            self.assertEqual(attempts[0]["task"].id, parent_id)
            self.assertEqual(attempts[1]["task"].id, child_id)
            self.assertEqual(
                [attempt["requested_by"].id if attempt["requested_by"] else None for attempt in attempts],
                [None] + [self.user_id] * (bulk.OCR_ATTEMPTS_PER_PAGE - 1),
            )
            self.assertEqual(attempts[0]["outgoing_child_id"], child_id)
            self.assertEqual(attempts[1]["outgoing_child_id"], grandchild_id)
            self.assertEqual(attempts[0]["retry_context"].outgoing_run.id, run_id)
            self.assertEqual(attempts[1]["retry_context"].incoming_run.id, run_id)
            self.assertFalse(attempts[0]["retry_context"].eligible)
            self.assertFalse(attempts[1]["retry_context"].eligible)
            self.assertIsNotNone(first_page["next_after_task_id"])
            second_page = bulk.intake_ocr_attempt_chain(
                db,
                operation_id=operation_id,
                item_id=item_id,
                after_task_id=first_page["next_after_task_id"],
            )
            self.assertEqual(len(second_page["attempts"]), 3)
            self.assertEqual(second_page["attempts"][0]["task"].id, attempts[-1]["outgoing_child_id"])
            self.assertEqual(second_page["attempts"][-1]["task"].id, latest_task_id)
            self.assertEqual(
                second_page["attempts"][0]["retry_context"].incoming_run.id,
                attempts[-1]["retry_context"].outgoing_run.id,
            )
            self.assertIsNone(second_page["next_after_task_id"])

    def test_retry_review_queue_and_continuation_stay_inside_exact_operation(self) -> None:
        first_parent_id, first_failure_id = self.create_failed_upload_receipt()
        with SessionLocal() as db:
            operation_id = db.get(Task, first_parent_id).intake_operation_id
            shared_document_id = db.get(Task, first_parent_id).document_id
        second_parent_id, second_failure_id = self.create_failed_upload_receipt(operation_id)
        outside_parent_id, outside_failure_id = self.create_failed_upload_receipt(
            document_id=shared_document_id,
        )

        retry_children = []
        for parent_id, failure_id in (
            (first_parent_id, first_failure_id),
            (second_parent_id, second_failure_id),
            (outside_parent_id, outside_failure_id),
        ):
            with session_scope() as db:
                _run, child, _intent, created = ocr_retries.request_retry(
                    db,
                    parent_task_id=parent_id,
                    failure_history_id=failure_id,
                    user_id=self.user_id,
                    request_id=os.urandom(16).hex(),
                )
                self.assertTrue(created)
                tasks.transition_task_status(child, TaskStatus.recognizing)
                db.flush()
                tasks.transition_task_status(child, TaskStatus.needs_review)
                retry_children.append(child.id)
                db.commit()

        with SessionLocal() as db:
            outside_operation_id = db.get(Task, outside_parent_id).intake_operation_id
            queue = bulk.intake_ocr_retry_review_queue(db, operation_id)
            self.assertEqual(queue["total"], 2)
            self.assertEqual(
                [row["task"].id for row in queue["reviews"]],
                retry_children[:2],
            )
            self.assertEqual(
                routes_tasks._ocr_retry_review_scope(
                    db, db.get(Task, retry_children[0]), str(operation_id),
                ),
                operation_id,
            )
            self.assertIsNone(
                routes_tasks._ocr_retry_review_scope(
                    db, db.get(Task, retry_children[0]),
                    str(outside_operation_id),
                )
            )
            with patch.object(bulk, "OCR_RETRY_REVIEW_PAGE_SIZE", 1):
                page_one = bulk.intake_ocr_retry_review_queue(db, operation_id, 1)
                page_two = bulk.intake_ocr_retry_review_queue(db, operation_id, 2)
            page_ids = [
                row["task"].id
                for page in (page_one, page_two)
                for row in page["reviews"]
            ]
            self.assertEqual(page_one["pages"], 2)
            self.assertEqual(page_ids, retry_children[:2])
            self.assertEqual(len(set(page_ids)), 2)
            self.assertEqual(
                bulk.next_ocr_retry_review_task_id(
                    db, operation_id, db.get(Task, retry_children[0]),
                ),
                retry_children[1],
            )
            self.assertEqual(
                bulk.next_ocr_retry_review_task_id(
                    db, operation_id, db.get(Task, retry_children[1]),
                ),
                retry_children[0],
            )
            self.assertIsNone(
                bulk.next_ocr_retry_review_task_id(
                    db, operation_id, db.get(Task, retry_children[2]),
                )
            )
            self.assertEqual(
                db.get(Task, retry_children[2]).document_id,
                db.get(Task, retry_children[0]).document_id,
            )
            outside = bulk.intake_ocr_retry_review_queue(
                db, db.get(Task, outside_parent_id).intake_operation_id,
            )
            self.assertEqual(
                [row["task"].id for row in outside["reviews"]],
                [retry_children[2]],
            )
            first = db.get(Task, retry_children[0])
            tasks.transition_task_status(first, TaskStatus.approved)
            db.flush()
            still_waiting = bulk.intake_ocr_retry_review_queue(db, operation_id)
            self.assertEqual(
                [row["task"].id for row in still_waiting["reviews"]],
                [retry_children[1]],
            )
            self.assertEqual(
                bulk.next_ocr_retry_review_task_id(db, operation_id, first),
                retry_children[1],
            )

    def test_ocr_retry_recovery_cursor_reaches_later_missing_intent(self) -> None:
        pairs = [self.create_failed_upload_receipt() for _ in range(2)]
        children = []
        run_ids = []
        for parent_id, failure_id in pairs:
            with session_scope() as db:
                run, child, _intent, _created = ocr_retries.request_retry(
                    db,
                    parent_task_id=parent_id,
                    failure_history_id=failure_id,
                    user_id=self.user_id,
                )
                children.append(child.id)
                run_ids.append(run.id)
        with session_scope() as db:
            db.execute(delete(JobOutbox).where(JobOutbox.task_id == children[1]))
        with patch.object(outbox, "_ocr_retry_scan_after_id", run_ids[0] - 1), patch.object(
            outbox, "active_ocr_task_ids", return_value={children[0]},
        ):
            with session_scope() as db:
                outbox.restore_pending_ocr_retry_intents(db, limit=1)
                self.assertEqual(
                    int(db.scalar(select(func.count()).select_from(JobOutbox).where(
                        JobOutbox.task_id == children[1], JobOutbox.job_kind == "ocr",
                    )) or 0),
                    0,
                )
            with session_scope() as db:
                restored = outbox.restore_pending_ocr_retry_intents(db, limit=1)
                self.assertEqual(len(restored), 1)
        with session_scope() as db:
            self.assertEqual(
                int(db.scalar(select(func.count()).select_from(JobOutbox).where(
                    JobOutbox.task_id == children[1], JobOutbox.job_kind == "ocr",
                )) or 0),
                1,
            )

    def test_retry_chain_attributes_incoming_and_outgoing_actors_separately(self) -> None:
        parent_id, failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            first_run, child, _intent, _created = ocr_retries.request_retry(
                db,
                parent_task_id=parent_id,
                failure_history_id=failure_id,
                user_id=self.user_id,
            )
            child_id = child.id
            first_actor_id = first_run.requested_by_id
        with session_scope() as db:
            child = db.get(Task, child_id)
            tasks.transition_task_status(child, TaskStatus.recognizing)
            db.flush()
            self.assertTrue(tasks.fail(db, child, "synthetic second OCR failure"))
            next_failure_id = db.scalar(
                select(TaskStatusHistory.id)
                .where(TaskStatusHistory.task_id == child_id)
                .order_by(TaskStatusHistory.id.desc())
                .limit(1)
            )
        with session_scope() as db:
            second_actor = User(
                login=f"synthetic-chain-reviewer-{os.urandom(8).hex()}",
                full_name="Synthetic Second Retry Reviewer",
                password_hash="synthetic-only",
                sro_login="synthetic-chain-reviewer",
                is_active=True,
            )
            db.add(second_actor)
            db.flush()
            second_actor_id = second_actor.id
            ocr_retries.request_retry(
                db,
                parent_task_id=child_id,
                failure_history_id=next_failure_id,
                user_id=second_actor_id,
            )
        with session_scope() as db:
            context = ocr_retries.context_for_task(db, db.get(Task, child_id))
        self.assertEqual(context.incoming_requested_by.id, first_actor_id)
        self.assertEqual(context.outgoing_requested_by.id, second_actor_id)

    def test_ocr_retry_rejects_kad_and_sro_executed_lineage(self) -> None:
        parent_id, failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            parent = db.get(Task, parent_id)
            parent.recognized = {"_kad_source_v1": {"source_identity_sha256": "a" * 64}}
        with session_scope() as db:
            with self.assertRaises(ocr_retries.OcrRetryConflict):
                ocr_retries.request_retry(
                    db,
                    parent_task_id=parent_id,
                    failure_history_id=failure_id,
                    user_id=self.user_id,
                )
            db.rollback()

        parent_id, failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            db.add(Event(
                task_id=parent_id,
                user_id=self.user_id,
                kind="preview_started",
                message="Operator preview is ready",
                payload={"screenshot_count": 0},
            ))
        with session_scope() as db:
            with self.assertRaises(ocr_retries.OcrRetryConflict):
                ocr_retries.request_retry(
                    db,
                    parent_task_id=parent_id,
                    failure_history_id=failure_id,
                    user_id=self.user_id,
                )
            db.rollback()

    def test_upload_rooted_retry_accepts_shared_canonical_kad_document(self) -> None:
        parent_id, failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            parent = db.get(Task, parent_id)
            document = db.get(Document, parent.document_id)
            document.source = IntakeSource.kad

        with session_scope() as db:
            run, child, intent_id, created = ocr_retries.request_retry(
                db,
                parent_task_id=parent_id,
                failure_history_id=failure_id,
                user_id=self.user_id,
            )
            self.assertTrue(created)
            self.assertIsNotNone(intent_id)
            self.assertIsNone(child.intake_item_id)
            self.assertEqual(run.source_operation_id, db.get(Task, parent_id).intake_operation_id)
            child_id = child.id
            retry_item_id = run.source_intake_item_id
            db.commit()

        with session_scope() as db:
            claim = leases.claim_ocr(db, child_id)
            self.assertIsNotNone(claim)
            retry_source = db.get(IntakeItem, retry_item_id)
            self.assertEqual(retry_source.source, IntakeSource.upload)
            child_task = db.get(Task, child_id)
            self.assertEqual(db.get(Document, child_task.document_id).source, IntakeSource.kad)

    def test_retry_child_with_partial_current_receipt_is_not_worker_eligible(self) -> None:
        parent_id, failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            _run, child, _intent_id, _created = ocr_retries.request_retry(
                db,
                parent_task_id=parent_id,
                failure_history_id=failure_id,
                user_id=self.user_id,
            )
            child_id = child.id
            run = db.scalar(select(OcrRetryRun).where(OcrRetryRun.child_task_id == child_id))
            operation_id = run.source_operation_id
            db.commit()

        with session_scope() as db:
            child = db.get(Task, child_id)
            child.intake_operation_id = operation_id
            db.commit()

        with session_scope() as db:
            self.assertIsNone(leases.claim_ocr(db, child_id))
            self.assertNotIn(child_id, leases.reconcilable_ocr_task_ids(db))

    def test_retry_rejects_mixed_upload_and_kad_source_operation(self) -> None:
        parent_id, failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            parent = db.get(Task, parent_id)
            source = db.get(IntakeItem, parent.intake_item_id)
            db.add(IntakeItem(
                operation_id=parent.intake_operation_id,
                item_index=source.item_index + 1,
                document_id=parent.document_id,
                filename="synthetic-mixed-kad.pdf",
                source=IntakeSource.kad,
                received_by_id=self.user_id,
            ))

        with session_scope() as db:
            with self.assertRaises(ocr_retries.OcrRetryConflict):
                ocr_retries.request_retry(
                    db,
                    parent_task_id=parent_id,
                    failure_history_id=failure_id,
                    user_id=self.user_id,
                )
            db.rollback()

    def test_ocr_retry_fails_closed_if_original_bytes_no_longer_match_receipt(self) -> None:
        parent_id, failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            parent = db.get(Task, parent_id)
            document = db.get(Document, parent.document_id)
            document.content_hash = "0" * 64
        with session_scope() as db:
            with self.assertRaises(ocr_retries.OcrRetrySourceUnavailable):
                ocr_retries.request_retry(
                    db,
                    parent_task_id=parent_id,
                    failure_history_id=failure_id,
                    user_id=self.user_id,
                )
            db.rollback()
            self.assertIsNone(db.scalar(
                select(OcrRetryRun).where(OcrRetryRun.parent_task_id == parent_id)
            ))

    def test_database_rejects_direct_ocr_retry_with_another_tasks_failure(self) -> None:
        parent_id, _failure_id = self.create_failed_upload_receipt()
        _other_parent_id, unrelated_failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            parent = db.get(Task, parent_id)
            document = db.get(Document, parent.document_id)
            item = db.get(IntakeItem, parent.intake_item_id)
            child = tasks.create_task(
                db, document, user_id=self.user_id, scenario="person_card"
            )
            with self.assertRaises(DBAPIError) as raised:
                db.execute(insert(OcrRetryRun).values(
                    parent_task_id=parent_id,
                    failure_history_id=unrelated_failure_id,
                    child_task_id=child.id,
                    source_intake_item_id=item.id,
                    source_operation_id=parent.intake_operation_id,
                    document_id=parent.document_id,
                    requested_by_id=self.user_id,
                ))
            self.assertEqual(getattr(raised.exception.orig, "sqlstate", None), "23514")
            db.rollback()

    def test_database_rejects_ocr_retry_with_partial_parent_receipt_context(self) -> None:
        parent_id, failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            parent = db.get(Task, parent_id)
            source_item_id = parent.intake_item_id
            source_operation_id = parent.intake_operation_id
            document = db.get(Document, parent.document_id)
            document_id = document.id
            parent.intake_item_id = None

        with session_scope() as db:
            child = tasks.create_task(
                db, db.get(Document, document_id), user_id=self.user_id, scenario="person_card"
            )
            with self.assertRaises(DBAPIError) as raised:
                db.execute(insert(OcrRetryRun).values(
                    parent_task_id=parent_id,
                    failure_history_id=failure_id,
                    child_task_id=child.id,
                    source_intake_item_id=source_item_id,
                    source_operation_id=source_operation_id,
                    document_id=document_id,
                    requested_by_id=self.user_id,
                ))
            self.assertEqual(getattr(raised.exception.orig, "sqlstate", None), "23514")
            db.rollback()

    def test_database_rejects_retry_child_rebound_to_a_new_receipt(self) -> None:
        parent_id, failure_id = self.create_failed_upload_receipt()
        with session_scope() as db:
            run, child, _intent_id, _created = ocr_retries.request_retry(
                db,
                parent_task_id=parent_id,
                failure_history_id=failure_id,
                user_id=self.user_id,
            )
            child_id = child.id
            source_operation_id = run.source_operation_id
            source_item_id = run.source_intake_item_id
            document_id = run.document_id
            db.commit()

        with session_scope() as db:
            child = db.get(Task, child_id)
            claim = leases.claim_ocr(db, child_id)
            self.assertIsNotNone(claim)
            self.assertTrue(leases.fail_ocr(db, claim, "SyntheticFailure"))
            failure_history_id = db.scalar(
                select(TaskStatusHistory.id)
                .where(TaskStatusHistory.task_id == child_id)
                .order_by(TaskStatusHistory.id.desc())
                .limit(1)
            )
            rebinding_item = IntakeItem(
                operation_id=source_operation_id,
                item_index=1,
                document_id=document_id,
                filename="synthetic-rebound-receipt.pdf",
                source=IntakeSource.upload,
                received_by_id=self.user_id,
            )
            db.add(rebinding_item)
            db.flush()
            child.intake_item_id = rebinding_item.id
            child.intake_operation_id = source_operation_id
            candidate = tasks.create_task(
                db, db.get(Document, document_id), user_id=self.user_id, scenario="person_card"
            )
            candidate_id = candidate.id

        with session_scope() as db:
            with self.assertRaises(DBAPIError) as raised:
                db.execute(insert(OcrRetryRun).values(
                    parent_task_id=child_id,
                    failure_history_id=failure_history_id,
                    child_task_id=candidate_id,
                    source_intake_item_id=source_item_id,
                    source_operation_id=source_operation_id,
                    document_id=document_id,
                    requested_by_id=self.user_id,
                ))
            self.assertEqual(getattr(raised.exception.orig, "sqlstate", None), "23514")
            db.rollback()

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

    def test_exact_rejection_response_replay_returns_success_without_new_event(self) -> None:
        self.task_id = self.create_task_with_status(TaskStatus.needs_review)
        request = SimpleNamespace(scope={"state": {"request_id": "e" * 32}})
        user = SimpleNamespace(id=self.user_id)
        with session_scope() as db:
            first = task_reject(request, self.task_id, user, db)
        self.assertEqual(first.status_code, 303)

        with session_scope() as db:
            before = int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id, Event.kind == "rejected",
                )
            ) or 0)
            replay = task_reject(request, self.task_id, user, db)
            after = int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id, Event.kind == "rejected",
                )
            ) or 0)
            task = db.get(Task, self.task_id)

        self.assertEqual(replay.status_code, 303)
        self.assertEqual(task.status, TaskStatus.rejected)
        self.assertEqual(before, 1)
        self.assertEqual(after, before)

    def test_local_validation_retry_reuses_pending_intent_or_acknowledges_delivery(self) -> None:
        from app.api.routes_tasks import task_run

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
            self.task_id = task.id
            tasks.approve(db, task, self.user_id)
            first_intent = tasks.request_local_validation(db, task, self.user_id, "1" * 32)
            replayed_intent = tasks.request_local_validation(db, task, self.user_id, "2" * 32)
            self.assertIsNotNone(first_intent)
            self.assertEqual(replayed_intent, first_intent)
            self.assertEqual(int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id,
                    Event.kind == "local_validation_requested",
                )
            ) or 0), 1)
            self.assertEqual(int(db.scalar(
                select(func.count()).select_from(JobOutbox).where(
                    JobOutbox.task_id == self.task_id,
                    JobOutbox.job_kind == "local_validation",
                )
            ) or 0), 1)

        request = SimpleNamespace(scope={"state": {"request_id": "a" * 32}})
        user = SimpleNamespace(id=self.user_id)

        def deliver_and_finish_local_validation(db, intent_id):
            intent = db.get(JobOutbox, intent_id)
            self.assertIsNotNone(intent)
            self.assertEqual(intent.job_kind, "local_validation")
            task = db.get(Task, intent.task_id)
            db.delete(intent)
            db.commit()
            tasks.finish_local_validation(db, task, self.user_id)
            db.commit()

        with patch("app.api.routes_tasks.outbox.dispatch_required", side_effect=deliver_and_finish_local_validation) as dispatch:
            with session_scope() as db:
                first = task_run(request, self.task_id, user, db)
            with session_scope() as db:
                replay = task_run(request, self.task_id, user, db)

        self.assertEqual(first.status_code, 303)
        self.assertEqual(replay.status_code, 303)
        dispatch.assert_called_once()
        with session_scope() as db:
            requested = int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == self.task_id,
                    Event.kind == "local_validation_requested",
                )
            ) or 0)
            pending = int(db.scalar(
                select(func.count()).select_from(JobOutbox).where(
                    JobOutbox.task_id == self.task_id,
                    JobOutbox.job_kind == "local_validation",
                )
            ) or 0)
            task = db.get(Task, self.task_id)
            self.assertEqual(requested, 1)
            self.assertEqual(pending, 0)
            self.assertEqual(task.status, TaskStatus.done)

    def test_failed_login_audit_is_durable_and_contains_no_attempted_credentials(self) -> None:
        def disposable_session():
            with SessionLocal() as db:
                yield db

        prior_override = app.dependency_overrides.get(get_session)
        app.dependency_overrides[get_session] = disposable_session
        try:
            with patch("app.api.routes_auth.authenticate_sro_credentials", side_effect=SroLoginError("rejected")):
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

    def test_assignment_edit_replay_with_stale_version_returns_committed_state(self) -> None:
        values = {name: "" for name, _label in assignments.ASSIGNMENT_FIELDS}
        values["practitioner"] = "Синтетический управляющий"
        with session_scope() as db:
            card = assignments.create_manual_debtor_card(db, self.user_id)
            assignments.update_debtor_card(
                db,
                card.id,
                self.user_id,
                card.version,
                {"last_name": "Тестов", "first_name": "Иван"},
                confirm=True,
            )
            draft = assignments.create_assignment_draft(db, card.id, self.user_id)
            draft_id = draft.id
            assignments.update_assignment_draft(
                db,
                draft.id,
                self.user_id,
                draft.version,
                "А40-12345/2026",
                values,
                "awaiting_information",
            )

        with session_scope() as db:
            draft = db.get(AssignmentDraft, draft_id)
            committed_version = draft.version
            committed_actor = draft.updated_by_id
            assignment_revisions = db.scalar(
                select(func.count()).select_from(AssignmentDraftRevision).where(
                    AssignmentDraftRevision.assignment_draft_id == draft_id
                )
            )
            replay = assignments.update_assignment_draft(
                db,
                draft_id,
                self.user_id,
                committed_version - 1,
                "А40-12345/2026",
                values,
                "awaiting_information",
            )
            self.assertEqual(replay.version, committed_version)
            self.assertEqual(replay.updated_by_id, committed_actor)
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(AssignmentDraftRevision).where(
                        AssignmentDraftRevision.assignment_draft_id == draft_id
                    )
                ),
                assignment_revisions,
            )

    def test_debtor_card_edit_replay_with_stale_version_returns_committed_state(self) -> None:
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["person"]}
        values.update(last_name="Синтетический", first_name="Должник")
        with session_scope() as db:
            card = assignments.create_manual_debtor_card(db, self.user_id)
            card_id = card.id
            assignments.update_debtor_card(
                db, card_id, self.user_id, card.version, values, confirm=True,
            )

        with session_scope() as db:
            card = db.get(DebtorCardDraft, card_id)
            committed_version = card.version
            committed_actor = card.updated_by_id
            revisions = db.scalar(
                select(func.count()).select_from(DebtorCardDraftRevision).where(
                    DebtorCardDraftRevision.debtor_card_id == card_id
                )
            )
            replay = assignments.update_debtor_card(
                db, card_id, self.user_id, committed_version - 1, values, confirm=True,
            )
            self.assertEqual(replay.version, committed_version)
            self.assertEqual(replay.updated_by_id, committed_actor)
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(DebtorCardDraftRevision).where(
                        DebtorCardDraftRevision.debtor_card_id == card_id
                    )
                ),
                revisions,
            )

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
        self.task_id = self.create_task_with_status(TaskStatus.new)
        with session_scope() as db:
            intent_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7002,
                job_kind="ocr",
            )

        with patch.object(outbox, "active_ocr_task_ids", return_value=set()), patch.object(
            outbox, "_enqueue", side_effect=QueueUnavailable("ConnectionError")
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

        with patch.object(outbox, "active_ocr_task_ids", return_value=set()), patch.object(
            outbox, "_enqueue"
        ) as enqueue:
            with SessionLocal() as db:
                self.assertEqual(
                    outbox.dispatch_intent(db, intent_id), outbox.DispatchOutcome.CONFIRMED
                )
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
                approval_event_id=1,
            )

        from app.workers import queues

        with patch.object(queues, "enqueue_write", return_value="synthetic-rq-id") as enqueue:
            with SessionLocal() as db:
                self.assertEqual(
                    outbox.dispatch_intent(db, intent_id), outbox.DispatchOutcome.CONFIRMED
                )

        enqueue.assert_called_once_with(
            self.task_id,
            live=False,
            user_id=self.user_id,
        )

    def test_legacy_kad_intent_is_withheld_without_attempt_or_worker_enqueue(self) -> None:
        with session_scope() as db:
            before = outbox.status_summary(db)
            intent_id = db.execute(
                insert(JobOutbox)
                .values(
                    task_id=self.task_id,
                    job_kind="kad",
                    dedupe_key=hashlib.sha256(b"legacy-kad-intent").hexdigest(),
                    actor_id=None,
                )
                .returning(JobOutbox.id)
            ).scalar_one()
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
                self.assertEqual(
                    outbox.dispatch_intent(db, intent_id), outbox.DispatchOutcome.DEFERRED
                )
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
        self.task_id = self.create_task_with_status(TaskStatus.new)
        with session_scope() as db:
            intent_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7003,
                job_kind="ocr",
            )

        with patch.object(outbox, "active_ocr_task_ids", return_value=set()), patch.object(
            outbox, "_enqueue", side_effect=RuntimeError("synthetic process exit")
        ) as enqueue:
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

        with patch.object(outbox, "active_ocr_task_ids", return_value=set()), patch.object(
            outbox, "_enqueue"
        ) as retry_enqueue:
            with SessionLocal() as db:
                self.assertEqual(
                    outbox.dispatch_intent(db, intent_id), outbox.DispatchOutcome.CONFIRMED
                )
        retry_enqueue.assert_called_once()

    def test_concurrent_outbox_dispatchers_do_not_enqueue_the_same_active_claim(self) -> None:
        self.task_id = self.create_task_with_status(TaskStatus.new)
        with session_scope() as db:
            intent_id = outbox.create_intake_intent(
                db,
                task_id=self.task_id,
                operation_id=7004,
                job_kind="ocr",
            )

        enqueue_entered = threading.Event()
        allow_enqueue_to_finish = threading.Event()
        first_result: list[outbox.DispatchOutcome] = []
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

        with patch.object(outbox, "active_ocr_task_ids", return_value=set()), patch.object(
            outbox, "_enqueue", side_effect=blocked_enqueue
        ) as enqueue:
            thread = threading.Thread(target=first_dispatcher, daemon=True)
            thread.start()
            self.assertTrue(enqueue_entered.wait(timeout=5))
            try:
                with SessionLocal() as db:
                    self.assertEqual(
                        outbox.dispatch_intent(db, intent_id), outbox.DispatchOutcome.DEFERRED
                    )
            finally:
                allow_enqueue_to_finish.set()
                thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(first_result, [outbox.DispatchOutcome.CONFIRMED])
        enqueue.assert_called_once()

    def test_active_ocr_delivery_defers_without_losing_recovery_intent(self) -> None:
        self.task_id = self.create_task_with_status(TaskStatus.new)
        with session_scope() as db:
            intent_id = outbox.create_intake_intent(
                db, task_id=self.task_id, operation_id=7008, job_kind="ocr"
            )

        with patch.object(outbox, "active_ocr_task_ids", return_value={self.task_id}), patch.object(
            outbox, "_enqueue"
        ) as enqueue:
            with SessionLocal() as db:
                self.assertEqual(
                    outbox.dispatch_intent(db, intent_id), outbox.DispatchOutcome.DEFERRED
                )
        enqueue.assert_not_called()
        with session_scope() as db:
            intent = db.get(JobOutbox, intent_id)
            self.assertIsNotNone(intent)
            self.assertIsNone(intent.lease_token)
            self.assertIsNone(intent.lease_until)
            self.assertGreater(intent.available_at, db.scalar(select(func.clock_timestamp())))
            db.execute(
                update(JobOutbox).where(JobOutbox.id == intent_id).values(
                    available_at=func.clock_timestamp()
                )
            )

        with patch.object(outbox, "active_ocr_task_ids", return_value=set()), patch.object(
            outbox, "_enqueue"
        ) as enqueue:
            with SessionLocal() as db:
                self.assertEqual(
                    outbox.dispatch_intent(db, intent_id), outbox.DispatchOutcome.CONFIRMED
                )
        enqueue.assert_called_once()

    def test_advanced_task_discards_intent_as_already_started(self) -> None:
        self.task_id = self.create_task_with_status(TaskStatus.new)
        with session_scope() as db:
            db.execute(
                update(Task)
                .where(Task.id == self.task_id)
                .values(status=TaskStatus.recognizing)
            )
            intent_id = outbox.create_intake_intent(
                db, task_id=self.task_id, operation_id=7010, job_kind="ocr"
            )

        with patch.object(outbox, "_enqueue") as enqueue:
            with SessionLocal() as db:
                outcome = outbox.dispatch_intent(db, intent_id)
        enqueue.assert_not_called()
        self.assertEqual(outcome, outbox.DispatchOutcome.ALREADY_STARTED)
        with session_scope() as db:
            self.assertIsNone(db.get(JobOutbox, intent_id))

    def test_unreadable_ocr_queue_keeps_intent_for_later_retry(self) -> None:
        self.task_id = self.create_task_with_status(TaskStatus.new)
        with session_scope() as db:
            intent_id = outbox.create_intake_intent(
                db, task_id=self.task_id, operation_id=7009, job_kind="ocr"
            )

        with patch.object(
            outbox, "active_ocr_task_ids", side_effect=QueueUnavailable("ConnectionError")
        ), patch.object(outbox, "_enqueue") as enqueue:
            with SessionLocal() as db, self.assertRaises(QueueUnavailable):
                outbox.dispatch_intent(db, intent_id)
        enqueue.assert_not_called()
        with session_scope() as db:
            intent = db.get(JobOutbox, intent_id)
            self.assertIsNotNone(intent)
            self.assertEqual(intent.last_error_type, "ConnectionError")
            self.assertIsNone(intent.lease_token)

    def test_shared_upload_batch_recovers_lost_ack_across_staff_without_rebinding_receipt(self) -> None:
        with session_scope() as db:
            second_user = User(
                login=f"synthetic-upload-resume-{os.urandom(8).hex()}",
                full_name="Synthetic Upload Resumer",
                password_hash="synthetic-only",
                sro_login="synthetic-upload-resume",
                is_active=True,
            )
            db.add(second_user)
            db.flush()
            second_user_id = second_user.id

        contents = [b"synthetic transport part one", b"synthetic transport part two"]
        filenames = ["synthetic-transport-one.pdf", "synthetic-transport-two.pdf"]
        manifest_files = [
            {
                "filename": filename,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for filename, content in zip(filenames, contents, strict=True)
        ]
        def db_override():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        def current_user_override(request: Request):
            return SimpleNamespace(id=int(request.headers.get("x-synthetic-actor", self.user_id)))

        app.dependency_overrides[get_session] = db_override
        app.dependency_overrides[current_user] = current_user_override
        try:
            with tempfile.TemporaryDirectory(prefix="sro-upload-transport-") as storage:
                with patch.object(settings, "upload_dir", Path(storage)), TestClient(app) as client:
                    registered = client.post(
                        "/upload-batches",
                        json={
                            "request_key": "a" * 48,
                            "files_per_chunk": 1,
                            "files": manifest_files,
                        },
                        headers={"Origin": "http://testserver", "Accept": "application/json"},
                    )
                    self.assertEqual(registered.status_code, 201, registered.text)
                    batch_id = registered.json()["id"]

                    incomplete_repeat = client.post(
                        f"/upload-batches/{batch_id}/reprocess",
                        data={"request_key": "d" * 48},
                        headers={"Origin": "http://testserver", "X-Synthetic-Actor": str(second_user_id)},
                    )
                    self.assertEqual(incomplete_repeat.status_code, 409, incomplete_repeat.text)

                    def send_chunk(index, content, filename, actor_id=None):
                        headers = {
                            "Origin": "http://testserver",
                            "Accept": "application/json",
                            "X-Synthetic-Actor": str(actor_id or self.user_id),
                        }
                        return client.post(
                            f"/upload-batches/{batch_id}/chunks/{index}",
                            files={"files": (filename, content, "application/pdf")},
                            headers=headers,
                        )

                    with patch.object(
                        routes_upload_batches.outbox,
                        "dispatch_required",
                        side_effect=QueueUnavailable("synthetic queue outage after commit"),
                    ):
                        lost_ack = send_chunk(0, contents[0], filenames[0])
                    self.assertEqual(lost_ack.status_code, 503)
                    with session_scope() as db:
                        original_operation_id = db.scalar(
                            select(UploadTransportChunkReceipt.operation_id).where(
                                UploadTransportChunkReceipt.batch_id == batch_id,
                                UploadTransportChunkReceipt.chunk_index == 0,
                            )
                        )

                    state = client.get(
                        "/upload-batches",
                        headers={"Accept": "application/json", "X-Synthetic-Actor": str(second_user_id)},
                    ).json()
                    listed = next(item for item in state["batches"] if item["id"] == batch_id)
                    self.assertTrue(listed["chunks"][0]["accepted"])
                    self.assertFalse(listed["chunks"][1]["accepted"])

                    with patch.object(routes_upload_batches.outbox, "dispatch_required"):
                        replay = send_chunk(0, contents[0], filenames[0], second_user_id)
                        mismatch = send_chunk(0, b"changed synthetic payload", filenames[0], second_user_id)
                    self.assertEqual(replay.status_code, 200, replay.text)
                    self.assertEqual(mismatch.status_code, 409)
                    self.assertEqual(replay.json()["operation_id"], original_operation_id)

                    barrier = threading.Barrier(2)

                    def race_missing_chunk(user_id):
                        barrier.wait(timeout=10)
                        return send_chunk(1, contents[1], filenames[1], user_id)

                    with patch.object(routes_upload_batches.outbox, "dispatch_required"):
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            outcomes = list(pool.map(race_missing_chunk, (self.user_id, second_user_id)))
                    self.assertEqual(
                        [response.status_code for response in outcomes],
                        [200, 200],
                        [response.text for response in outcomes],
                    )
                    operation_ids = {response.json()["operation_id"] for response in outcomes}
                    self.assertEqual(len(operation_ids), 1)
                    self.assertEqual(
                        sorted(response.json()["created"] for response in outcomes),
                        [False, True],
                    )
                    self.assertEqual(next(iter(operation_ids)), outcomes[0].json()["operation_id"])

                    repeat_key = "e" * 48
                    repeat_barrier = threading.Barrier(2)

                    def create_explicit_repeat(user_id):
                        repeat_barrier.wait(timeout=10)
                        return client.post(
                            f"/upload-batches/{batch_id}/reprocess",
                            data={"request_key": repeat_key},
                            headers={"Origin": "http://testserver", "X-Synthetic-Actor": str(user_id)},
                        )

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        repeat_outcomes = list(pool.map(
                            create_explicit_repeat, (self.user_id, second_user_id)
                        ))
                    self.assertEqual(sorted(response.status_code for response in repeat_outcomes), [200, 201])
                    repeated_child_ids = {response.json()["batch_id"] for response in repeat_outcomes}
                    self.assertEqual(len(repeated_child_ids), 1)
                    child_id = repeated_child_ids.pop()
                    with session_scope() as db:
                        child = db.get(UploadTransportBatch, child_id)
                        original_batch = db.get(UploadTransportBatch, batch_id)
                        child_items = list(db.scalars(
                            select(UploadTransportBatchItem)
                            .where(UploadTransportBatchItem.batch_id == child_id)
                            .order_by(UploadTransportBatchItem.file_index)
                        ))
                        child_receipts = list(db.scalars(
                            select(UploadTransportChunkReceipt).where(
                                UploadTransportChunkReceipt.batch_id == child_id
                            )
                        ))
                        original_counts = (
                            db.scalar(select(func.count()).select_from(Task).where(Task.intake_operation_id.in_(
                                select(UploadTransportChunkReceipt.operation_id).where(
                                    UploadTransportChunkReceipt.batch_id == batch_id
                                )
                            ))),
                            db.scalar(select(func.count()).select_from(JobOutbox).where(JobOutbox.task_id.in_(
                                select(Task.id).where(Task.intake_operation_id.in_(
                                    select(UploadTransportChunkReceipt.operation_id).where(
                                        UploadTransportChunkReceipt.batch_id == batch_id
                                    )
                                ))
                            ))),
                        )
                    self.assertIn(child.created_by_id, {self.user_id, second_user_id})
                    self.assertEqual(child.reprocess_parent_id, batch_id)
                    self.assertEqual(child.reprocess_parent_manifest_sha256, original_batch.manifest_sha256)
                    self.assertEqual(len(child_items), 2)
                    self.assertEqual(child_receipts, [])
                    self.assertEqual(original_counts, (2, 2))

                    with self.assertRaises(DBAPIError):
                        with SessionLocal() as db:
                            db.execute(insert(UploadTransportBatch).values(
                                id=os.urandom(16).hex(),
                                created_by_id=self.user_id,
                                request_key_sha256=routes_upload_batches.upload_transport.request_key_sha256(os.urandom(24).hex()),
                                manifest_sha256=original_batch.manifest_sha256,
                                files_per_chunk=original_batch.files_per_chunk,
                                file_count=original_batch.file_count,
                                total_bytes=original_batch.total_bytes,
                                reprocess_parent_id=batch_id,
                                reprocess_parent_manifest_sha256=original_batch.manifest_sha256,
                                reprocess_request_key_sha256=None,
                            ))
                            db.commit()

                    with self.assertRaises(DBAPIError):
                        with SessionLocal() as db:
                            forged_id = os.urandom(16).hex()
                            db.execute(insert(UploadTransportBatch).values(
                                id=forged_id,
                                created_by_id=self.user_id,
                                request_key_sha256=routes_upload_batches.upload_transport.request_key_sha256(os.urandom(24).hex()),
                                manifest_sha256=original_batch.manifest_sha256,
                                files_per_chunk=original_batch.files_per_chunk,
                                file_count=original_batch.file_count,
                                total_bytes=original_batch.total_bytes,
                                reprocess_parent_id=batch_id,
                                reprocess_parent_manifest_sha256=original_batch.manifest_sha256,
                                reprocess_request_key_sha256=os.urandom(32).hex(),
                            ))
                            db.execute(insert(UploadTransportBatchItem).values(
                                batch_id=forged_id,
                                file_index=0,
                                chunk_index=0,
                                filename=child_items[0].filename,
                                file_size=child_items[0].file_size,
                                content_sha256="0" * 64,
                            ))
                            db.execute(insert(UploadTransportBatchItem).values(
                                batch_id=forged_id,
                                file_index=1,
                                chunk_index=1,
                                filename=child_items[1].filename,
                                file_size=child_items[1].file_size,
                                content_sha256=child_items[1].content_sha256,
                            ))
                            db.commit()

                    another_intentional_repeat = client.post(
                        f"/upload-batches/{batch_id}/reprocess",
                        data={"request_key": "f" * 48},
                        headers={"Origin": "http://testserver", "X-Synthetic-Actor": str(self.user_id)},
                    )
                    self.assertEqual(another_intentional_repeat.status_code, 201)
                    self.assertNotEqual(another_intentional_repeat.json()["batch_id"], child_id)

                    other_parent = client.post(
                        "/upload-batches",
                        json={
                            "request_key": "b" * 48,
                            "files_per_chunk": 1,
                            "files": [{
                                "filename": "synthetic-other-parent.pdf",
                                "size": len(contents[0]),
                                "sha256": hashlib.sha256(contents[0]).hexdigest(),
                            }],
                        },
                        headers={"Origin": "http://testserver", "Accept": "application/json"},
                    )
                    self.assertEqual(other_parent.status_code, 201, other_parent.text)
                    with self.assertRaises(DBAPIError):
                        with SessionLocal() as db:
                            parent = db.get(UploadTransportBatch, other_parent.json()["id"])
                            source_item = db.scalar(select(UploadTransportBatchItem).where(
                                UploadTransportBatchItem.batch_id == parent.id
                            ))
                            incomplete_child_id = os.urandom(16).hex()
                            db.execute(insert(UploadTransportBatch).values(
                                id=incomplete_child_id,
                                created_by_id=self.user_id,
                                request_key_sha256=routes_upload_batches.upload_transport.request_key_sha256(os.urandom(24).hex()),
                                manifest_sha256=parent.manifest_sha256,
                                files_per_chunk=parent.files_per_chunk,
                                file_count=parent.file_count,
                                total_bytes=parent.total_bytes,
                                reprocess_parent_id=parent.id,
                                reprocess_parent_manifest_sha256=parent.manifest_sha256,
                                reprocess_request_key_sha256=os.urandom(32).hex(),
                            ))
                            db.execute(insert(UploadTransportBatchItem).values(
                                batch_id=incomplete_child_id,
                                file_index=source_item.file_index,
                                chunk_index=source_item.chunk_index,
                                filename=source_item.filename,
                                file_size=source_item.file_size,
                                content_sha256=source_item.content_sha256,
                            ))
                            db.commit()
                    reused_for_other_parent = client.post(
                        f"/upload-batches/{other_parent.json()['id']}/reprocess",
                        data={"request_key": repeat_key},
                        headers={"Origin": "http://testserver", "X-Synthetic-Actor": str(second_user_id)},
                    )
                    self.assertEqual(reused_for_other_parent.status_code, 409)
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(current_user, None)

        with session_scope() as db:
            receipts = list(
                db.scalars(
                    select(UploadTransportChunkReceipt)
                    .where(UploadTransportChunkReceipt.batch_id == batch_id)
                    .order_by(UploadTransportChunkReceipt.chunk_index)
                )
            )
            batch = db.get(UploadTransportBatch, batch_id)
            items = list(db.scalars(
                select(UploadTransportBatchItem).where(UploadTransportBatchItem.batch_id == batch_id)
            ))
        self.assertEqual(batch.file_count, 2)
        self.assertEqual(len(items), 2)
        self.assertEqual([row.chunk_index for row in receipts], [0, 1])
        self.assertEqual(receipts[0].accepted_by_id, self.user_id)
        self.assertIn(receipts[1].accepted_by_id, {self.user_id, second_user_id})
        with self.assertRaises(DBAPIError):
            with SessionLocal() as db:
                db.execute(
                    update(UploadTransportChunkReceipt)
                    .where(UploadTransportChunkReceipt.batch_id == batch_id)
                    .values(accepted_by_id=self.user_id)
                )
                db.commit()

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

        previous_errors = dict(before.last_error_counts)
        current_errors = dict(summary.last_error_counts)
        error_deltas = {
            error_type: current_errors.get(error_type, 0) - previous_errors.get(error_type, 0)
            for error_type in set(previous_errors) | set(current_errors)
        }
        error_deltas = {error_type: count for error_type, count in error_deltas.items() if count}

        self.assertEqual(summary.pending - before.pending, 3)
        self.assertEqual(summary.eligible - before.eligible, 1)
        self.assertEqual(summary.leased - before.leased, 1)
        self.assertEqual(summary.delayed - before.delayed, 1)
        self.assertIsNotNone(summary.oldest_age_seconds)
        self.assertEqual(
            error_deltas,
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

    def test_audit_contract_trigger_requires_mandatory_evidence_fields(self) -> None:
        required_kinds = (
            "ocr_attempts_exhausted",
            "ocr_claimed",
            "ocr_claim_completed",
            "ocr_claim_failed",
            "operator_action_not_applied",
            "task_claimed",
            "fields_saved",
            "approved",
            "local_validation_requested",
            "writing_started",
            "duplicate",
            "local_validation_completed",
            "preview_started",
            "live_save_blocked",
        )
        for kind in required_kinds:
            with self.subTest(kind=kind):
                actor_required = kind in tasks._AUDIT_EVENT_ACTOR_REQUIRED
                task_required = kind in tasks._AUDIT_EVENT_TASK_REQUIRED
                try:
                    with session_scope() as db:
                        db.execute(
                            text(
                                "INSERT INTO public.event (task_id, user_id, kind, message, payload) "
                                "VALUES (:task_id, :user_id, :kind, 'ignored', '{}'::jsonb)"
                            ),
                            {
                                "task_id": self.task_id if task_required else None,
                                "user_id": self.user_id if actor_required else None,
                                "kind": kind,
                            },
                        )
                except DBAPIError as error:
                    self.assertEqual(error.orig.sqlstate, "23514")
                    self.assertIn("missing required audit event payload fields", str(error.orig))
                else:
                    self.fail(f"Audit event kind {kind} accepted missing required fields")

    def test_audit_contract_trigger_requires_actor_and_task_context(self) -> None:
        global_kinds = {"session_started", "session_login_failed", "session_ended"}
        actor_kinds = set(tasks._AUDIT_EVENT_ACTOR_REQUIRED)
        task_kinds = set(tasks._AUDIT_EVENT_TASK_REQUIRED)
        self.assertEqual(task_kinds, set(tasks._AUDIT_EVENT_PAYLOAD_KEYS) - global_kinds)

        for kind in actor_kinds:
            with self.subTest(kind=kind, condition="actor"):
                try:
                    with session_scope() as db:
                        db.execute(
                            text(
                                "INSERT INTO public.event (task_id, user_id, kind, message, payload) "
                                "VALUES (:task_id, NULL, :kind, 'ignored', '{}'::jsonb)"
                            ),
                            {
                                "task_id": self.task_id if kind in task_kinds else None,
                                "kind": kind,
                            },
                        )
                except DBAPIError as error:
                    self.assertEqual(error.orig.sqlstate, "23514")
                    self.assertIn("attributable user", str(error.orig))
                else:
                    self.fail(f"Audit event kind {kind} accepted a missing actor")

        for kind in task_kinds:
            with self.subTest(kind=kind, condition="task"):
                try:
                    with session_scope() as db:
                        db.execute(
                            text(
                                "INSERT INTO public.event (task_id, user_id, kind, message, payload) "
                                "VALUES (NULL, :user_id, :kind, 'ignored', '{}'::jsonb)"
                            ),
                            {
                                "user_id": self.user_id if kind in actor_kinds else None,
                                "kind": kind,
                            },
                        )
                except DBAPIError as error:
                    self.assertEqual(error.orig.sqlstate, "23514")
                    self.assertIn("task context", str(error.orig))
                else:
                    self.fail(f"Task event kind {kind} accepted missing task context")

    def test_global_session_events_remain_taskless(self) -> None:
        with session_scope() as db:
            db.execute(
                text(
                    "INSERT INTO public.event (task_id, user_id, kind, message, payload) "
                    "VALUES (NULL, NULL, 'session_login_failed', 'ignored', '{}'::jsonb), "
                    "(NULL, :user_id, 'session_started', 'ignored', '{}'::jsonb), "
                    "(NULL, :user_id, 'session_ended', 'ignored', '{}'::jsonb)"
                ),
                {"user_id": self.user_id},
            )

    def test_runtime_guard_readback_detects_missing_required_table(self) -> None:
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text(
                "ALTER TABLE public.debtor_card_create_request "
                "RENAME TO debtor_card_create_request_hidden"
            ))
            differences = runtime_guard_differences(connection)
            transaction.rollback()
        self.assertIn(
            "required runtime table missing: debtor_card_create_request",
            differences,
        )

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

    def test_runtime_guard_readback_detects_missing_ocr_retry_lineage_constraint(self) -> None:
        with engine.connect() as connection:
            transaction = connection.begin()
            self.assertEqual(runtime_guard_differences(connection), [])
            connection.execute(text(
                "ALTER TABLE public.ocr_retry_run DROP CONSTRAINT uq_ocr_retry_run_child"
            ))
            differences = runtime_guard_differences(connection)
            transaction.rollback()
        self.assertIn("OCR retry lineage constraints missing or ambiguous", differences)

    def test_runtime_guard_refuses_missing_shared_upload_registration_identity(self) -> None:
        with engine.connect() as connection:
            transaction = connection.begin()
            self.assertEqual(runtime_guard_differences(connection), [])
            connection.execute(text(
                "ALTER TABLE public.upload_transport_batch "
                "DROP CONSTRAINT uq_upload_transport_batch_creator_key"
            ))
            differences = runtime_guard_differences(connection)
            transaction.rollback()
        self.assertIn("shared upload transport identity constraints missing or ambiguous", differences)

    def test_direct_user_deactivation_update_revokes_active_sessions(self) -> None:
        token = os.urandom(32).hex()
        with session_scope() as db:
            self.issue_test_login_session(db, self.user_id, token)
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
            self.issue_test_login_session(db, self.user_id, token)

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
        # The immutable-history trigger normally raises 55000. New composite
        # retry-lineage FKs may reject TRUNCATE first with PostgreSQL 0A000.
        self.assertIn(truncate_error.exception.orig.sqlstate, {"55000", "0A000"})

        with session_scope() as db:
            saved = db.get(TaskStatusHistory, history_id)
            self.assertEqual(saved.from_status, "needs_review")
            self.assertEqual(saved.to_status, "approved")

    def test_direct_task_status_history_insert_is_rejected(self) -> None:
        with self.assertRaises(DBAPIError) as insert_error:
            with session_scope() as db:
                db.execute(
                    text(
                        "INSERT INTO public.task_status_history "
                        "(task_id, from_status, to_status) "
                        "VALUES (:task_id, 'new', 'done')"
                    ),
                    {"task_id": self.task_id},
                )
        self.assertEqual(insert_error.exception.orig.sqlstate, "55000")
        with session_scope() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count(TaskStatusHistory.id)).where(
                        TaskStatusHistory.task_id == self.task_id
                    )
                ),
                0,
            )

    def test_runtime_guard_readback_detects_status_history_insert_guard_drift(self) -> None:
        with engine.connect() as connection:
            transaction = connection.begin()
            self.assertEqual(runtime_guard_differences(connection), [])
            connection.execute(
                text(
                    "ALTER TABLE public.task_status_history DISABLE TRIGGER "
                    "task_status_history_require_transition"
                )
            )
            self.assertIn(
                "critical runtime trigger shape differs: "
                "task_status_history_require_transition",
                runtime_guard_differences(connection),
            )
            connection.execute(
                text(
                    "ALTER TABLE public.task_status_history ENABLE TRIGGER "
                    "task_status_history_require_transition"
                )
            )
            connection.execute(
                text(
                    "CREATE OR REPLACE FUNCTION "
                    "public.sro_require_triggered_status_history_insert() "
                    "RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog "
                    "AS $$ BEGIN RETURN NEW; END; $$"
                )
            )
            self.assertIn(
                "critical runtime trigger function differs: "
                "sro_require_triggered_status_history_insert",
                runtime_guard_differences(connection),
            )
            transaction.rollback()

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
            first = self.issue_test_login_session(db, self.user_id, first_token)
            second = self.issue_test_login_session(db, self.user_id, second_token)
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
            self.issue_test_login_session(db, self.user_id, active_token)

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
            self.issue_test_login_session(db, self.user_id, token)

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
        self.issue_test_login_session(login_transaction, self.user_id, token)
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
        self.issue_test_login_session(login_transaction, self.user_id, token)
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
            self.issue_test_login_session(db, self.user_id, token)

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
