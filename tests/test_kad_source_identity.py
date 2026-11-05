from __future__ import annotations

import hashlib
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, select

from app.db.base import SessionLocal, engine, session_scope
from app.db.schema import EXPECTED_SCHEMA_REVISION
from app.db.models import (
    Document,
    IntakeItem,
    IntakeOperation,
    IntakeSource,
    KadImportRequestReceipt,
    KadSourceRun,
    Task,
    User,
)
from app.services import kad_import, kad_operations, leases, tasks
from tests.postgres_safety import assert_disposable_postgres


CARD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PDF_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PDF_BYTES = b"%PDF-1.4\nsynthetic source identity fixture\n%%EOF\n"


def source_payload(pdf_id: str = PDF_ID, request_id: str = "12345678-1234-1234-1234-123456789abc") -> dict:
    selected = f"https://kad.arbitr.ru/Kad/PdfDocument/{CARD_ID}/{pdf_id}/act.pdf"
    card = f"https://kad.arbitr.ru/Card/{CARD_ID}"
    return kad_import.validate_source({
        "requestId": request_id,
        "caseNumber": "А41-12345/2026",
        "cardUrl": card,
        "candidateInventoryComplete": False,
        "selectedUrl": selected,
        "candidates": [{"url": selected, "label": "Synthetic act"}],
        "selectionReason": "employee_selected_visible_document",
    })


@unittest.skipUnless(
    os.environ.get("SRO_TEST_POSTGRES") == "1",
    "requires the disposable PostgreSQL migration drill",
)
class KadSourceIdentityPostgreSQLTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_disposable_postgres()
        with engine.connect() as connection:
            self.assertEqual(MigrationContext.configure(connection).get_current_revision(), EXPECTED_SCHEMA_REVISION)
        self.card_id = str(uuid.uuid4())
        self.pdf_id = str(uuid.uuid4())
        self.key_prefix = os.urandom(16).hex()
        with session_scope() as db:
            self.pdf_bytes = PDF_BYTES + os.urandom(8)
            self.users = []
            for index in range(2):
                user = User(
                    login=f"synthetic-kad-identity-{os.urandom(8).hex()}-{index}",
                    full_name=f"Synthetic KAD User {index}",
                    password_hash="synthetic-only",
                    sro_login="synthetic-kad-identity",
                    is_active=True,
                )
                db.add(user)
                db.flush()
                self.users.append(user.id)
            self.document = Document(
                filename="synthetic-kad.pdf",
                stored_path="/tmp/synthetic-kad.pdf",
                content_hash=hashlib.sha256(self.pdf_bytes).hexdigest(),
                source=IntakeSource.kad,
                uploaded_by_id=self.users[0],
            )
            db.add(self.document)
            db.flush()
            self.document_id = self.document.id

    def source(self, pdf_id: str | None = None, *, request_id: str = "12345678-1234-1234-1234-123456789abc") -> dict:
        selected_pdf = pdf_id or self.pdf_id
        url = f"https://kad.arbitr.ru/Kad/PdfDocument/{self.card_id}/{selected_pdf}/act.pdf"
        return kad_import.validate_source({
            "requestId": request_id,
            "caseNumber": "А41-12345/2026",
            "cardUrl": f"https://kad.arbitr.ru/Card/{self.card_id}",
            "candidateInventoryComplete": False,
            "selectedUrl": url,
            "candidates": [{"url": url, "label": "Synthetic act"}],
            "selectionReason": "employee_selected_visible_document",
        })

    def create_run(self, user_id: int, key: str, source: dict, *, reprocess: bool = False) -> int:
        digest = hashlib.sha256(self.pdf_bytes).hexdigest()
        with session_scope() as db:
            decision = kad_operations.begin_import(
                db,
                user_id=user_id,
                request_key=self.key_prefix + key[-16:],
                manifest_sha256=tasks.intake_manifest("kad_browser", [digest]),
                source=source,
                reprocess=reprocess,
            )
            if not decision.is_new:
                assert decision.task is not None
                return decision.task.id
            operation = decision.operation
            document = db.get(Document, self.document_id)
            item = tasks.create_intake_item(
                db,
                operation_id=operation.id,
                item_index=0,
                document=document,
                filename="kad-document.pdf",
                source=IntakeSource.kad,
                received_by_id=user_id,
            )
            task = tasks.create_task(
                db,
                document,
                user_id=user_id,
                intake_operation_id=operation.id,
                intake_item_id=item.id,
            )
            task.recognized = {kad_import.SOURCE_KEY: {
                **source,
                "sha256": digest,
                "run_generation": decision.generation,
                **({"reprocessed_from_task_id": decision.parent_task_id} if decision.parent_task_id else {}),
            }}
            kad_operations.complete_import(db, decision, task, user_id)
            return task.id

    def test_same_kad_act_replays_across_staff_but_other_act_in_same_case_does_not(self) -> None:
        first = self.create_run(self.users[0], "1" * 48, self.source())
        replay = self.create_run(
            self.users[1], "2" * 48,
            self.source(request_id="87654321-4321-4321-4321-cba987654321"),
        )
        different_act = self.create_run(
            self.users[1], "3" * 48, self.source(str(uuid.uuid4())),
        )
        self.assertEqual(first, replay)
        self.assertNotEqual(first, different_act)
        with session_scope() as db:
            identity = kad_operations.source_identity(self.source())
            self.assertEqual(db.scalar(select(func.count()).select_from(Task).join(KadSourceRun).where(
                KadSourceRun.source_identity_sha256 == identity
            )), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(KadImportRequestReceipt).where(
                KadImportRequestReceipt.user_id.in_(self.users)
            )), 3)
            self.assertEqual(db.scalar(select(func.count()).select_from(KadSourceRun).where(
                KadSourceRun.source_identity_sha256 == identity
            )), 1)

    def test_current_kad_helper_ocr_claims_shared_upload_origin_in_both_worker_modes(self) -> None:
        with session_scope() as db:
            db.get(Document, self.document_id).source = IntakeSource.upload
        task_id = self.create_run(self.users[0], "d" * 48, self.source())

        with session_scope() as db:
            self.assertIn(task_id, leases.reconcilable_ocr_task_ids(db))
            self.assertIsNotNone(leases.claim_ocr(db, task_id))
            db.rollback()
        with session_scope() as db:
            task = db.get(Task, task_id)
            self.assertTrue(tasks.claim_unleased_task(db, task))
            self.assertEqual(db.get(Document, self.document_id).source, IntakeSource.upload)

    def test_same_hash_different_selected_act_stays_separate_and_reprocess_keeps_lineage(self) -> None:
        original = self.create_run(self.users[0], "4" * 48, self.source())
        different_act_same_pdf_bytes = self.create_run(
            self.users[1], "5" * 48, self.source(str(uuid.uuid4())),
        )
        replayed = self.create_run(self.users[1], "6" * 48, self.source(), reprocess=True)
        self.assertNotEqual(original, different_act_same_pdf_bytes)
        self.assertNotEqual(original, replayed)
        with session_scope() as db:
            child = db.scalar(select(KadSourceRun).where(KadSourceRun.task_id == replayed))
            parent = db.get(KadSourceRun, child.parent_run_id)
            self.assertEqual(child.generation, 2)
            self.assertEqual(parent.task_id, original)
            child_task = db.get(Task, replayed)
            source = child_task.recognized[kad_import.SOURCE_KEY]
            self.assertEqual(source["run_generation"], 2)
            self.assertEqual(source["reprocessed_from_task_id"], original)

    def test_changed_bytes_for_same_selected_source_create_linked_version(self) -> None:
        original = self.create_run(self.users[0], "7" * 48, self.source())
        with session_scope() as db:
            decision = kad_operations.begin_import(
                db,
                user_id=self.users[1],
                request_key=self.key_prefix + "8" * 16,
                manifest_sha256=tasks.intake_manifest("kad_browser", ["f" * 64]),
                source=self.source(),
                reprocess=False,
            )
            self.assertTrue(decision.is_new)
            self.assertEqual(decision.generation, 2)
            self.assertEqual(decision.parent_task_id, original)
            self.assertFalse(decision.request_receipt.reprocess)
            parent = db.get(Task, original)
            self.assertEqual(parent.document.content_hash, hashlib.sha256(self.pdf_bytes).hexdigest())
            db.rollback()

    def test_same_selected_url_with_conflicting_case_claim_is_rejected_even_for_reprocess(self) -> None:
        self.create_run(self.users[0], "b" * 48, self.source())
        conflicting = self.source() | {"caseNumber": "А40-98765/2026"}
        conflicting = kad_import.validate_source(conflicting)
        digest = hashlib.sha256(self.pdf_bytes).hexdigest()
        with session_scope() as db:
            with self.assertRaises(kad_operations.KadSourceConflict):
                kad_operations.begin_import(
                    db,
                    user_id=self.users[1],
                    request_key=self.key_prefix + "c" * 16,
                    manifest_sha256=tasks.intake_manifest("kad_browser", [digest]),
                    source=conflicting,
                    reprocess=True,
                )
            db.rollback()

    def test_two_staff_racing_same_source_create_one_task(self) -> None:
        start = threading.Barrier(2)

        def submit(user_id: int, key: str) -> int:
            start.wait(timeout=10)
            return self.create_run(user_id, key, self.source())

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda args: submit(*args),
                [(self.users[0], "9" * 48), (self.users[1], "a" * 48)],
            ))
        self.assertEqual(results[0], results[1])
        with session_scope() as db:
            identity = kad_operations.source_identity(self.source())
            self.assertEqual(db.scalar(select(func.count()).select_from(Task).join(KadSourceRun).where(
                KadSourceRun.source_identity_sha256 == identity
            )), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(KadSourceRun).where(
                KadSourceRun.source_identity_sha256 == identity
            )), 1)
