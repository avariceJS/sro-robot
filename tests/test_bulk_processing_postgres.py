"""Synthetic end-to-end persistence and real two-employee race boundaries."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from fastapi.testclient import TestClient
from types import SimpleNamespace

from app.db.base import SessionLocal, session_scope
from app.db.models import DebtorCardDraft, DebtorCardDraftRevision, Document, Event, IntakeOperation, IntakeSource, JobOutbox, Task, TaskStatus, User
from app.services import assignments, bulk, fields, tasks
from app.api.deps import current_user
from app.db.base import get_session
from app.main import app
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

    def recognize(self, task_id):
        with session_scope() as db:
            return tasks.save_recognized(db, db.get(Task, task_id), synthetic_card(self.inn))

    def post_review_form(self, task_id, token, overrides=None, user=1):
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
                return client.post(f"/tasks/{task_id}/approve-internal-card", data=form,
                                   headers={"Origin": "http://testserver"}, follow_redirects=False)
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(current_user, None)

    def test_ocr_prepares_internal_card_without_approval_or_external_intent(self):
        task_id = self.make_task()
        self.assertTrue(self.recognize(task_id))
        self.assertFalse(self.recognize(task_id))
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            card = db.scalar(select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == task_id))
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertIsNone(task.approved_by_id)
            self.assertEqual(card.preparation_state, "prepared")
            self.assertEqual(card.created_by_id, self.users[0])
            self.assertEqual(card.display_name, "Макетова Анна Петровна")
            self.assertEqual(db.scalar(select(func.count()).select_from(JobOutbox).where(JobOutbox.task_id == task_id)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(Event).where(Event.task_id == task_id, Event.kind == "approved")), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(DebtorCardDraftRevision).where(DebtorCardDraftRevision.debtor_card_id == card.id)), 1)

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
            with self.assertRaises(assignments.DraftConflict):
                assignments.update_debtor_card(db, card_id, self.users[0], 1, values)

    def test_bulk_dashboard_counts_and_polls_active_work_outside_page_and_scopes_operations(self):
        operation_a = self.make_operation(0)
        operation_b = self.make_operation(1)
        older_active = self.make_task(0, TaskStatus.recognizing, operation_a)
        terminal_ids = [self.make_task(1, TaskStatus.failed, operation_b) for _ in range(bulk.PAGE_SIZE)]
        with session_scope() as db:
            active = db.get(Task, older_active)
            active.created_at = active.created_at.replace(year=2000)

        with SessionLocal() as db:
            first = bulk.bulk_task_context(db, [operation_a, operation_b], page=1)
            second = bulk.bulk_task_context(db, [operation_a, operation_b], page=2)
            only_b = bulk.bulk_task_context(db, [operation_b], status="failed", page=1)
            outsider = bulk.bulk_task_context(db, [self.make_operation()], page=1)
        self.assertEqual(first["counts"]["total"], bulk.PAGE_SIZE + 1)
        self.assertEqual(first["counts"]["failed"], bulk.PAGE_SIZE)
        self.assertEqual(first["counts"]["processing"], 1)
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
