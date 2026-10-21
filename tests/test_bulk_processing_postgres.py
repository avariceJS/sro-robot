"""Synthetic end-to-end persistence and real two-employee race boundaries."""

import os
import re
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from html import unescape
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from types import SimpleNamespace
from unittest.mock import patch

from app.db.base import SessionLocal, engine, session_scope
from app.db.models import AssignmentDraft, AssignmentDraftRevision, DebtorCardDraft, DebtorCardDraftRevision, Document, Event, IntakeItem, IntakeOperation, IntakeSource, JobOutbox, Task, TaskStatus, User
from app.services import assignments, bulk, fields, leases, tasks
from app.api.deps import current_user
from app.api import routes_assignments
from app.db.base import get_session
from app.main import app
from sro_robot.core.entity_extract import parse_entity
from sro_robot.core.extract import parse
from tests.postgres_safety import assert_disposable_postgres


def synthetic_card(inn="772881234508"):
    return parse(["ОПРЕДЕЛЕНИЕ 14 октября 2099 года\nДело № А99-12345/2099\n"
                  f"Должник: Макетова Анна Петровна, ИНН {inn}\n"
                  "Адрес регистрации: 101000, г. Примерск, ул. Тестовая, д. 1"])


@unittest.skipUnless(os.environ.get("SRO_TEST_POSTGRES") == "1", "requires disposable PostgreSQL")
class BulkProcessingPostgresTests(unittest.TestCase):
    def setUp(self):
        assert_disposable_postgres()
        self.suffix = os.urandom(6).hex()
        with session_scope() as db:
            users = [User(login=f"bulk-{self.suffix}-{n}", full_name=f"Synthetic staff {n}",
                          password_hash="synthetic-only", sro_login="synthetic", is_active=True)
                     for n in range(2)]
            db.add_all(users)
            db.flush()
            self.users = [user.id for user in users]
        # Each test uses a different checksum-valid synthetic identifier.
        digits = [int(c) for c in ("99" + str(int(self.suffix, 16) % 100000000).zfill(8))]
        digits.append(sum(v*w for v,w in zip(digits, (7,2,4,10,3,5,9,4,6,8))) % 11 % 10)
        digits.append(sum(v*w for v,w in zip(digits, (3,7,2,4,10,3,5,9,4,6,8))) % 11 % 10)
        self.inn = "".join(map(str, digits))

    def make_ogrn(self):
        prefix = f"1{int(self.suffix, 16) % 100_000_000_000:011d}"
        return prefix + str(int(prefix) % 11 % 10)

    def make_operation(self, user=0):
        with session_scope() as db:
            operation = IntakeOperation(
                user_id=self.users[user], idempotency_key=os.urandom(16).hex(),
                manifest_sha256=os.urandom(32).hex(),
            )
            db.add(operation)
            db.flush()
            return operation.id

    def make_task(self, user=0, status=TaskStatus.recognizing, operation_id=None):
        with session_scope() as db:
            document = Document(filename="synthetic-bulk.pdf", stored_path="/synthetic/bulk.pdf",
                                content_hash=os.urandom(32).hex(), source=IntakeSource.batch,
                                uploaded_by_id=self.users[user])
            db.add(document)
            db.flush()
            task = Task(document_id=document.id, scenario="person_card", profile="court_ruling",
                        status=status, recognized={}, values={}, assignee_id=self.users[user],
                        intake_operation_id=operation_id)
            db.add(task)
            db.flush()
            return task.id

    def test_intake_history_keeps_exact_operations_and_paginates(self):
        with session_scope() as db:
            baseline_operations = int(db.scalar(
                select(func.count(func.distinct(IntakeItem.operation_id))).where(
                    IntakeItem.source.in_((IntakeSource.upload, IntakeSource.batch))
                )
            ) or 0)
        with session_scope() as db:
            upload_operation_ids = []
            for operation_index in range(51):
                operation = IntakeOperation(
                    user_id=self.users[0],
                    idempotency_key=os.urandom(16).hex(),
                    manifest_sha256=os.urandom(32).hex(),
                )
                db.add(operation)
                db.flush()
                upload_operation_ids.append(operation.id)
                file_count = 2 if operation_index == 50 else 1
                for item_index in range(file_count):
                    filename = (
                        f"same-synthetic-name-{item_index}.pdf"
                        if operation_index < 50
                        else f"current-upload-{item_index}.pdf"
                    )
                    content_hash = os.urandom(32).hex()
                    document = Document(
                        filename=filename,
                        stored_path=f"/synthetic/{content_hash}.pdf",
                        content_hash=content_hash,
                        source=IntakeSource.batch,
                        uploaded_by_id=self.users[0],
                    )
                    db.add(document)
                    db.flush()
                    item = IntakeItem(
                        operation_id=operation.id,
                        item_index=item_index,
                        document_id=document.id,
                        filename=filename,
                        source=IntakeSource.batch,
                        received_by_id=self.users[0],
                    )
                    db.add(item)
                    db.flush()
                    db.add(Task(
                        document_id=document.id,
                        intake_operation_id=operation.id,
                        intake_item_id=item.id,
                        scenario="person_card",
                        profile="default",
                        status=(TaskStatus.failed if item_index else TaskStatus.needs_review),
                        recognized={},
                        values={},
                    ))

            kad_operation = IntakeOperation(
                user_id=self.users[0],
                idempotency_key=os.urandom(16).hex(),
                manifest_sha256=os.urandom(32).hex(),
            )
            db.add(kad_operation)
            db.flush()
            kad_hash = os.urandom(32).hex()
            kad_document = Document(
                filename="synthetic-kad.pdf",
                stored_path=f"/synthetic/{kad_hash}.pdf",
                content_hash=kad_hash,
                source=IntakeSource.kad,
                uploaded_by_id=self.users[0],
            )
            db.add(kad_document)
            db.flush()
            db.add(IntakeItem(
                operation_id=kad_operation.id,
                item_index=0,
                document_id=kad_document.id,
                filename=kad_document.filename,
                source=IntakeSource.kad,
                received_by_id=self.users[0],
            ))

        with session_scope() as db:
            first = bulk.intake_history_page(db, 1)
            second = bulk.intake_history_page(db, 2)
            expected_total = baseline_operations + 51
            expected_pages = (expected_total + bulk.INTAKE_HISTORY_PAGE_SIZE - 1) // bulk.INTAKE_HISTORY_PAGE_SIZE
            self.assertEqual((first["total"], first["pages"]), (expected_total, expected_pages))
            self.assertEqual(len(first["operations"]), 50)
            self.assertEqual(
                len(second["operations"]),
                min(bulk.INTAKE_HISTORY_PAGE_SIZE, expected_total - bulk.INTAKE_HISTORY_PAGE_SIZE),
            )
            self.assertEqual(
                [row["operation_id"] for row in first["operations"]],
                sorted(upload_operation_ids, reverse=True)[:50],
            )
            newest = first["operations"][0]
            self.assertEqual(newest["item_count"], 2)
            self.assertEqual(newest["task_count"], 2)
            self.assertEqual(newest["recoverable_count"], 0)
            self.assertEqual(newest["filenames"], ["current-upload-0.pdf", "current-upload-1.pdf"])
            self.assertEqual(
                newest["status_counts"],
                [{"label": "на проверке", "count": 1}, {"label": "ошибка", "count": 1}],
            )
            self.assertEqual(second["operations"][0]["operation_id"], upload_operation_ids[0])

    def test_intake_history_search_matches_literal_receipt_filenames_by_operation(self):
        suffix = os.urandom(5).hex()
        receipt_a = f"{suffix}_Receipt%_Кейс\\A.pdf"
        receipt_b = f"{suffix}_Receipt%_Кейс_B.pdf"
        with session_scope() as db:
            canonical_document = Document(
                filename=f"canonical-{suffix}.pdf",
                stored_path=f"/synthetic/canonical-{suffix}.pdf",
                content_hash=os.urandom(32).hex(),
                source=IntakeSource.upload,
                uploaded_by_id=self.users[0],
            )
            db.add(canonical_document)
            db.flush()
            operations = []
            for operation_index in range(3):
                operation = IntakeOperation(
                    user_id=self.users[operation_index % 2],
                    idempotency_key=os.urandom(16).hex(),
                    manifest_sha256=os.urandom(32).hex(),
                )
                db.add(operation)
                db.flush()
                operations.append(operation.id)
                filenames = (
                    [receipt_a, receipt_b] if operation_index == 0
                    else [receipt_a] if operation_index == 1
                    else [f"{suffix}_other-file.pdf"]
                )
                for item_index, filename in enumerate(filenames):
                    db.add(IntakeItem(
                        operation_id=operation.id,
                        item_index=item_index,
                        document_id=canonical_document.id,
                        filename=filename,
                        source=IntakeSource.upload,
                        received_by_id=self.users[operation_index % 2],
                    ))
        with session_scope() as db:
            before = (
                db.scalar(select(func.count()).select_from(Task)),
                db.scalar(select(func.count()).select_from(IntakeOperation)),
                db.scalar(select(func.count()).select_from(Event)),
            )
            history = bulk.intake_history_page(db, 1, f"{suffix}_receipt%_кейс\\")
            after = (
                db.scalar(select(func.count()).select_from(Task)),
                db.scalar(select(func.count()).select_from(IntakeOperation)),
                db.scalar(select(func.count()).select_from(Event)),
            )
        self.assertEqual(history["total"], 2)
        self.assertEqual(len(history["operations"]), 2)
        self.assertEqual(after, before)
        result_by_id = {row["operation_id"]: row for row in history["operations"]}
        self.assertEqual(result_by_id[operations[0]]["item_count"], 2)
        self.assertEqual(result_by_id[operations[0]]["matching_filename_count"], 1)
        self.assertEqual(result_by_id[operations[0]]["matching_filenames"], [receipt_a])
        self.assertEqual(result_by_id[operations[1]]["item_count"], 1)
        self.assertEqual(result_by_id[operations[1]]["matching_filename_count"], 1)
        self.assertEqual(result_by_id[operations[1]]["matching_filenames"], [receipt_a])

    def test_intake_history_search_scales_across_ten_thousand_synthetic_receipts(self):
        suffix = os.urandom(6).hex()
        needle = f"history-search-{suffix}-needle.pdf"
        with session_scope() as db:
            document = Document(
                filename=f"canonical-history-search-{suffix}.pdf",
                stored_path=f"/synthetic/history-search-{suffix}.pdf",
                content_hash=os.urandom(32).hex(),
                source=IntakeSource.batch,
                uploaded_by_id=self.users[0],
            )
            db.add(document)
            db.flush()
            operations = [
                IntakeOperation(
                    user_id=self.users[0],
                    idempotency_key=os.urandom(16).hex(),
                    manifest_sha256=os.urandom(32).hex(),
                )
                for _ in range(10_000)
            ]
            db.add_all(operations)
            db.flush()
            db.add_all([
                IntakeItem(
                    operation_id=operation.id,
                    item_index=0,
                    document_id=document.id,
                    filename=needle if index == 6_789 else f"history-search-{suffix}-{index}.pdf",
                    source=IntakeSource.batch,
                    received_by_id=self.users[0],
                )
                for index, operation in enumerate(operations)
            ])
        with session_scope() as db:
            started = time.perf_counter()
            history = bulk.intake_history_page(db, 1, needle)
            elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(history["total"], 1)
        self.assertEqual(len(history["operations"]), 1)
        self.assertEqual(history["operations"][0]["item_count"], 1)
        self.assertEqual(history["operations"][0]["matching_filenames"], [needle])
        print(
            "INTAKE_HISTORY_SEARCH_BENCHMARK "
            f"inserted_receipts=10000 matching_operations=1 "
            f"service_query_elapsed_ms={elapsed_ms:.2f} synthetic=true"
        )

    def test_upload_recovery_prepares_only_current_new_tasks_with_audit(self):
        with session_scope() as db:
            operation = IntakeOperation(
                user_id=self.users[0],
                idempotency_key=os.urandom(16).hex(),
                manifest_sha256=os.urandom(32).hex(),
            )
            db.add(operation)
            db.flush()
            operation_id = operation.id
            task_ids = []
            for index, status in enumerate((TaskStatus.new, TaskStatus.failed)):
                digest = os.urandom(32).hex()
                document = Document(
                    filename=f"recover-{index}.pdf",
                    stored_path=f"/synthetic/{digest}.pdf",
                    content_hash=digest,
                    source=IntakeSource.batch,
                    uploaded_by_id=self.users[0],
                )
                db.add(document)
                db.flush()
                item = IntakeItem(
                    operation_id=operation_id,
                    item_index=index,
                    document_id=document.id,
                    filename=document.filename,
                    source=IntakeSource.batch,
                    received_by_id=self.users[0],
                )
                db.add(item)
                db.flush()
                task = Task(
                    document_id=document.id,
                    scenario="person_card",
                    profile="default",
                    status=status,
                    recognized={},
                    values={},
                    intake_operation_id=operation_id,
                    intake_item_id=item.id,
                    assignee_id=self.users[0],
                )
                db.add(task)
                db.flush()
                task_ids.append(task.id)

        with session_scope() as db:
            first_intents = bulk.prepare_intake_recovery(
                db,
                operation_id=operation_id,
                user_id=self.users[1],
                request_id="a" * 32,
            )
            db.commit()
        with session_scope() as db:
            replay_intents = bulk.prepare_intake_recovery(
                db,
                operation_id=operation_id,
                user_id=self.users[0],
                request_id="b" * 32,
            )
            db.commit()
            self.assertEqual(first_intents, replay_intents)
            self.assertEqual(len(first_intents), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(JobOutbox).where(
                JobOutbox.task_id.in_(task_ids)
            )), 1)
            recovery_events = db.execute(
                select(Event.task_id, Event.user_id, Event.payload)
                .where(Event.kind == "intake_recovery_requested", Event.task_id.in_(task_ids))
                .order_by(Event.id)
            ).all()
            self.assertEqual([row.task_id for row in recovery_events], [task_ids[0], task_ids[0]])
            self.assertEqual([row.user_id for row in recovery_events], [self.users[1], self.users[0]])
            self.assertEqual([row.payload["request_id"] for row in recovery_events], ["a" * 32, "b" * 32])
            history = bulk.intake_history_page(db, 1)
            recovered = next(row for row in history["operations"] if row["operation_id"] == operation_id)
            self.assertEqual(recovered["recoverable_count"], 1)

        with session_scope() as db:
            kad_operation = IntakeOperation(
                user_id=self.users[0],
                idempotency_key=os.urandom(16).hex(),
                manifest_sha256=os.urandom(32).hex(),
            )
            db.add(kad_operation)
            db.flush()
            digest = os.urandom(32).hex()
            document = Document(
                filename="synthetic-kad-recovery.pdf",
                stored_path=f"/synthetic/{digest}.pdf",
                content_hash=digest,
                source=IntakeSource.kad,
                uploaded_by_id=self.users[0],
            )
            db.add(document)
            db.flush()
            item = IntakeItem(
                operation_id=kad_operation.id,
                item_index=0,
                document_id=document.id,
                filename=document.filename,
                source=IntakeSource.kad,
                received_by_id=self.users[0],
            )
            db.add(item)
            db.flush()
            kad_task = Task(
                document_id=document.id,
                scenario="person_card",
                profile="default",
                status=TaskStatus.new,
                recognized={},
                values={},
                intake_operation_id=kad_operation.id,
                intake_item_id=item.id,
                assignee_id=self.users[0],
            )
            db.add(kad_task)
            db.flush()
            with self.assertRaises(LookupError):
                bulk.prepare_intake_recovery(
                    db, operation_id=kad_operation.id, user_id=self.users[0], request_id=None
                )
            self.assertIsNone(leases.claim_ocr(db, kad_task.id))
            self.assertNotIn(kad_task.id, leases.reconcilable_ocr_task_ids(db))

    def test_upload_receipt_recovers_shared_document_without_rewriting_canonical_source(self):
        with session_scope() as db:
            operation = IntakeOperation(
                user_id=self.users[0],
                idempotency_key=os.urandom(16).hex(),
                manifest_sha256=os.urandom(32).hex(),
            )
            db.add(operation)
            db.flush()
            digest = os.urandom(32).hex()
            document = Document(
                filename="synthetic-cross-source.pdf",
                stored_path=f"/synthetic/{digest}.pdf",
                content_hash=digest,
                source=IntakeSource.kad,
                uploaded_by_id=self.users[0],
            )
            db.add(document)
            db.flush()
            item = IntakeItem(
                operation_id=operation.id,
                item_index=0,
                document_id=document.id,
                filename="synthetic-cross-source.pdf",
                source=IntakeSource.upload,
                received_by_id=self.users[0],
            )
            db.add(item)
            db.flush()
            task = Task(
                document_id=document.id,
                scenario="person_card",
                profile="default",
                status=TaskStatus.new,
                recognized={},
                values={},
                intake_operation_id=operation.id,
                intake_item_id=item.id,
                assignee_id=self.users[0],
            )
            db.add(task)
            db.flush()
            operation_id = operation.id
            task_id = task.id
            document_id = document.id

        with session_scope() as db:
            history = bulk.intake_history_page(db, 1)
            row = next(
                row for row in history["operations"] if row["operation_id"] == operation_id
            )
            self.assertEqual(row["recoverable_count"], 1)
            self.assertEqual(row["recovery_withheld_count"], 0)
            intent_ids = bulk.prepare_intake_recovery(
                db,
                operation_id=operation_id,
                user_id=self.users[1],
                request_id="c" * 32,
            )
            self.assertEqual(len(intent_ids), 1)
            db.commit()
            claim = leases.claim_ocr(db, task_id)
            self.assertIsNotNone(claim)
            self.assertEqual(db.get(Document, document_id).source, IntakeSource.kad)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(JobOutbox).where(
                    JobOutbox.task_id == task_id
                )),
                1,
            )

    def test_mixed_source_operation_is_withheld_from_recovery_and_worker_claim(self):
        with session_scope() as db:
            operation = IntakeOperation(
                user_id=self.users[0],
                idempotency_key=os.urandom(16).hex(),
                manifest_sha256=os.urandom(32).hex(),
            )
            db.add(operation)
            db.flush()
            digest = os.urandom(32).hex()
            document = Document(
                filename="synthetic-mixed-operation.pdf",
                stored_path=f"/synthetic/{digest}.pdf",
                content_hash=digest,
                source=IntakeSource.upload,
                uploaded_by_id=self.users[0],
            )
            db.add(document)
            db.flush()
            upload_item = IntakeItem(
                operation_id=operation.id,
                item_index=0,
                document_id=document.id,
                filename=document.filename,
                source=IntakeSource.upload,
                received_by_id=self.users[0],
            )
            db.add(upload_item)
            db.flush()
            task = Task(
                document_id=document.id,
                scenario="person_card",
                profile="default",
                status=TaskStatus.new,
                recognized={},
                values={},
                intake_operation_id=operation.id,
                intake_item_id=upload_item.id,
                assignee_id=self.users[0],
            )
            db.add(task)
            db.flush()
            db.add(IntakeItem(
                operation_id=operation.id,
                item_index=1,
                document_id=document.id,
                filename="synthetic-mixed-kad.pdf",
                source=IntakeSource.kad,
                received_by_id=self.users[0],
            ))
            operation_id, task_id = operation.id, task.id

        with session_scope() as db:
            row = next(
                row for row in bulk.intake_history_page(db, 1)["operations"]
                if row["operation_id"] == operation_id
            )
            self.assertEqual(row["recoverable_count"], 0)
            self.assertEqual(row["recovery_withheld_count"], 1)
            with self.assertRaises(LookupError):
                bulk.prepare_intake_recovery(
                    db,
                    operation_id=operation_id,
                    user_id=self.users[1],
                    request_id=None,
                )
            self.assertIsNone(leases.claim_ocr(db, task_id))
            self.assertNotIn(task_id, leases.reconcilable_ocr_task_ids(db))

    def recognize(self, task_id):
        with session_scope() as db:
            return tasks.save_recognized(db, db.get(Task, task_id), synthetic_card(self.inn))

    def test_intake_receipt_rows_are_append_only_in_postgres(self):
        operation_id = self.make_operation()
        with session_scope() as db:
            document = Document(
                filename="synthetic-receipt.pdf",
                stored_path=f"/synthetic/{os.urandom(16).hex()}.pdf",
                content_hash=os.urandom(32).hex(),
                source=IntakeSource.upload,
                uploaded_by_id=self.users[0],
            )
            db.add(document)
            db.flush()
            item = IntakeItem(
                operation_id=operation_id,
                item_index=0,
                document_id=document.id,
                filename=document.filename,
                source=IntakeSource.upload,
                received_by_id=self.users[0],
            )
            db.add(item)
            db.flush()
            item_id = item.id

        with session_scope() as db:
            with self.assertRaises(DBAPIError):
                db.execute(
                    text("UPDATE public.intake_item SET source = 'kad' WHERE id = :item_id"),
                    {"item_id": item_id},
                )
            db.rollback()
        with session_scope() as db:
            with self.assertRaises(DBAPIError):
                db.execute(
                    text("DELETE FROM public.intake_item WHERE id = :item_id"),
                    {"item_id": item_id},
                )
            db.rollback()
        with session_scope() as db:
            with self.assertRaises(DBAPIError):
                db.execute(text("TRUNCATE TABLE public.intake_item CASCADE"))
            db.rollback()
        with SessionLocal() as db:
            receipt = db.get(IntakeItem, item_id)
            self.assertEqual(receipt.source, IntakeSource.upload)
            self.assertEqual(receipt.filename, "synthetic-receipt.pdf")

    def test_legal_entity_draft_persists_revision_and_links_to_internal_assignment(self):
        prefix = f"{int(self.suffix, 16) % 1_000_000_000:09d}"
        check = sum(int(digit) * weight for digit, weight in zip(
            prefix, (2, 4, 10, 3, 5, 9, 4, 6, 8), strict=True
        )) % 11 % 10
        company_inn = prefix + str(check)
        with session_scope() as db:
            card = assignments.create_manual_debtor_card(db, self.users[0], "legal_entity")
            values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
            values.update(
                legal_name=f"ООО Учебное дело {self.suffix}", legal_form="ООО",
                inn=company_inn, case_number="А99-12345/2099", court="Учебный арбитражный суд",
            )
            card = assignments.update_debtor_card(db, card.id, self.users[0], card.version, values)
            assignment = assignments.create_assignment_draft(db, card.id, self.users[0])
            db.flush()
            card_id, assignment_id = card.id, assignment.id

        with session_scope() as db:
            card = db.get(DebtorCardDraft, card_id)
            revision = db.scalar(select(DebtorCardDraftRevision).where(
                DebtorCardDraftRevision.debtor_card_id == card_id,
                DebtorCardDraftRevision.revision == card.version,
            ))
            assignment = db.get(AssignmentDraft, assignment_id)
            self.assertEqual(card.entity_kind, "legal_entity")
            self.assertEqual(card.inn, company_inn)
            self.assertEqual(revision.snapshot["entity_kind"], "legal_entity")
            self.assertEqual(assignment.debtor_card_id, card_id)
            self.assertEqual(assignment.case_number, "А99-12345/2099")

    def test_company_and_ip_registration_ids_find_cards_and_assignments(self):
        company_prefix = f"{int(self.suffix, 16) % 1_000_000_000:09d}"
        company_check = sum(int(digit) * weight for digit, weight in zip(
            company_prefix, (2, 4, 10, 3, 5, 9, 4, 6, 8), strict=True
        )) % 11 % 10
        ogrnip_prefix = f"3{int(self.suffix, 16) % 10_000_000_000_000:013d}"
        ogrnip = ogrnip_prefix + str(int(ogrnip_prefix) % 13 % 10)
        ogrn = self.make_ogrn()
        kpp = f"77{int(self.suffix, 16) % 10_000_000:07d}"
        company_values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
        company_values.update(
            legal_name=f"ООО Search {self.suffix}",
            inn=company_prefix + str(company_check), ogrn=ogrn, kpp=kpp,
        )
        ip_values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["sole_proprietor"]}
        ip_values.update(debtor_name=f"ИП Search {self.suffix}", inn=self.inn, ogrnip=ogrnip)
        with session_scope() as db:
            company = assignments.create_manual_debtor_card(db, self.users[0], "legal_entity")
            company = assignments.update_debtor_card(
                db, company.id, self.users[0], company.version, company_values,
            )
            company_assignment = assignments.create_assignment_draft(db, company.id, self.users[0])
            practitioner = f"Синтетический управляющий {self.suffix}"
            practitioner_values = {name: "" for name, _label in assignments.ASSIGNMENT_FIELDS}
            practitioner_values["practitioner"] = practitioner
            company_assignment = assignments.update_assignment_draft(
                db, company_assignment.id, self.users[0], company_assignment.version,
                company_assignment.case_number, practitioner_values, "draft",
            )
            ip = assignments.create_manual_debtor_card(db, self.users[0], "sole_proprietor")
            ip = assignments.update_debtor_card(db, ip.id, self.users[0], ip.version, ip_values)
            ip_assignment = assignments.create_assignment_draft(db, ip.id, self.users[0])
            company_name, ip_name = company.display_name, ip.display_name
            self.assertEqual(company_assignment.debtor_card_id, company.id)
            self.assertEqual(ip_assignment.debtor_card_id, ip.id)

        def db_override():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_session] = db_override
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(
            id=self.users[0], login="synthetic", full_name="Synthetic staff",
        )
        try:
            with TestClient(app, base_url="http://testserver") as client:
                for identifier, expected_name in (
                    (ogrn, company_name), (ogrnip, ip_name), (kpp, company_name),
                ):
                    for path in ("/debtors", "/assignments/new", "/assignments"):
                        with self.subTest(identifier=identifier, path=path):
                            response = client.get(path, params={"q": identifier})
                            self.assertEqual(response.status_code, 200)
                            self.assertIn(expected_name, response.text)
                practitioner_response = client.get(
                    "/assignments", params={"q": practitioner},
                )
                self.assertEqual(practitioner_response.status_code, 200)
                self.assertIn(company_name, practitioner_response.text)
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(current_user, None)

    def post_review_form(self, task_id, token, overrides=None, user=1, action="approve-internal-card"):
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            form = {field.attr: field.value for field in fields.review_fields(task)}
        form.update(overrides or {})
        form["review_token"] = token

        def db_override():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_session] = db_override
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=self.users[user])
        try:
            with TestClient(app, base_url="http://testserver") as client:
                return client.post(f"/tasks/{task_id}/{action}", data=form,
                                   headers={"Origin": "http://testserver"}, follow_redirects=False)
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(current_user, None)

    def test_ocr_prepares_internal_card_without_implying_assignment_intent(self):
        operation_id = self.make_operation()
        task_id = self.make_task(operation_id=operation_id)
        self.assertTrue(self.recognize(task_id))
        self.assertFalse(self.recognize(task_id))
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            card = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id))
            assignment = db.scalar(select(AssignmentDraft).where(AssignmentDraft.source_task_id == task_id))
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertIsNone(task.approved_by_id)
            self.assertEqual(card.preparation_state, "prepared")
            self.assertEqual(card.created_by_id, self.users[0])
            self.assertEqual(card.display_name, "Макетова Анна Петровна")
            self.assertIsNone(assignment)
            batch = bulk.bulk_task_context(db, [operation_id])
            self.assertEqual(batch["counts"]["prepared"], 1)
            self.assertEqual(batch["counts"]["assignment_drafts"], 0)
            self.assertIsNone(batch["task_rows"][0]["assignment_id"])
            self.assertEqual(db.scalar(select(func.count()).select_from(JobOutbox).where(JobOutbox.task_id == task_id)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(Event).where(Event.task_id == task_id, Event.kind == "approved")), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(DebtorCardDraftRevision).where(DebtorCardDraftRevision.debtor_card_id == card.id)), 1)

    def test_entity_document_review_confirmation_and_assignment_are_one_local_workflow(self):
        prefix = f"{int(self.suffix, 16) % 1_000_000_000:09d}"
        check = sum(int(digit) * weight for digit, weight in zip(
            prefix, (2, 4, 10, 3, 5, 9, 4, 6, 8), strict=True
        )) % 11 % 10
        company_inn = prefix + str(check)
        company_ogrn = self.make_ogrn()
        source = (
            "ОПРЕДЕЛЕНИЕ\n14 октября 2099 года\nДело А99-12345/2099\nАрбитражный суд Москвы\n"
            f"Должник: ООО «Синтетическая организация {self.suffix}» ИНН {company_inn} "
            f"ОГРН {company_ogrn} КПП 770001001\n"
            "Адрес регистрации: 101000, г. Примерск, ул. Тестовая, д. 1\n"
            "Заявитель: ИП Иванов Иван Иванович"
        )
        task_id = self.make_task()
        with session_scope() as db:
            task = db.get(Task, task_id)
            self.assertTrue(tasks.save_recognized(db, task, parse_entity([source, source])))
            self.assertEqual(task.scenario, "entity_review")
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertIsNone(db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id)))

        with SessionLocal() as db:
            task = db.get(Task, task_id)
            token = tasks.review_token(task)
            recognized = fields.review_fields(task)
            values = {field.attr: field.value for field in recognized}
            values["legal_name"] = f"ООО «Исправленное название {self.suffix}»"

        def db_override():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_session] = db_override
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=self.users[1])
        try:
            with TestClient(app, base_url="http://testserver") as client:
                page_response = client.get(f"/tasks/{task_id}")
                self.assertEqual(page_response.status_code, 200)
                self.assertIn("Наименование организации", page_response.text)
                self.assertIn("Источник: вариант OCR 1", page_response.text)
                self.assertIn("14 октября 2099 года", page_response.text)
                self.assertIn("А99-12345/2099", page_response.text)
                self.assertIn("Арбитражный суд Москвы", page_response.text)
                self.assertIn(company_inn, page_response.text)
                self.assertIn("101000, г. Примерск, ул. Тестовая, д. 1", page_response.text)
                self.assertIn("Адрес регистрации", page_response.text)
                self.assertNotIn("Подготовить настоящую карточку СРО без сохранения", page_response.text)
                values["review_token"] = token
                confirmed = client.post(
                    f"/tasks/{task_id}/approve-internal-card", data=values,
                    headers={"Origin": "http://testserver"}, follow_redirects=False,
                )
                self.assertEqual(confirmed.status_code, 303)
                debtor_id = int(confirmed.headers["location"].split("/debtors/")[1].split("?")[0])
                assignment_response = client.post(
                    f"/debtors/{debtor_id}/assignments", data={"request_key": os.urandom(32).hex()},
                    headers={"Origin": "http://testserver"}, follow_redirects=False,
                )
                self.assertEqual(assignment_response.status_code, 303)
                assignment_id = int(assignment_response.headers["location"].split("/assignments/")[1].split("?")[0])
                assignment_page = client.get(f"/assignments/{assignment_id}")
                self.assertEqual(assignment_page.status_code, 200)
                self.assertIn("Дата прекращения процедуры", assignment_page.text)
                self.assertIn("Вознаграждение АУ", assignment_page.text)
                with SessionLocal() as db:
                    draft = db.get(AssignmentDraft, assignment_id)
                    reviewed_card = db.get(DebtorCardDraft, debtor_id)
                    self.assertEqual(reviewed_card.values["address_kind"]["value"], "registration")
                    edit_values = {
                        name: str(draft.values.get(name) or "")
                        for name, _label in assignments.ASSIGNMENT_FIELDS
                    }
                    assignment_version = draft.version
                    assignment_case = draft.case_number
                    assignment_status = draft.status
                edit_values.update(
                    case_number=assignment_case,
                    status=assignment_status,
                    creditors="Синтетический кредитор",
                    procedure_termination_date="2099-12-31",
                    version=str(assignment_version),
                )
                saved_assignment = client.post(
                    f"/assignments/{assignment_id}", data=edit_values,
                    headers={"Origin": "http://testserver"}, follow_redirects=False,
                )
                self.assertEqual(saved_assignment.status_code, 303)
                resumed_page = client.get(f"/assignments/{assignment_id}")
                self.assertEqual(resumed_page.status_code, 200)
                self.assertIn("Синтетический кредитор", resumed_page.text)
                self.assertIn("2099-12-31", resumed_page.text)
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(current_user, None)

        with SessionLocal() as db:
            task = db.get(Task, task_id)
            debtor = db.get(DebtorCardDraft, debtor_id)
            assignment = db.get(AssignmentDraft, assignment_id)
            self.assertEqual(task.status, TaskStatus.approved)
            self.assertEqual(task.approved_by_id, self.users[1])
            self.assertEqual(debtor.entity_kind, "legal_entity")
            self.assertEqual(debtor.display_name, f"ООО «Исправленное название {self.suffix}»")
            self.assertEqual(debtor.inn, company_inn)
            self.assertEqual(debtor.preparation_state, "reviewed")
            self.assertEqual(assignment.debtor_card_id, debtor_id)
            self.assertEqual(assignment.case_number, "А99-12345/2099")
            self.assertEqual(assignment.values["arbitration_court"], "Арбитражный суд Москвы")
            self.assertEqual(assignment.values["creditors"], "Синтетический кредитор")
            self.assertEqual(assignment.values["procedure_termination_date"], "2099-12-31")

        duplicate_id = self.make_task()
        with session_scope() as db:
            duplicate_task = db.get(Task, duplicate_id)
            self.assertTrue(tasks.save_recognized(db, duplicate_task, parse_entity([source, source])))
        with SessionLocal() as db:
            duplicate_task = db.get(Task, duplicate_id)
            duplicate_token = tasks.review_token(duplicate_task)
            duplicate_values = {field.attr: field.value for field in fields.review_fields(duplicate_task)}
        duplicate_values["review_token"] = duplicate_token
        app.dependency_overrides[get_session] = db_override
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=self.users[0])
        try:
            with TestClient(app, base_url="http://testserver") as client:
                duplicate_response = client.post(
                    f"/tasks/{duplicate_id}/approve-internal-card", data=duplicate_values,
                    headers={"Origin": "http://testserver"}, follow_redirects=False,
                )
                self.assertEqual(duplicate_response.status_code, 303)
                self.assertIn("internal_card=duplicate", duplicate_response.headers["location"])
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(current_user, None)
        with SessionLocal() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(DebtorCardDraft).where(DebtorCardDraft.inn == company_inn)),
                1,
            )

    def test_person_review_page_shows_role_bound_source_excerpts(self):
        source = (
            "ОПРЕДЕЛЕНИЕ 14 октября 2099 года. Дело № А99-12345/2099.\n"
            "Заявитель: Петров Пётр Иванович, ИНН 500100001146\n"
            f"Должник: Макетова Анна Петровна, ИНН {self.inn}, СНИЛС 112-233-445 95\n\n"
            "Адрес регистрации: 101000, г. Примерск,\nулица Тестовая, дом 7"
        )
        task_id = self.make_task()
        with session_scope() as db:
            self.assertTrue(tasks.save_recognized(db, db.get(Task, task_id), parse([source])))

        def db_override():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_session] = db_override
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=self.users[0])
        try:
            with TestClient(app, base_url="http://testserver") as client:
                response = client.get(f"/tasks/{task_id}")
                self.assertEqual(response.status_code, 200)
                self.assertIn("Источник: вариант OCR 1", response.text)
                self.assertIn("Макетова Анна Петровна", response.text)
                self.assertIn(self.inn, response.text)
                self.assertIn("112-233-445 95", response.text)
                self.assertIn("101000, г. Примерск,", response.text)
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(current_user, None)

    def test_mixed_person_company_and_ip_ocr_can_finish_in_parallel_without_cross_type_cards(self):
        prefix = f"{int(self.suffix, 16) % 1_000_000_000:09d}"
        company_check = sum(int(digit) * weight for digit, weight in zip(
            prefix, (2, 4, 10, 3, 5, 9, 4, 6, 8), strict=True
        )) % 11 % 10
        company_inn = prefix + str(company_check)
        company_ogrn = self.make_ogrn()
        ip_base = (prefix + "12")[:10]
        ip_tenth = sum(int(digit) * weight for digit, weight in zip(
            ip_base, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8), strict=True
        )) % 11 % 10
        ip_first_eleven = ip_base + str(ip_tenth)
        ip_last = sum(int(digit) * weight for digit, weight in zip(
            ip_first_eleven, (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8), strict=True
        )) % 11 % 10
        ip_inn = ip_first_eleven + str(ip_last)
        operation_id = self.make_operation()
        task_ids = [self.make_task(operation_id=operation_id) for _ in range(3)]

        company_text = (
            f"Определение. Дело А99-12345/2099. Должник: ООО «Компания {self.suffix}» "
            f"ИНН {company_inn} ОГРН {company_ogrn} КПП 770001001"
        )
        ip_text = (
            f"Определение. Дело А99-23456/2099. Должник: ИП Иванов Иван Иванович "
            f"ИНН {ip_inn} ОГРНИП 319500100000001"
        )
        cards = {
            task_ids[0]: synthetic_card(self.inn),
            task_ids[1]: parse_entity([company_text, company_text]),
            task_ids[2]: parse_entity([ip_text, ip_text]),
        }

        def finish_recognition(item):
            task_id, card = item
            with session_scope() as db:
                task = db.get(Task, task_id)
                return tasks.save_recognized(db, task, card)

        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(list(pool.map(finish_recognition, cards.items())), [True, True, True])

        with SessionLocal() as db:
            reviewed = {task.id: task for task in db.scalars(select(Task).where(Task.id.in_(task_ids)))}
            self.assertEqual(reviewed[task_ids[0]].scenario, "person_card")
            self.assertEqual(reviewed[task_ids[1]].scenario, "entity_review")
            self.assertEqual(reviewed[task_ids[2]].scenario, "entity_review")
            self.assertTrue(all(task.status == TaskStatus.needs_review for task in reviewed.values()))
            prepared = list(db.scalars(select(DebtorCardDraft).where(
                DebtorCardDraft.source_task_id.in_(task_ids)
            )))
            self.assertEqual(len(prepared), 1)
            self.assertEqual(prepared[0].source_task_id, task_ids[0])
            self.assertEqual(prepared[0].entity_kind, "person")
            self.assertIsNone(db.scalar(select(AssignmentDraft).where(
                AssignmentDraft.source_task_id.in_(task_ids)
            )))
            batch = bulk.bulk_task_context(db, [operation_id])
            self.assertEqual(batch["counts"]["prepared"], 1)
            self.assertEqual(batch["counts"]["needs_review"], 2)

    def test_ip_card_can_link_only_an_explicitly_selected_reviewed_person_card(self):
        with session_scope() as db:
            person = assignments.create_manual_debtor_card(db, self.users[0], "person")
            person_values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["person"]}
            person_values.update(last_name="Синтетический", first_name="Предприниматель", inn=self.inn)
            person = assignments.update_debtor_card(db, person.id, self.users[0], person.version, person_values)
            ip_card = assignments.create_manual_debtor_card(db, self.users[0], "sole_proprietor")
            ip_values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["sole_proprietor"]}
            ip_values.update(debtor_name="Синтетический Предприниматель", inn=self.inn)
            ip_card = assignments.update_debtor_card(db, ip_card.id, self.users[0], ip_card.version, ip_values)
            company = assignments.create_manual_debtor_card(db, self.users[0], "legal_entity")
            company_values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
            company_values.update(legal_name="ООО Синтетическая организация")
            company = assignments.update_debtor_card(db, company.id, self.users[0], company.version, company_values)
            person_id, ip_id, company_id = person.id, ip_card.id, company.id
            ip_version = ip_card.version

        def db_override():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_session] = db_override
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=self.users[1])
        try:
            with TestClient(app, base_url="http://testserver") as client:
                page = client.post(
                    f"/debtors/{ip_id}/related-person/search",
                    data={"person_q": "Синтетический"},
                    headers={"Origin": "http://testserver"},
                )
                self.assertEqual(page.status_code, 200)
                self.assertIn("no-store", page.headers["cache-control"])
                self.assertNotIn("Синтетический", str(page.url))
                self.assertIn("Связь с физическим лицом", page.text)
                self.assertIn(f'value="{person_id}"', page.text)
                response = client.post(
                    f"/debtors/{ip_id}/related-person",
                    data={"version": str(ip_version), "person_card_id": str(person_id)},
                    headers={"Origin": "http://testserver"}, follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                replay = client.post(
                    f"/debtors/{ip_id}/related-person",
                    data={"version": str(ip_version), "person_card_id": str(person_id)},
                    headers={"Origin": "http://testserver"}, follow_redirects=False,
                )
                self.assertEqual(replay.status_code, 303)
                with SessionLocal() as db:
                    current_ip = db.get(DebtorCardDraft, ip_id)
                    current_version = current_ip.version
                    revision_count = db.scalar(
                        select(func.count()).select_from(DebtorCardDraftRevision).where(
                            DebtorCardDraftRevision.debtor_card_id == ip_id
                        )
                    )
                self.assertEqual(current_version, ip_version + 1)
                self.assertEqual(revision_count, ip_version + 1)
                rejected = client.post(
                    f"/debtors/{ip_id}/related-person",
                    data={"version": str(current_version), "person_card_id": str(company_id)},
                    headers={"Origin": "http://testserver"}, follow_redirects=False,
                )
                self.assertEqual(rejected.status_code, 409)
                removed = client.post(
                    f"/debtors/{ip_id}/related-person",
                    data={"version": str(current_version), "person_card_id": ""},
                    headers={"Origin": "http://testserver"}, follow_redirects=False,
                )
                self.assertEqual(removed.status_code, 303)
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(current_user, None)

        with SessionLocal() as db:
            ip_card = db.get(DebtorCardDraft, ip_id)
            self.assertNotIn("related_person_card_id", ip_card.values)
            relation_revision = db.scalar(select(DebtorCardDraftRevision).where(
                DebtorCardDraftRevision.debtor_card_id == ip_id,
                DebtorCardDraftRevision.revision == ip_version + 1,
            ))
            self.assertEqual(relation_revision.changed_by_id, self.users[1])
            self.assertEqual(relation_revision.snapshot["values"]["related_person_card_id"]["value"], str(person_id))
            removal_revision = db.scalar(select(DebtorCardDraftRevision).where(
                DebtorCardDraftRevision.debtor_card_id == ip_id,
                DebtorCardDraftRevision.revision == ip_version + 2,
            ))
            self.assertNotIn("related_person_card_id", removal_revision.snapshot["values"])
            self.assertEqual(removal_revision.changed_by_id, self.users[1])
            self.assertEqual(ip_card.version, ip_version + 2)

    def test_ip_person_link_waits_for_concurrent_person_review_state_change(self):
        with session_scope() as db:
            person = assignments.create_manual_debtor_card(db, self.users[0], "person")
            person_values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["person"]}
            person_values.update(
                last_name="Синтетический", first_name="Связанный", inn=self.inn,
            )
            person = assignments.update_debtor_card(
                db, person.id, self.users[0], person.version, person_values, confirm=True,
            )
            ip_card = assignments.create_manual_debtor_card(db, self.users[0], "sole_proprietor")
            ip_values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["sole_proprietor"]}
            ip_values.update(debtor_name="Синтетический Связанный", inn=self.inn)
            ip_card = assignments.update_debtor_card(
                db, ip_card.id, self.users[0], ip_card.version, ip_values, confirm=True,
            )
            person_id, ip_id, ip_version = person.id, ip_card.id, ip_card.version

        started = threading.Event()
        worker_pid: dict[str, int] = {}

        def link_person():
            with SessionLocal() as db:
                worker_pid["value"] = db.scalar(text("SELECT pg_backend_pid()"))
                started.set()
                try:
                    assignments.set_sole_proprietor_person_link(
                        db, ip_id, self.users[1], ip_version, person_id,
                    )
                    db.commit()
                    return "linked"
                except assignments.DraftNotReady:
                    db.rollback()
                    return "rejected_unreviewed"

        with ThreadPoolExecutor(max_workers=1) as pool, SessionLocal() as editor:
            person = editor.get(DebtorCardDraft, person_id)
            draft_values = {
                name: str((person.values.get(name) or {}).get("value") or "")
                for name in assignments.ENTITY_CARD_FIELDS["person"]
            }
            assignments.update_debtor_card(
                editor, person_id, self.users[0], person.version, draft_values, confirm=False,
            )
            future = pool.submit(link_person)
            self.assertTrue(started.wait(timeout=10))

            deadline = time.monotonic() + 10
            blocked = False
            while time.monotonic() < deadline:
                with engine.connect() as observer:
                    blocked = bool(observer.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                        {"pid": worker_pid["value"]},
                    ))
                if blocked:
                    break
                time.sleep(0.02)
            self.assertTrue(blocked, "person-link selection did not wait for the in-flight card edit")
            editor.commit()
            self.assertEqual(future.result(timeout=10), "rejected_unreviewed")

        with SessionLocal() as db:
            person = db.get(DebtorCardDraft, person_id)
            ip_card = db.get(DebtorCardDraft, ip_id)
            self.assertEqual(person.preparation_state, "draft")
            self.assertNotIn("related_person_card_id", ip_card.values)

    def test_source_assignment_rechecks_approval_after_waiting_for_task_lock(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with session_scope() as db:
            task = db.get(Task, task_id)
            task_values = {field.attr: field.value for field in fields.review_fields(task)}
            task_values.update(case_number="А99-РЕЙС/2099", court="Синтетический арбитражный суд")
            tasks.save_values(db, task, task_values, self.users[0])
            tasks.approve(db, task, self.users[0])
            card = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id))
            card_values = {
                name: str((card.values.get(name) or {}).get("value") or "")
                for name in assignments.ENTITY_CARD_FIELDS["person"]
            }
            card_values.update(last_name="Синтетический", first_name="Должник")
            assignments.update_debtor_card(
                db, card.id, self.users[0], card.version, card_values, confirm=True,
            )
            card_id = card.id
            source_case = task.values["case_number"]
            source_court = task.values["court"]

        checked = threading.Event()
        begin_create = threading.Event()
        worker_pid: dict[str, int] = {}

        def create_from_stale_approval():
            with SessionLocal() as db:
                worker_pid["value"] = db.scalar(text("SELECT pg_backend_pid()"))
                task = db.get(Task, task_id)
                if task.status != TaskStatus.approved or not tasks.approval_matches(db, task):
                    raise AssertionError("Source task was not approved before the concurrent rejection")
                checked.set()
                if not begin_create.wait(timeout=10):
                    raise TimeoutError("Source-assignment race was not released")
                try:
                    assignment = assignments.create_assignment_draft(
                        db, card_id, self.users[1], source_task_id=task_id,
                        source_case_number=source_case, source_court=source_court,
                    )
                    db.commit()
                    return ("created", assignment.id)
                except assignments.DraftNotReady:
                    db.rollback()
                    return ("rejected", None)

        with ThreadPoolExecutor(max_workers=1) as pool, SessionLocal() as rejecting_employee:
            future = pool.submit(create_from_stale_approval)
            self.assertTrue(checked.wait(timeout=10))
            task = rejecting_employee.scalar(
                select(Task).where(Task.id == task_id)
                .with_for_update().execution_options(populate_existing=True)
            )
            tasks.reject(rejecting_employee, task, self.users[0])
            begin_create.set()

            deadline = time.monotonic() + 10
            blocked = False
            while time.monotonic() < deadline:
                with engine.connect() as observer:
                    blocked = bool(observer.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                        {"pid": worker_pid["value"]},
                    ))
                if blocked:
                    break
                time.sleep(0.02)
            self.assertTrue(blocked, "assignment did not wait for the in-flight source decision")
            rejecting_employee.commit()
            self.assertEqual(future.result(timeout=10), ("rejected", None))

        with SessionLocal() as db:
            task = db.get(Task, task_id)
            assignment = db.scalar(
                select(AssignmentDraft).where(AssignmentDraft.source_task_id == task_id)
            )
            self.assertEqual(task.status, TaskStatus.rejected)
            self.assertIsNone(assignment)

    def test_assignment_request_concurrency_replay_and_fresh_intent(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with session_scope() as db:
            card = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id))
            values = {
                name: str((card.values.get(name) or {}).get("value") or "")
                for name in assignments.DEBTOR_CARD_FIELDS
            }
            values.update(last_name="Синтетический", first_name="Должник")
            assignments.update_debtor_card(db, card.id, self.users[0], card.version, values, confirm=True)
            card_id = card.id

        request_key = os.urandom(32).hex()
        barrier = threading.Barrier(2)

        def create_same_request(_index):
            with session_scope() as db:
                barrier.wait(timeout=10)
                return assignments.create_assignment_draft_once(
                    db, card_id, self.users[0], request_key,
                ).id

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create_same_request, range(2)))
        self.assertEqual(results[0], results[1])

        # Repeating the request after an unknown response returns the committed object.
        with session_scope() as db:
            replay = assignments.create_assignment_draft_once(db, card_id, self.users[0], request_key)
            count = db.scalar(select(func.count()).select_from(AssignmentDraft).where(
                AssignmentDraft.debtor_card_id == card_id
            ))
        self.assertEqual(replay.id, results[0])
        self.assertEqual(count, 1)

        # A newly opened action has a new key, so same debtor/case remains valid.
        with session_scope() as db:
            independent = assignments.create_assignment_draft_once(
                db, card_id, self.users[0], os.urandom(32).hex(),
            )
        self.assertNotEqual(independent.id, results[0])
        self.assertEqual(independent.case_number, replay.case_number)

    def test_manual_card_creation_request_replays_safely_under_concurrency(self):
        request_key = os.urandom(32).hex()
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["person"]}
        values["last_name"] = "Синтетический"
        barrier = threading.Barrier(2)

        def create_same_request(_index):
            with session_scope() as db:
                barrier.wait(timeout=10)
                card = assignments.create_manual_debtor_card_once(
                    db, self.users[0], "person", values, False, request_key,
                )
                return card.id

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create_same_request, range(2)))
        self.assertEqual(results[0], results[1])

        with session_scope() as db:
            count = db.scalar(select(func.count()).select_from(DebtorCardDraft).where(
                DebtorCardDraft.display_name == "Синтетический"
            ))
            replay = assignments.create_manual_debtor_card_once(
                db, self.users[0], "person", values, False, request_key,
            )
        self.assertEqual(count, 1)
        self.assertEqual(replay.id, results[0])

        changed = dict(values, last_name="Другое имя")
        with session_scope() as db:
            with self.assertRaises(assignments.DebtorCardRequestConflict):
                assignments.create_manual_debtor_card_once(
                    db, self.users[0], "person", changed, False, request_key,
                )

        with session_scope() as db:
            independent = assignments.create_manual_debtor_card_once(
                db, self.users[0], "person", values, False, os.urandom(32).hex(),
            )
        self.assertNotEqual(independent.id, results[0])

    def test_manual_card_draft_resumes_confirms_and_imports_into_assignment(self):
        def db_override():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_session] = db_override
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=self.users[0])
        try:
            with TestClient(app, base_url="http://testserver") as client:
                opened = client.get("/debtors/new")
                self.assertEqual(opened.status_code, 200)
                self.assertIn("Сохранить черновик", opened.text)
                self.assertIn("Подтвердить данные", opened.text)
                request_key = re.search(r'name="request_key" value="([0-9a-f]{64})"', opened.text).group(1)

                create_form = {
                    "action": "draft", "first_name": "Анна",
                    "case_number": "А99-4242/2099", "court": "Синтетический суд",
                    "request_key": request_key,
                }
                saved = client.post("/debtors/new", data=create_form,
                                    headers={"Origin": "http://testserver"}, follow_redirects=False)
                self.assertEqual(saved.status_code, 303)
                card_id = int(saved.headers["location"].split("/debtors/")[1].split("?")[0])
                replayed = client.post("/debtors/new", data=create_form,
                                       headers={"Origin": "http://testserver"}, follow_redirects=False)
                self.assertEqual(replayed.status_code, 303)
                self.assertEqual(replayed.headers["location"], saved.headers["location"])
                with session_scope() as db:
                    card = db.get(DebtorCardDraft, card_id)
                    self.assertEqual(card.preparation_state, "draft")
                    self.assertIsNone(card.source_task_id)
                    self.assertEqual(card.display_name, "Анна")
                    self.assertEqual(db.scalar(select(func.count()).select_from(DebtorCardDraftRevision)
                                               .where(DebtorCardDraftRevision.debtor_card_id == card_id)), 2)
                    form_values = {name: str((card.values.get(name) or {}).get("value") or "")
                                   for name in assignments.DEBTOR_CARD_FIELDS}
                    version = card.version
                form_values.update(version=version, action="confirm", last_name="Макетова")
                confirmed = client.post(f"/debtors/{card_id}", data=form_values,
                                        headers={"Origin": "http://testserver"}, follow_redirects=False)
                self.assertEqual(confirmed.status_code, 303)
                with session_scope() as db:
                    card = db.get(DebtorCardDraft, card_id)
                    self.assertEqual(card.preparation_state, "reviewed")
                    self.assertEqual(card.display_name, "Макетова Анна")
                    self.assertIsNone(card.source_task_id)

                imported = client.post("/assignments/new", data={"debtor_id": card_id, "request_key": os.urandom(32).hex()},
                                       headers={"Origin": "http://testserver"}, follow_redirects=False)
                self.assertEqual(imported.status_code, 303)
                assignment_id = int(imported.headers["location"].split("/assignments/")[1].split("?")[0])
                with session_scope() as db:
                    assignment = db.get(AssignmentDraft, assignment_id)
                    self.assertEqual(assignment.debtor_card_id, card_id)
                    self.assertEqual(assignment.case_number, "А99-4242/2099")
                    self.assertEqual(assignment.values["arbitration_court"], "Синтетический суд")
                    self.assertEqual(assignment.values["practitioner"], "")
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(current_user, None)

    def test_concurrent_employees_do_not_create_two_cards_for_one_inn(self):
        ids = [self.make_task(n) for n in range(2)]
        barrier = threading.Barrier(2)
        def complete(task_id):
            barrier.wait(timeout=10)
            return self.recognize(task_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(complete, ids)), [True, True])
        with SessionLocal() as db:
            cards = db.scalars(select(DebtorCardDraft).where(DebtorCardDraft.inn == self.inn)).all()
            self.assertEqual(len(cards), 1)
            sources = db.scalars(select(Task).where(Task.id.in_(ids))).all()
            states = [(task.recognized[assignments.PREPARATION_KEY]) for task in sources]
            self.assertEqual(sorted(state["state"] for state in states), ["needs_review", "prepared"])
            duplicate = next(state for state in states if state["state"] == "needs_review")
            self.assertEqual(duplicate["reason"], "possible_duplicate")
            self.assertEqual(duplicate["possible_card_id"], cards[0].id)

    def test_unicode_decimal_inn_is_canonicalized_before_duplicate_lookup(self):
        ascii_inn = self.inn
        localized_inn = ascii_inn.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
        with session_scope() as db:
            existing = assignments.create_manual_debtor_card(db, self.users[0], "person")
            existing_values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["person"]}
            existing_values.update(last_name="Синтетический", first_name="Должник", inn=ascii_inn)
            assignments.update_debtor_card(db, existing.id, self.users[0], existing.version, existing_values)
            candidate = assignments.create_manual_debtor_card(db, self.users[1], "person")
            candidate_id = candidate.id

        candidate_values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["person"]}
        candidate_values.update(last_name="Другой", first_name="Должник", inn=localized_inn)
        with self.assertRaises(assignments.DuplicateDraft):
            with session_scope() as db:
                candidate = db.get(DebtorCardDraft, candidate_id)
                assignments.update_debtor_card(
                    db, candidate.id, self.users[1], candidate.version, candidate_values,
                )

        with SessionLocal() as db:
            current = db.get(DebtorCardDraft, candidate_id)
            self.assertIsNone(current.inn)
            self.assertEqual(current.version, 1)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(DebtorCardDraft)
                          .where(DebtorCardDraft.inn == ascii_inn)),
                1,
            )

    def test_concurrent_ascii_and_unicode_inn_edits_share_duplicate_lock(self):
        ascii_inn = "772881234508"
        localized_inn = ascii_inn.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
        with session_scope() as db:
            card_ids = [
                assignments.create_manual_debtor_card(db, user_id, "person").id
                for user_id in self.users
            ]
        barrier = threading.Barrier(2)

        def confirm(card_id, user_id, inn):
            values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["person"]}
            values.update(last_name=f"Синтетический{user_id}", first_name="Должник", inn=inn)
            with session_scope() as db:
                card = db.get(DebtorCardDraft, card_id)
                barrier.wait(timeout=10)
                try:
                    assignments.update_debtor_card(db, card_id, user_id, card.version, values)
                    return "saved"
                except assignments.DuplicateDraft:
                    return "duplicate"

        inputs = [
            (card_ids[0], self.users[0], ascii_inn),
            (card_ids[1], self.users[1], localized_inn),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda args: confirm(*args), inputs)), ["duplicate", "saved"])

        with SessionLocal() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(DebtorCardDraft)
                          .where(DebtorCardDraft.inn == ascii_inn)),
                1,
            )
            cards = db.scalars(select(DebtorCardDraft).where(DebtorCardDraft.id.in_(card_ids))).all()
            self.assertEqual(sorted(card.preparation_state for card in cards), ["draft", "reviewed"])
            saved = next(card for card in cards if card.inn is not None)
            self.assertEqual(saved.inn, ascii_inn)
            self.assertEqual(saved.values["inn"]["value"], ascii_inn)

    def test_concurrent_company_cards_with_same_ogrn_are_rejected(self):
        ogrn = self.make_ogrn()
        with session_scope() as db:
            card_ids = [
                assignments.create_manual_debtor_card(db, user_id, "legal_entity").id
                for user_id in self.users[:2]
            ]
        barrier = threading.Barrier(2)

        def confirm(card_id, user_id):
            values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
            values.update(legal_name=f"ООО Синтетика {user_id}", ogrn=ogrn)
            with session_scope() as db:
                card = db.get(DebtorCardDraft, card_id)
                barrier.wait(timeout=10)
                try:
                    assignments.update_debtor_card(db, card_id, user_id, card.version, values)
                    return "saved"
                except assignments.DuplicateDraft:
                    return "duplicate"

        inputs = list(zip(card_ids, self.users[:2], strict=True))
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda args: confirm(*args), inputs)), ["duplicate", "saved"])

        with SessionLocal() as db:
            count = db.scalar(select(func.count()).select_from(DebtorCardDraft).where(
                DebtorCardDraft.entity_kind == "legal_entity",
                DebtorCardDraft.values["ogrn"]["value"].astext == ogrn,
            ))
            self.assertEqual(count, 1)
            index_names = set(db.execute(text(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
                "AND tablename='debtor_card_draft'"
            )).scalars())
            self.assertIn("uq_debtor_card_draft_legal_entity_ogrn", index_names)
            self.assertIn("uq_debtor_card_draft_sole_proprietor_ogrnip", index_names)

    def test_multiple_assignments_per_debtor_and_parallel_edit_history(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with session_scope() as db:
            card = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id))
            automatic = db.scalar(select(AssignmentDraft).where(AssignmentDraft.source_task_id == task_id))
            self.assertIsNone(automatic)
            confirmed = {
                name: str((card.values.get(name) or {}).get("value") or "")
                for name in assignments.DEBTOR_CARD_FIELDS
            }
            assignments.update_debtor_card(db, card.id, self.users[0], card.version, confirmed)
            first = assignments.create_assignment_draft(db, card.id, self.users[0])
            second = assignments.create_assignment_draft(db, card.id, self.users[1])
            self.assertEqual(first.case_number, second.case_number)
            self.assertEqual(first.debtor_card_id, second.debtor_card_id)
            self.assertNotEqual(first.id, second.id)
            first_id = first.id

        barrier = threading.Barrier(2)
        def edit(user_id):
            with session_scope() as db:
                barrier.wait(timeout=10)
                try:
                    assignments.update_assignment_draft(
                        db, first_id, user_id, 1, "А99-12345/2099",
                        {"practitioner": f"Synthetic practitioner {user_id}"}, "awaiting_information",
                    )
                    return "saved"
                except assignments.DraftConflict:
                    return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(edit, self.users)), ["saved", "stale"])
        linked_task_id = self.make_task()
        with session_scope() as db:
            assignment = db.get(AssignmentDraft, first_id)
            assignment.source_task_id = linked_task_id
            assignment.updated_by_id = self.users[0]
            assignment.version += 1
        with SessionLocal() as db:
            saved = db.get(AssignmentDraft, first_id)
            history = db.scalars(
                select(AssignmentDraftRevision)
                .where(AssignmentDraftRevision.assignment_draft_id == first_id)
                .order_by(AssignmentDraftRevision.revision)
            ).all()
            self.assertEqual(saved.version, 3)
            self.assertEqual([item.revision for item in history], [1, 2, 3])
            self.assertIn(history[-1].changed_by_id, self.users)
            self.assertEqual(history[-1].snapshot["source_task_id"], linked_task_id)

    def test_two_employee_http_conflicts_preserve_and_allow_selective_card_and_assignment_edits(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with session_scope() as db:
            card = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id))
            values = {
                name: str((card.values.get(name) or {}).get("value") or "")
                for name in assignments.DEBTOR_CARD_FIELDS
            }
            values.update(last_name="Синтетический")
            assignments.update_debtor_card(db, card.id, self.users[0], card.version, values, confirm=True)
            card_id = card.id
            draft = assignments.create_assignment_draft(db, card.id, self.users[0])
            assignment_id = draft.id

        test_app = FastAPI()
        test_app.include_router(__import__("app.api.routes_assignments", fromlist=["router"]).router)
        test_app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
        test_app.add_api_route("/", lambda: {}, name="tasks_page")
        test_app.add_api_route("/debtors/intake", lambda: {}, name="debtor_intake_page")
        test_app.add_api_route("/logout", lambda: {}, name="logout")
        test_app.add_api_route("/tasks/{task_id}", lambda task_id: {}, name="task_page")
        test_app.add_api_route("/tasks/{task_id}/document", lambda task_id: {}, name="task_document")
        test_app.add_api_route("/tasks/{task_id}/document/preview", lambda task_id: {}, name="task_document_preview")

        def identify_employee(request: Request):
            employee_id = int(request.headers["x-synthetic-employee"])
            return SimpleNamespace(id=employee_id, login=f"synthetic-{employee_id}", full_name=f"Сотрудник {employee_id}")

        def db_override():
            with SessionLocal() as db:
                yield db

        test_app.dependency_overrides[current_user] = identify_employee
        test_app.dependency_overrides[get_session] = db_override
        clients = [TestClient(test_app, headers={"x-synthetic-employee": str(user_id)}) for user_id in self.users]
        try:
            with SessionLocal() as db:
                card = db.get(DebtorCardDraft, card_id)
                current_card_values = {
                    name: str((card.values.get(name) or {}).get("value") or "")
                    for name in assignments.DEBTOR_CARD_FIELDS
                }
                card_version = card.version
            for client in clients:
                page = client.get(f"/debtors/{card_id}")
                self.assertEqual(page.status_code, 200)
                self.assertIn(f'name="version" value="{card_version}"', page.text)

            card_edits = [dict(current_card_values, last_name=f"Сотрудник-{n}", version=str(card_version), action="draft")
                          for n in (1, 2)]
            with ThreadPoolExecutor(max_workers=2) as pool:
                card_responses = list(pool.map(
                    lambda pair: pair[0].post(f"/debtors/{card_id}", data=pair[1], follow_redirects=False),
                    zip(clients, card_edits),
                ))
            self.assertEqual(sorted(response.status_code for response in card_responses), [200, 303])
            conflict_index = next(index for index, response in enumerate(card_responses) if response.status_code == 200)
            card_conflict = card_responses[conflict_index]
            self.assertEqual(card_conflict.headers["cache-control"], "private, no-store")
            self.assertIn("Сохранено сейчас", card_conflict.text)
            self.assertIn("Ваше значение", card_conflict.text)
            self.assertIn(f"Сотрудник-{conflict_index + 1}", card_conflict.text)
            self.assertIn("Применить моё значение", card_conflict.text)
            self.assertNotIn("Сотрудник-", str(card_conflict.url))

            with SessionLocal() as db:
                card = db.get(DebtorCardDraft, card_id)
                saved_name = str((card.values.get("last_name") or {}).get("value") or "")
                latest_version = card.version
                reapply_card = {
                    name: str((card.values.get(name) or {}).get("value") or "")
                    for name in assignments.DEBTOR_CARD_FIELDS
                }
            my_name = f"Сотрудник-{conflict_index + 1}"
            reapply_card.update(last_name=my_name, version=str(latest_version), action="draft")
            accepted = clients[conflict_index].post(f"/debtors/{card_id}", data=reapply_card, follow_redirects=False)
            self.assertEqual(accepted.status_code, 303)
            with SessionLocal() as db:
                self.assertEqual(db.get(DebtorCardDraft, card_id).values["last_name"]["value"], my_name)
            self.assertNotEqual(saved_name, my_name)

            with SessionLocal() as db:
                draft = db.get(AssignmentDraft, assignment_id)
                assignment_values = {name: str(draft.values.get(name) or "") for name, _ in assignments.ASSIGNMENT_FIELDS}
                assignment_version = draft.version
                case_number, status = draft.case_number, draft.status
            for client in clients:
                page = client.get(f"/assignments/{assignment_id}")
                self.assertEqual(page.status_code, 200)
                self.assertIn(f'name="version" value="{assignment_version}"', page.text)
            assignment_edits = [
                {**assignment_values, "case_number": case_number, "status": status,
                 "practitioner": f"Управляющий-{n}", "version": str(assignment_version)}
                for n in (1, 2)
            ]
            with ThreadPoolExecutor(max_workers=2) as pool:
                assignment_responses = list(pool.map(
                    lambda pair: pair[0].post(f"/assignments/{assignment_id}", data=pair[1], follow_redirects=False),
                    zip(clients, assignment_edits),
                ))
            self.assertEqual(sorted(response.status_code for response in assignment_responses), [200, 303])
            conflict_index = next(index for index, response in enumerate(assignment_responses) if response.status_code == 200)
            assignment_conflict = assignment_responses[conflict_index]
            self.assertEqual(assignment_conflict.headers["cache-control"], "private, no-store")
            self.assertIn("Сохранено сейчас", assignment_conflict.text)
            self.assertIn("Ваше значение", assignment_conflict.text)
            self.assertIn(f"Управляющий-{conflict_index + 1}", assignment_conflict.text)
            self.assertIn("Применить моё значение", assignment_conflict.text)
            with SessionLocal() as db:
                draft = db.get(AssignmentDraft, assignment_id)
                latest_values = {name: str(draft.values.get(name) or "") for name, _ in assignments.ASSIGNMENT_FIELDS}
                latest_version, latest_case, latest_status = draft.version, draft.case_number, draft.status
            my_practitioner = f"Управляющий-{conflict_index + 1}"
            latest_values.update(practitioner=my_practitioner, case_number=latest_case,
                                 status=latest_status, version=str(latest_version))
            accepted = clients[conflict_index].post(
                f"/assignments/{assignment_id}", data=latest_values, follow_redirects=False,
            )
            self.assertEqual(accepted.status_code, 303)
            with SessionLocal() as db:
                self.assertEqual(db.get(AssignmentDraft, assignment_id).values["practitioner"], my_practitioner)
        finally:
            for client in clients:
                client.close()

    def test_worklists_cover_1000_plus_rows_with_stable_cursor_pages(self):
        tag = f"Synthetic-Worklist-{self.suffix}"
        history_card_id = None
        history_assignment_ids = set()
        with session_scope() as db:
            cards = [
                DebtorCardDraft(
                    entity_kind="person", display_name=f"{tag}-{index:04d}", inn=None,
                    preparation_state="reviewed", values={}, created_by_id=self.users[0],
                    updated_by_id=self.users[0], version=1,
                )
                for index in range(1005)
            ]
            db.add_all(cards)
            db.flush()
            card_ids = {card.id for card in cards}
            assignments_rows = [
                AssignmentDraft(
                    debtor_card_id=card.id, case_number=f"{tag}-{index:04d}", status="draft",
                    values={}, version=1, created_by_id=self.users[0], updated_by_id=self.users[0],
                )
                for index, card in enumerate(cards)
            ]
            db.add_all(assignments_rows)
            db.flush()
            assignment_ids = {assignment.id for assignment in assignments_rows}
            history_card = DebtorCardDraft(
                entity_kind="person", display_name=f"Synthetic-Shared-History-{self.suffix}", inn=None,
                preparation_state="reviewed", values={}, created_by_id=self.users[0],
                updated_by_id=self.users[0], version=1,
            )
            db.add(history_card)
            db.flush()
            history_card_id = history_card.id
            history_rows = [
                AssignmentDraft(
                    debtor_card_id=history_card.id,
                    case_number=f"А99-{index:05d}/2099", status="draft", values={},
                    version=1, created_by_id=self.users[0], updated_by_id=self.users[0],
                )
                for index in range(101)
            ]
            db.add_all(history_rows)
            db.flush()
            history_assignment_ids = {assignment.id for assignment in history_rows}

        pattern = f"%{tag}%"
        with SessionLocal() as db:
            start = time.perf_counter()
            old_debtor_rows = db.scalars(
                select(DebtorCardDraft).where(DebtorCardDraft.display_name.ilike(pattern))
                .order_by(DebtorCardDraft.updated_at.desc(), DebtorCardDraft.id.desc())
            ).all()
            old_debtor_ms = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            old_assignment_rows = db.scalars(
                select(AssignmentDraft).where(AssignmentDraft.case_number.ilike(pattern))
                .order_by(AssignmentDraft.updated_at.desc(), AssignmentDraft.id.desc())
            ).all()
            old_assignment_ms = (time.perf_counter() - start) * 1000
        self.assertEqual(len(old_debtor_rows), 1005)
        self.assertEqual(len(old_assignment_rows), 1005)

        test_app = FastAPI()
        from app.api.routes_assignments import router
        test_app.include_router(router)
        test_app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
        test_app.add_api_route("/", lambda: {}, name="tasks_page")
        test_app.add_api_route("/debtors/intake", lambda: {}, name="debtor_intake_page")
        test_app.add_api_route("/logout", lambda: {}, name="logout")
        test_app.add_api_route("/tasks/{task_id}", lambda task_id: {}, name="task_page")
        test_app.add_api_route("/tasks/{task_id}/document", lambda task_id: {}, name="task_document")
        test_app.add_api_route("/tasks/{task_id}/document/preview", lambda task_id: {}, name="task_document_preview")

        def employee(request: Request):
            return SimpleNamespace(id=self.users[0], login="synthetic", full_name="Synthetic staff")

        def db_override():
            with SessionLocal() as db:
                yield db

        test_app.dependency_overrides[current_user] = employee
        test_app.dependency_overrides[get_session] = db_override
        with TestClient(test_app, base_url="http://testserver") as client:
            history_index = SessionLocal()
            try:
                index_definition = history_index.execute(text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
                    "AND indexname='ix_assignment_draft_debtor_card_id_id'"
                )).scalar_one()
            finally:
                history_index.close()
            self.assertIn("(debtor_card_id, id)", index_definition)

            history_page = client.get(f"/debtors/{history_card_id}")
            self.assertEqual(history_page.status_code, 200)
            self.assertIn("50 из 101", history_page.text)
            self.assertIn("Страница 1 из 3", history_page.text)
            history_collected = set(map(int, re.findall(
                r'href="http://testserver/assignments/(\d+)"', history_page.text,
            )))
            self.assertEqual(len(history_collected), 50)
            next_history = re.search(
                r'<nav class="pagination" aria-label="Страницы назначений по карточке">'
                r'.*?href="([^"]*before=[^"]+)"', history_page.text, re.S,
            )
            self.assertIsNotNone(next_history)
            history_url = unescape(next_history.group(1))
            while history_url:
                page = client.get(history_url)
                self.assertEqual(page.status_code, 200)
                ids = set(map(int, re.findall(
                    r'href="http://testserver/assignments/(\d+)"', page.text,
                )))
                self.assertFalse(history_collected.intersection(ids))
                history_collected.update(ids)
                navigation = re.search(
                    r'<nav class="pagination" aria-label="Страницы назначений по карточке">'
                    r'.*?href="([^"]*before=[^"]+)"', page.text, re.S,
                )
                history_url = unescape(navigation.group(1)) if navigation else ""
            self.assertEqual(history_collected, history_assignment_ids)

            def load_old_unpaged(request, db, statement, entity, path, filters):
                query = statement.order_by(entity.updated_at.desc(), entity.id.desc())
                if path == "/assignments/new":
                    query = query.limit(100)
                rows = db.scalars(query).all()
                return rows, len(rows), 1, 1, "", ""

            with patch.object(routes_assignments, "_worklist_page", side_effect=load_old_unpaged):
                start = time.perf_counter()
                old_debtors_response = client.get("/debtors", params={"q": tag})
                old_debtors_http_ms = (time.perf_counter() - start) * 1000
                start = time.perf_counter()
                old_assignments_response = client.get("/assignments", params={"q": tag})
                old_assignments_http_ms = (time.perf_counter() - start) * 1000
                start = time.perf_counter()
                old_choice_response = client.get("/assignments/new", params={"q": tag})
                old_choice_http_ms = (time.perf_counter() - start) * 1000
            self.assertEqual(old_debtors_response.status_code, 200)
            self.assertEqual(old_assignments_response.status_code, 200)
            self.assertEqual(old_choice_response.status_code, 200)
            self.assertEqual(len(re.findall(r'class="primary-link" href="http://testserver/debtors/\d+"', old_debtors_response.text)), 1005)
            self.assertEqual(len(re.findall(r'href="http://testserver/assignments/\d+"', old_assignments_response.text)), 1005)
            self.assertEqual(len(re.findall(r'name="debtor_id" value="\d+"', old_choice_response.text)), 100)

            baseline_assignment_selects = []
            def count_baseline_assignment_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
                if (
                    statement.lstrip().upper().startswith("SELECT")
                    and "set_config" not in statement.lower()
                ):
                    baseline_assignment_selects.append(statement)
            event.listen(engine, "before_cursor_execute", count_baseline_assignment_selects)
            try:
                with SessionLocal() as db:
                    legacy_rows = db.scalars(
                        select(AssignmentDraft).where(AssignmentDraft.case_number.ilike(pattern))
                        .order_by(AssignmentDraft.id.desc()).limit(50)
                    ).all()
                    for row in legacy_rows:
                        _ = row.debtor_card.display_name, row.updated_by.full_name
            finally:
                event.remove(engine, "before_cursor_execute", count_baseline_assignment_selects)
            self.assertEqual(len(legacy_rows), 50)
            # One row query, 50 distinct debtor-card loads and one shared user load.
            self.assertEqual(len(baseline_assignment_selects), 52)

            start = time.perf_counter()
            first = client.get("/debtors", params={"q": tag})
            debtor_first_page_ms = (time.perf_counter() - start) * 1000
            self.assertEqual(first.status_code, 200)
            self.assertIn("1005 всего", first.text)
            self.assertIn("Страница 1 из 21", first.text)
            collected = set(map(int, re.findall(r"/debtors/(\d+)", " ".join(
                re.findall(r'class="primary-link" href="([^"]+)"', first.text)
            ))))
            self.assertEqual(len(collected), 50)

            # A new row above the cursor must not shift or duplicate older results.
            with session_scope() as db:
                newer = DebtorCardDraft(
                    entity_kind="person", display_name=f"{tag}-new-during-scan", inn=None,
                    preparation_state="reviewed", values={}, created_by_id=self.users[0],
                    updated_by_id=self.users[0], version=1,
                )
                db.add(newer)
                db.flush()
                newer_id = newer.id

            start = time.perf_counter()
            next_link = re.search(r'rel="next" href="([^"]+)"', first.text)
            self.assertIsNotNone(next_link)
            page_url = unescape(next_link.group(1))
            while page_url:
                page = client.get(page_url)
                self.assertEqual(page.status_code, 200)
                ids = set(map(int, re.findall(r"/debtors/(\d+)", " ".join(
                    re.findall(r'class="primary-link" href="([^"]+)"', page.text)
                ))))
                self.assertFalse(collected.intersection(ids))
                collected.update(ids)
                following = re.search(r'rel="next" href="([^"]+)"', page.text)
                page_url = unescape(following.group(1)) if following else ""
            debtor_full_sweep_ms = (time.perf_counter() - start) * 1000
            self.assertEqual(collected, card_ids)
            self.assertNotIn(newer_id, collected)

            assignment_selects = []
            def count_assignment_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
                if (
                    statement.lstrip().upper().startswith("SELECT")
                    and "set_config" not in statement.lower()
                ):
                    assignment_selects.append(statement)
            event.listen(engine, "before_cursor_execute", count_assignment_selects)
            start = time.perf_counter()
            try:
                assignment_page = client.get("/assignments", params={"q": tag})
            finally:
                event.remove(engine, "before_cursor_execute", count_assignment_selects)
            assignment_first_page_ms = (time.perf_counter() - start) * 1000
            self.assertEqual(assignment_page.status_code, 200)
            self.assertEqual(
                len(assignment_selects), 2,
                f"assignment list must avoid per-row relationship queries: {assignment_selects}",
            )
            self.assertIn("1005 всего", assignment_page.text)
            self.assertIn("Страница 1 из 21", assignment_page.text)
            assignment_collected = set(map(int, re.findall(
                r'href="http://testserver/assignments/(\d+)"', assignment_page.text,
            )))
            self.assertEqual(len(assignment_collected), 50)
            start = time.perf_counter()
            next_link = re.search(r'rel="next" href="([^"]+)"', assignment_page.text)
            page_url = unescape(next_link.group(1)) if next_link else ""
            while page_url:
                page = client.get(page_url)
                self.assertEqual(page.status_code, 200)
                ids = set(map(int, re.findall(
                    r'href="http://testserver/assignments/(\d+)"', page.text,
                )))
                self.assertFalse(assignment_collected.intersection(ids))
                assignment_collected.update(ids)
                following = re.search(r'rel="next" href="([^"]+)"', page.text)
                page_url = unescape(following.group(1)) if following else ""
            assignment_full_sweep_ms = (time.perf_counter() - start) * 1000
            self.assertEqual(assignment_collected, assignment_ids)

            start = time.perf_counter()
            choice_page = client.get("/assignments/new", params={"q": tag})
            choice_first_page_ms = (time.perf_counter() - start) * 1000
            self.assertEqual(choice_page.status_code, 200)
            self.assertIn("1006", choice_page.text)
            self.assertIn("Страница 1 из 21", choice_page.text)
            choice_collected = set(map(int, re.findall(r'name="debtor_id" value="(\d+)"', choice_page.text)))
            self.assertEqual(len(choice_collected), 50)
            start = time.perf_counter()
            next_link = re.search(r'rel="next" href="([^"]+)"', choice_page.text)
            page_url = unescape(next_link.group(1)) if next_link else ""
            while page_url:
                page = client.get(page_url)
                self.assertEqual(page.status_code, 200)
                ids = set(map(int, re.findall(r'name="debtor_id" value="(\d+)"', page.text)))
                self.assertFalse(choice_collected.intersection(ids))
                choice_collected.update(ids)
                following = re.search(r'rel="next" href="([^"]+)"', page.text)
                page_url = unescape(following.group(1)) if following else ""
            choice_full_sweep_ms = (time.perf_counter() - start) * 1000
            self.assertEqual(choice_collected, card_ids | {newer_id})

            choice_forms = re.findall(
                r'<form method="post" action="[^\"]*/assignments/new">(.*?)</form>',
                choice_page.text, flags=re.DOTALL,
            )
            form_actions = []
            for form_html in choice_forms:
                debtor_match = re.search(r'name="debtor_id" value="(\d+)"', form_html)
                request_match = re.search(r'name="request_key" value="([0-9a-f]{64})"', form_html)
                self.assertIsNotNone(debtor_match)
                self.assertIsNotNone(request_match)
                form_actions.append((debtor_match.group(1), request_match.group(1)))
            self.assertEqual(len(form_actions), 50)
            self.assertEqual(len({key for _debtor_id, key in form_actions}), 50)
            independent_responses = [
                client.post(
                    "/assignments/new",
                    data={"debtor_id": debtor_id, "request_key": request_key},
                    follow_redirects=False,
                )
                for debtor_id, request_key in form_actions[:2]
            ]
            self.assertEqual([response.status_code for response in independent_responses], [303, 303])
            first_location, second_location = [response.headers["location"] for response in independent_responses]
            self.assertNotEqual(first_location, second_location)
            replay = client.post(
                "/assignments/new",
                data={"debtor_id": form_actions[0][0], "request_key": form_actions[0][1]},
                follow_redirects=False,
            )
            self.assertEqual(replay.status_code, 303)
            self.assertEqual(replay.headers["location"], first_location)

        print(
            "WORKLIST_BENCHMARK rows=1005 "
            f"baseline_debtors_all_ms={old_debtor_ms:.2f} "
            f"first_debtors_page_ms={debtor_first_page_ms:.2f} "
            f"full_debtors_sweep_ms={debtor_full_sweep_ms:.2f} "
            f"baseline_assignments_all_ms={old_assignment_ms:.2f} "
            f"baseline_debtors_http_ms={old_debtors_http_ms:.2f} "
            f"baseline_assignments_http_ms={old_assignments_http_ms:.2f} "
            f"baseline_import_choice_http_ms={old_choice_http_ms:.2f} "
            f"baseline_assignments_page_selects={len(baseline_assignment_selects)} "
            f"first_assignments_page_ms={assignment_first_page_ms:.2f} "
            f"first_assignments_page_selects={len(assignment_selects)} "
            f"full_assignments_sweep_ms={assignment_full_sweep_ms:.2f} "
            f"first_import_choice_page_ms={choice_first_page_ms:.2f} "
            f"full_import_choice_sweep_ms={choice_full_sweep_ms:.2f} "
            f"cards_seen={len(collected)} assignments_seen={len(assignment_collected)} "
            f"import_choices_seen={len(choice_collected)} pages=21 no_gaps=true "
            "independent_choice_forms=50 unique_keys=50 sequential_creates=2 replay_same=true"
        )
    def test_two_employee_review_tokens_refuse_lost_update(self):
        task_id = self.make_task(status=TaskStatus.needs_review)
        with SessionLocal() as db:
            token = tasks.review_token(db.get(Task, task_id))
        barrier = threading.Barrier(2)
        def edit(user_id):
            with session_scope() as db:
                task = db.get(Task, task_id)
                barrier.wait(timeout=10)
                try:
                    tasks.assert_review_token(db, task, token)
                except ValueError:
                    return "stale"
                tasks.save_values(db, task, {"last_name": f"Synthetic {user_id}"}, user_id)
                return "saved"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(edit, self.users)), ["saved", "stale"])
        with SessionLocal() as db:
            events = db.scalars(select(Event).where(Event.task_id == task_id, Event.kind == "fields_saved")).all()
            self.assertEqual(len(events), 1)
            self.assertIn(events[0].user_id, self.users)

    def test_internal_corrections_record_actor_and_immutable_revisions(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with session_scope() as db:
            card = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id))
            values = {name: str((card.values.get(name) or {}).get("value") or "") for name in assignments.DEBTOR_CARD_FIELDS}
            values["address"] = "101000, г. Примерск, ул. Исправленная, д. 2"
            card_id = card.id
            assignments.update_debtor_card(db, card_id, self.users[1], 1, values)
        with SessionLocal() as db:
            card = db.get(DebtorCardDraft, card_id)
            self.assertEqual(card.version, 2)
            self.assertEqual(card.updated_by_id, self.users[1])
            self.assertEqual(card.preparation_state, "reviewed")
            self.assertEqual(card.values["address"]["value"], values["address"])
            revisions = db.scalars(select(DebtorCardDraftRevision).where(DebtorCardDraftRevision.debtor_card_id == card_id).order_by(DebtorCardDraftRevision.revision)).all()
            self.assertEqual([r.changed_by_id for r in revisions], self.users)
            self.assertEqual([r.snapshot["preparation_state"] for r in revisions], ["prepared", "reviewed"])
        for sql in (
            "UPDATE debtor_card_draft_revision SET snapshot = '{}' WHERE debtor_card_id = :id",
            "DELETE FROM debtor_card_draft_revision WHERE debtor_card_id = :id",
            "TRUNCATE debtor_card_draft_revision",
            "INSERT INTO debtor_card_draft_revision (debtor_card_id,revision,snapshot,changed_by_id) VALUES (:id,99,'{}',:actor)",
        ):
            with self.subTest(sql=sql), self.assertRaises(DBAPIError), session_scope() as db:
                db.execute(text(sql), {"id": card_id, "actor": self.users[0]})
        with session_scope() as db:
            stale_values = dict(values)
            stale_values["address"] = "101000, г. Примерск, ул. Параллельная, д. 9"
            with self.assertRaises(assignments.DraftConflict):
                assignments.update_debtor_card(db, card_id, self.users[0], 1, stale_values)

    def test_bulk_dashboard_counts_and_polls_active_work_outside_page_and_scopes_operations(self):
        operation_a = self.make_operation(0)
        operation_b = self.make_operation(1)
        older_active = self.make_task(0, TaskStatus.recognizing, operation_a)
        older_queued = self.make_task(0, TaskStatus.new, operation_a)
        terminal_ids = [self.make_task(1, TaskStatus.failed, operation_b) for _ in range(bulk.PAGE_SIZE)]
        with session_scope() as db:
            active = db.get(Task, older_active)
            active.created_at = active.created_at.replace(year=2000)
            queued = db.get(Task, older_queued)
            queued.created_at = queued.created_at.replace(year=1999)

        with SessionLocal() as db:
            first = bulk.bulk_task_context(db, [operation_a, operation_b], page=1)
            second = bulk.bulk_task_context(db, [operation_a, operation_b], page=2)
            only_b = bulk.bulk_task_context(db, [operation_b], status="failed", page=1)
            outsider = bulk.bulk_task_context(db, [self.make_operation()], page=1)
        self.assertEqual(first["counts"]["total"], bulk.PAGE_SIZE + 2)
        self.assertEqual(first["counts"]["failed"], bulk.PAGE_SIZE)
        self.assertEqual(first["counts"]["processing"], 1)
        self.assertEqual(first["counts"]["queued"], 1)
        self.assertGreater(first["oldest_queued_age_seconds"], 0)
        self.assertTrue(first["should_poll"], "polling must consider active work beyond page one")
        self.assertEqual(len(first["task_rows"]), bulk.PAGE_SIZE)
        self.assertEqual((first["page"], first["pages"]), (1, 2))
        self.assertEqual(second["task_rows"][0]["task"].id, older_active)
        self.assertEqual(only_b["counts"]["total"], bulk.PAGE_SIZE)
        self.assertEqual(only_b["counts"]["failed"], bulk.PAGE_SIZE)
        self.assertTrue(all(row["task"].id in terminal_ids for row in only_b["task_rows"]))
        self.assertEqual(outsider["counts"]["total"], 0)
        # The operation owned by the second employee is visible when explicitly selected.
        self.assertTrue(all(row["task"].assignee_id == self.users[1] for row in only_b["task_rows"]))

    def test_task_dashboard_cursor_keeps_page_boundary_stable_when_new_tasks_arrive(self):
        operation_id = self.make_operation()
        task_ids = [
            self.make_task(status=TaskStatus.failed, operation_id=operation_id)
            for _ in range(bulk.PAGE_SIZE + 1)
        ]
        with SessionLocal() as db:
            first = bulk.bulk_task_context(db, [operation_id], status="failed")
        first_ids = [row["task"].id for row in first["task_rows"]]
        self.assertEqual(len(first_ids), bulk.PAGE_SIZE)
        self.assertIsNotNone(first["next_cursor"])

        # A new newest task would shift a page-2 OFFSET by one and repeat the
        # old page-1 tail. The keyset cursor must still reach that tail once.
        newest_id = self.make_task(status=TaskStatus.failed, operation_id=operation_id)
        with SessionLocal() as db:
            older = bulk.bulk_task_context(
                db, [operation_id], status="failed",
                cursor=("before", *first["next_cursor"]),
            )
        older_ids = [row["task"].id for row in older["task_rows"]]
        self.assertEqual(older_ids, [task_ids[0]])
        self.assertTrue(set(first_ids).isdisjoint(older_ids))
        self.assertEqual((older["page"], older["pages"]), (2, 2))

        with SessionLocal() as db:
            newer = bulk.bulk_task_context(
                db, [operation_id], status="failed",
                cursor=("after", *older["previous_cursor"]),
            )
        newer_ids = [row["task"].id for row in newer["task_rows"]]
        self.assertEqual(newer_ids, first_ids)
        self.assertNotIn(newest_id, newer_ids)
        self.assertTrue(set(newer_ids).isdisjoint(older_ids))

    def test_next_upload_review_task_stays_inside_exact_receipt_scope_and_wraps(self):
        operation_a = self.make_operation()
        operation_b = self.make_operation(user=1)
        task_ids = [
            self.make_task(status=TaskStatus.needs_review, operation_id=operation_a)
            for _ in range(3)
        ]
        outside_id = self.make_task(user=1, status=TaskStatus.needs_review, operation_id=operation_b)
        task_ids.append(outside_id)
        kad_receipt_id = self.make_task(status=TaskStatus.needs_review, operation_id=operation_a)
        task_ids.append(kad_receipt_id)
        no_receipt_id = self.make_task(status=TaskStatus.needs_review, operation_id=operation_a)
        task_ids.append(no_receipt_id)
        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with session_scope() as db:
            for index, task_id in enumerate(task_ids):
                task = db.get(Task, task_id)
                if task_id == no_receipt_id:
                    continue
                item = IntakeItem(
                    operation_id=task.intake_operation_id,
                    item_index=index,
                    document_id=task.document_id,
                    filename=f"synthetic-review-{index}.pdf",
                    source=(IntakeSource.kad if task_id == kad_receipt_id else IntakeSource.batch),
                    received_by_id=task.assignee_id,
                )
                db.add(item)
                db.flush()
                task.intake_item_id = item.id
                task.created_at = base_time + timedelta(minutes=index)

        with SessionLocal() as db:
            newest = db.get(Task, task_ids[2])
            oldest = db.get(Task, task_ids[0])
            self.assertEqual(
                bulk.next_upload_review_task_id(db, [operation_a], newest), task_ids[1]
            )
            self.assertEqual(
                bulk.next_upload_review_task_id(db, [operation_a], oldest), task_ids[2]
            )
            self.assertIsNone(
                bulk.next_upload_review_task_id(db, [operation_a], db.get(Task, outside_id))
            )

    def test_queue_cursor_recovers_to_current_head_when_older_task_starts_processing(self):
        operation_id = self.make_operation()
        task_ids = [
            self.make_task(status=TaskStatus.new, operation_id=operation_id)
            for _ in range(bulk.PAGE_SIZE + 1)
        ]
        oldest_id = task_ids[0]
        with session_scope() as db:
            oldest = db.get(Task, oldest_id)
            operation = db.get(IntakeOperation, operation_id)
            oldest_document = db.get(Document, oldest.document_id)
            item = IntakeItem(
                operation_id=operation.id,
                item_index=0,
                document_id=oldest_document.id,
                filename=oldest_document.filename,
                source=IntakeSource.batch,
                received_by_id=operation.user_id,
            )
            db.add(item)
            db.flush()
            oldest.intake_item_id = item.id
            oldest.created_at = oldest.created_at.replace(year=2000)

        with SessionLocal() as db:
            first = bulk.bulk_task_context(db, [operation_id], status="queued")
        first_ids = [row["task"].id for row in first["task_rows"]]
        self.assertEqual(len(first_ids), bulk.PAGE_SIZE)
        self.assertIn(oldest_id, set(task_ids) - set(first_ids))
        self.assertIsNotNone(first["next_cursor"])

        with session_scope() as db:
            self.assertTrue(tasks.claim_unleased_task(db, db.get(Task, oldest_id)))
        with SessionLocal() as db:
            current = bulk.bulk_task_context(
                db, [operation_id], status="queued",
                cursor=("before", *first["next_cursor"]),
            )

        current_ids = [row["task"].id for row in current["task_rows"]]
        self.assertEqual(current_ids, first_ids)
        self.assertEqual((current["page"], current["pages"]), (1, 1))
        self.assertEqual(current["counts"]["queued"], bulk.PAGE_SIZE)
        self.assertEqual(current["counts"]["processing"], 1)

    def test_combined_approval_route_rejects_stale_review_token_without_mutation(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            stale = tasks.review_token(task)
        with session_scope() as db:
            task = db.get(Task, task_id)
            tasks.save_values(db, task, {"last_name": "Synthetic concurrent correction"}, self.users[0])
        response = self.post_review_form(task_id, stale)
        self.assertEqual(response.status_code, 409)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertIsNone(task.approved_by_id)
            self.assertEqual(task.values["last_name"], "Synthetic concurrent correction")
            # OCR already prepared a candidate. A stale form must leave it intact.
            candidate = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id))
            self.assertEqual(candidate.preparation_state, "prepared")
            self.assertEqual(candidate.version, 1)
            self.assertEqual(candidate.display_name, "Макетова Анна Петровна")

    def test_stale_approval_preserves_submitted_values_in_conflict_response(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with SessionLocal() as db:
            token = tasks.review_token(db.get(Task, task_id))
        with session_scope() as db:
            task = db.get(Task, task_id)
            tasks.save_values(db, task, {"last_name": "Актуальное исправление"}, self.users[0])

        submitted = "Не сохранённое исправление"
        response = self.post_review_form(
            task_id, token, {"last_name": submitted}, action="approve",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn(submitted, response.text)
        self.assertIn("Актуальное исправление", response.text)
        self.assertNotIn(f'formaction="http://testserver/tasks/{task_id}/approve"', response.text)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(task.values["last_name"], "Актуальное исправление")

    def test_stale_internal_card_approval_preserves_submitted_values(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with SessionLocal() as db:
            token = tasks.review_token(db.get(Task, task_id))
        with session_scope() as db:
            task = db.get(Task, task_id)
            tasks.save_values(db, task, {"last_name": "Актуальное исправление"}, self.users[0])

        submitted = "Не сохранённое исправление внутренней карточки"
        response = self.post_review_form(
            task_id, token, {"last_name": submitted}, action="approve-internal-card",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn(submitted, response.text)
        self.assertIn("Актуальное исправление", response.text)
        self.assertNotIn(
            f'formaction="http://testserver/tasks/{task_id}/approve-internal-card"',
            response.text,
        )
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            candidate = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id))
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(task.values["last_name"], "Актуальное исправление")
            self.assertEqual(candidate.preparation_state, "prepared")
            self.assertEqual(candidate.version, 1)
            rejected = db.scalar(select(Event).where(
                Event.task_id == task_id,
                Event.kind == "operator_action_not_applied",
            ))
            self.assertEqual(rejected.user_id, self.users[1])
            self.assertEqual(rejected.payload["action"], "approve")
            self.assertEqual(rejected.payload["reason"], "precondition_not_met")
            self.assertNotIn(submitted, repr(rejected.payload))

    def test_internal_card_approval_refuses_invalid_manual_snils_without_partial_commit(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with SessionLocal() as db:
            token = tasks.review_token(db.get(Task, task_id))

        submitted = "11223344596"
        response = self.post_review_form(
            task_id, token, {"snils": submitted}, action="approve-internal-card",
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("СНИЛС должен содержать 11 цифр", response.text)
        self.assertIn(submitted, response.text)
        self.assertNotIn(f'formaction="http://testserver/tasks/{task_id}/approve-internal-card"', response.text)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            candidate = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id))
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(task.values, {})
            self.assertEqual(candidate.preparation_state, "prepared")
            self.assertEqual(candidate.version, 1)
            rejected = db.scalar(select(Event).where(
                Event.task_id == task_id,
                Event.kind == "operator_action_not_applied",
            ))
            self.assertEqual(rejected.user_id, self.users[1])
            self.assertEqual(rejected.payload["action"], "approve")
            self.assertNotIn(submitted, repr(rejected.payload))

    def test_combined_approval_response_replay_returns_the_existing_card(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            token = tasks.review_token(task)
        corrections = {
            "last_name": "Исправленная повторная проверка",
            "first_name": "Анна",
            "middle_name": "Петровна",
        }
        first = self.post_review_form(task_id, token, corrections)
        self.assertEqual(first.status_code, 303)
        location = first.headers["location"]
        card_id = int(location.split("/", 2)[2].split("?", 1)[0])
        with SessionLocal() as db:
            card = db.get(DebtorCardDraft, card_id)
            task = db.get(Task, task_id)
            approval_count = int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id, Event.kind == "approved",
                )
            ) or 0)
            revision_count = int(db.scalar(
                select(func.count()).select_from(DebtorCardDraftRevision).where(
                    DebtorCardDraftRevision.debtor_card_id == card_id,
                )
            ) or 0)
            self.assertEqual(card.preparation_state, "reviewed")
            self.assertEqual(task.status, TaskStatus.approved)
            self.assertTrue(tasks.approval_matches(db, task))

        replay = self.post_review_form(task_id, token, corrections)

        self.assertEqual(replay.status_code, 303)
        self.assertEqual(replay.headers["location"], location)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            card = db.get(DebtorCardDraft, card_id)
            self.assertEqual(task.status, TaskStatus.approved)
            self.assertEqual(task.approved_by_id, self.users[1])
            self.assertTrue(tasks.approval_matches(db, task))
            self.assertEqual(card.preparation_state, "reviewed")
            self.assertEqual(card.display_name, "Исправленная повторная проверка Анна Петровна")
            self.assertEqual(int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id, Event.kind == "approved",
                )
            ) or 0), approval_count)
            self.assertEqual(int(db.scalar(
                select(func.count()).select_from(DebtorCardDraftRevision).where(
                    DebtorCardDraftRevision.debtor_card_id == card_id,
                )
            ) or 0), revision_count)

    def test_field_save_response_replay_does_not_append_audit_event(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            token = tasks.review_token(task)
        corrections = {"last_name": "Исправление для точного повтора"}
        first = self.post_review_form(task_id, token, corrections, action="save")
        self.assertEqual(first.status_code, 303)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(task.values["last_name"], corrections["last_name"])
            save_count = int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id, Event.kind == "fields_saved",
                )
            ) or 0)
            rejected_count = int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id, Event.kind == "operator_action_not_applied",
                )
            ) or 0)

        replay = self.post_review_form(task_id, token, corrections, action="save")

        self.assertEqual(replay.status_code, 303)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertEqual(task.values["last_name"], corrections["last_name"])
            self.assertEqual(int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id, Event.kind == "fields_saved",
                )
            ) or 0), save_count)
            self.assertEqual(int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id, Event.kind == "operator_action_not_applied",
                )
            ) or 0), rejected_count)

        changed_stale_form = self.post_review_form(
            task_id, token, {"last_name": "Изменённый устаревший ввод"}, action="save",
        )
        self.assertEqual(changed_stale_form.status_code, 409)
        self.assertIn("введённые значения не сохранены", changed_stale_form.text)
        self.assertIn(corrections["last_name"], changed_stale_form.text)
        self.assertIn("Изменённый устаревший ввод", changed_stale_form.text)
        self.assertIn("Открыть актуальную версию", changed_stale_form.text)
        self.assertNotIn('formaction="http://testserver/tasks/' + str(task_id) + '/save"', changed_stale_form.text)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            self.assertEqual(task.values["last_name"], corrections["last_name"])
            self.assertEqual(int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id, Event.kind == "fields_saved",
                )
            ) or 0), save_count)

    def test_task_approval_response_replay_does_not_repeat_approval(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            token = tasks.review_token(task)
        corrections = {"last_name": "Исправление перед подтверждением"}
        first = self.post_review_form(task_id, token, corrections, action="approve")
        self.assertEqual(first.status_code, 303)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            self.assertEqual(task.status, TaskStatus.approved)
            self.assertEqual(task.approved_by_id, self.users[1])
            save_count = int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id, Event.kind == "fields_saved",
                )
            ) or 0)
            approval_count = int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id, Event.kind == "approved",
                )
            ) or 0)

        replay = self.post_review_form(task_id, token, corrections, action="approve")

        self.assertEqual(replay.status_code, 303)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            self.assertEqual(task.status, TaskStatus.approved)
            self.assertEqual(task.approved_by_id, self.users[1])
            self.assertTrue(tasks.approval_matches(db, task))
            self.assertEqual(int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id, Event.kind == "fields_saved",
                )
            ) or 0), save_count)
            self.assertEqual(int(db.scalar(
                select(func.count()).select_from(Event).where(
                    Event.task_id == task_id, Event.kind == "approved",
                )
            ) or 0), approval_count)

    def test_duplicate_combined_approval_preserves_corrections_and_approval(self):
        first_id = self.make_task()
        self.recognize(first_id)
        duplicate_id = self.make_task()
        self.recognize(duplicate_id)
        with SessionLocal() as db:
            task = db.get(Task, duplicate_id)
            token = tasks.review_token(task)
        response = self.post_review_form(
            duplicate_id, token,
            {"last_name": "Исправленная", "first_name": "Анна", "middle_name": "Петровна"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/tasks/{duplicate_id}?internal_card=duplicate")
        with SessionLocal() as db:
            task = db.get(Task, duplicate_id)
            self.assertEqual(task.status, TaskStatus.approved)
            self.assertEqual(task.approved_by_id, self.users[1])
            self.assertEqual(task.values["last_name"], "Исправленная")
            self.assertTrue(tasks.approval_matches(db, task))
            self.assertIsNone(db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == duplicate_id)))

    def test_combined_approval_cannot_overwrite_a_manually_corrected_prepared_card(self):
        task_id = self.make_task()
        self.recognize(task_id)
        with session_scope() as db:
            card = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id))
            values = {name: str((card.values.get(name) or {}).get("value") or "")
                      for name in assignments.DEBTOR_CARD_FIELDS}
            values["address"] = "101000, г. Примерск, ул. Сохранённая правка, д. 7"
            assignments.update_debtor_card(db, card.id, self.users[1], card.version, values)
            card_id = card.id
        with SessionLocal() as db:
            token = tasks.review_token(db.get(Task, task_id))
        response = self.post_review_form(task_id, token)
        self.assertEqual(response.status_code, 409)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            card = db.get(DebtorCardDraft, card_id)
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertIsNone(task.approved_by_id)
            self.assertEqual(card.version, 2)
            self.assertEqual(card.preparation_state, "reviewed")
            self.assertEqual(card.values["address"]["value"], values["address"])

    def test_ambiguous_role_stays_in_review_without_card(self):
        task_id = self.make_task()
        with session_scope() as db:
            task = db.get(Task, task_id)
            card = parse([f"Заявитель: Петрова Мария Ивановна, ИНН {self.inn}. Должник: ООО Макет"])
            self.assertTrue(tasks.save_recognized(db, task, card))
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id)))
            self.assertEqual(db.get(Task, task_id).status, TaskStatus.needs_review)


if __name__ == "__main__":
    unittest.main()
