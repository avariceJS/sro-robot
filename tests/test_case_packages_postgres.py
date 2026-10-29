from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import unittest
import uuid

from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.db.base import SessionLocal, engine, session_scope
from app.db.models import (
    AssignmentDraft,
    CasePackage,
    CasePackageAssignmentEvent,
    CasePackageTaskEvent,
    DebtorCardDraft,
    Document,
    IntakeSource,
    Task,
    TaskStatus,
    User,
)
from app.services import case_packages
from tests.postgres_safety import assert_disposable_postgres


@unittest.skipUnless(
    os.environ.get("SRO_TEST_POSTGRES") == "1",
    "requires the disposable PostgreSQL migration drill",
)
class CasePackagePostgreSQLTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_disposable_postgres()
        with engine.connect() as connection:
            self.assertEqual(
                MigrationContext.configure(connection).get_current_revision(),
                "20261029_preview",
            )
        suffix = uuid.uuid4().hex
        with session_scope() as db:
            user = User(
                login=f"case-package-{suffix}",
                full_name="Synthetic Case Package Operator",
                password_hash="synthetic-only",
                sro_login="synthetic-case-package",
                is_active=True,
            )
            db.add(user)
            db.flush()
            self.user_id = user.id
            self.task_ids = []
            for index in range(2):
                content = f"synthetic case package document {suffix} {index}".encode()
                document = Document(
                    filename=f"synthetic-act-{index}.pdf",
                    stored_path=f"/tmp/synthetic-act-{suffix}-{index}.pdf",
                    content_hash=hashlib.sha256(content).hexdigest(),
                    source=IntakeSource.upload,
                    uploaded_by_id=user.id,
                )
                db.add(document)
                db.flush()
                task = Task(
                    document_id=document.id,
                    scenario="person_card",
                    profile="default",
                    status=TaskStatus.needs_review,
                    recognized={},
                    values={},
                    case_number="А00-123/2026",
                )
                db.add(task)
                db.flush()
                self.task_ids.append(task.id)
            self.package, _ = case_packages.create_once(
                db,
                user_id=user.id,
                request_key=uuid.uuid4().hex * 2,
                title="Синтетический пакет",
                case_number="А00-123/2026",
            )
            self.package_id = self.package.id

    def _create_same_case_assignments(self) -> tuple[int, int, int]:
        with session_scope() as db:
            card = DebtorCardDraft(
                entity_kind="person",
                display_name="Synthetic Shared Debtor",
                preparation_state="reviewed",
                values={},
                created_by_id=self.user_id,
                updated_by_id=self.user_id,
            )
            db.add(card)
            db.flush()
            shared = {
                "debtor_card_id": card.id,
                "source_task_id": None,
                "case_number": "А00-SHARED/2026",
                "status": "draft",
                "created_by_id": self.user_id,
                "updated_by_id": self.user_id,
            }
            first = AssignmentDraft(**shared, values={"comments": "First"})
            second = AssignmentDraft(**shared, values={"comments": "Second"})
            db.add_all([first, second])
            db.flush()
            return first.id, second.id, card.id

    def test_membership_is_idempotent_and_retains_attributed_history(self) -> None:
        package_id = self.package_id
        task_id = self.task_ids[0]
        with session_scope() as db:
            self.assertTrue(case_packages.set_task_membership(
                db, package_id=package_id, task_id=task_id, actor_id=self.user_id,
                included=True, expected_event_id=0,
            ))
            db.commit()
        with session_scope() as db:
            self.assertFalse(case_packages.set_task_membership(
                db, package_id=package_id, task_id=task_id, actor_id=self.user_id,
                included=True, expected_event_id=0,
            ))
            db.commit()
            self.assertEqual([event.task_id for event in case_packages.current_membership_events(db, package_id)], [task_id])
            event_rows = db.scalars(
                select(CasePackageTaskEvent)
                .where(CasePackageTaskEvent.case_package_id == package_id)
                .order_by(CasePackageTaskEvent.id)
            ).all()
            self.assertEqual([(event.action, event.actor_id) for event in event_rows], [("added", self.user_id)])
            added_event_id = event_rows[-1].id
        with session_scope() as db:
            self.assertTrue(case_packages.set_task_membership(
                db, package_id=package_id, task_id=task_id, actor_id=self.user_id,
                included=False, expected_event_id=added_event_id,
            ))
            db.commit()
            removed_event_id = db.scalar(select(func.max(CasePackageTaskEvent.id)).where(
                CasePackageTaskEvent.case_package_id == package_id,
                CasePackageTaskEvent.task_id == task_id,
            ))
        with session_scope() as db:
            self.assertFalse(case_packages.set_task_membership(
                db, package_id=package_id, task_id=task_id, actor_id=self.user_id,
                included=False, expected_event_id=added_event_id,
            ))
            db.commit()
            self.assertEqual(case_packages.current_membership_events(db, package_id), [])
        with session_scope() as db:
            self.assertTrue(case_packages.set_task_membership(
                db, package_id=package_id, task_id=task_id, actor_id=self.user_id,
                included=True, expected_event_id=removed_event_id,
            ))
            db.commit()
            actions = db.scalars(
                select(CasePackageTaskEvent.action)
                .where(CasePackageTaskEvent.case_package_id == package_id)
                .order_by(CasePackageTaskEvent.id)
            ).all()
            self.assertEqual(actions, ["added", "removed", "added"])

    def test_stale_add_replay_cannot_restore_removed_membership(self) -> None:
        package_id, task_id = self.package_id, self.task_ids[0]
        with session_scope() as db:
            case_packages.set_task_membership(
                db, package_id=package_id, task_id=task_id, actor_id=self.user_id,
                included=True, expected_event_id=0,
            )
            db.commit()
        with session_scope() as db:
            added_id = db.scalar(select(func.max(CasePackageTaskEvent.id)).where(
                CasePackageTaskEvent.case_package_id == package_id,
                CasePackageTaskEvent.task_id == task_id,
            ))
            case_packages.set_task_membership(
                db, package_id=package_id, task_id=task_id, actor_id=self.user_id,
                included=False, expected_event_id=added_id,
            )
            db.commit()
        with session_scope() as db:
            with self.assertRaises(case_packages.CasePackageConflict):
                case_packages.set_task_membership(
                    db, package_id=package_id, task_id=task_id, actor_id=self.user_id,
                    included=True, expected_event_id=0,
                )
            db.rollback()
            self.assertEqual(case_packages.current_membership_events(db, package_id), [])

    def test_task_backlinks_follow_latest_event_per_package_and_ignore_case_match(self) -> None:
        task_id = self.task_ids[0]
        unrelated_same_case_task_id = self.task_ids[1]
        with session_scope() as db:
            second_package, _ = case_packages.create_once(
                db,
                user_id=self.user_id,
                request_key=uuid.uuid4().hex * 2,
                title="Вторая синтетическая папка",
                case_number="А00-123/2026",
            )
            unlinked_package, _ = case_packages.create_once(
                db,
                user_id=self.user_id,
                request_key=uuid.uuid4().hex * 2,
                title="Совпадающий номер без связи",
                case_number="А00-123/2026",
            )
            second_package_id = second_package.id
            unlinked_package_id = unlinked_package.id
            for package_id in (self.package_id, second_package_id):
                case_packages.set_task_membership(
                    db, package_id=package_id, task_id=task_id,
                    actor_id=self.user_id, included=True, expected_event_id=0,
                )
            db.commit()

        with session_scope() as db:
            self.assertEqual(
                {package.id for package in case_packages.active_case_packages_for_task(db, task_id)},
                {self.package_id, second_package_id},
            )
            first_added_id = db.scalar(select(func.max(CasePackageTaskEvent.id)).where(
                CasePackageTaskEvent.case_package_id == self.package_id,
                CasePackageTaskEvent.task_id == task_id,
            ))
            case_packages.set_task_membership(
                db, package_id=self.package_id, task_id=task_id,
                actor_id=self.user_id, included=False, expected_event_id=first_added_id,
            )
            db.commit()

        with session_scope() as db:
            self.assertEqual(
                [package.id for package in case_packages.active_case_packages_for_task(db, task_id)],
                [second_package_id],
            )
            removed_id = db.scalar(select(func.max(CasePackageTaskEvent.id)).where(
                CasePackageTaskEvent.case_package_id == self.package_id,
                CasePackageTaskEvent.task_id == task_id,
            ))
            case_packages.set_task_membership(
                db, package_id=self.package_id, task_id=task_id,
                actor_id=self.user_id, included=True, expected_event_id=removed_id,
            )
            db.commit()

        with session_scope() as db:
            self.assertEqual(
                {package.id for package in case_packages.active_case_packages_for_task(db, task_id)},
                {self.package_id, second_package_id},
            )
            self.assertEqual(case_packages.active_case_packages_for_task(db, unrelated_same_case_task_id), [])
            self.assertNotIn(
                unlinked_package_id,
                {package.id for package in case_packages.active_case_packages_for_task(db, task_id)},
            )

    def test_concurrent_repeated_add_creates_one_membership_transition(self) -> None:
        package_id, task_id = self.package_id, self.task_ids[1]

        def add_membership(_index: int) -> None:
            with SessionLocal() as db:
                case_packages.set_task_membership(
                    db,
                    package_id=package_id,
                    task_id=task_id,
                    actor_id=self.user_id,
                    included=True,
                    expected_event_id=0,
                )
                db.commit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(add_membership, range(2)))
        with session_scope() as db:
            count = db.scalar(
                select(func.count()).select_from(CasePackageTaskEvent).where(
                    CasePackageTaskEvent.case_package_id == package_id,
                    CasePackageTaskEvent.task_id == task_id,
                    CasePackageTaskEvent.action == "added",
                )
            )
            self.assertEqual(count, 1)

    def test_membership_event_table_rejects_update_delete_and_truncate(self) -> None:
        with session_scope() as db:
            case_packages.set_task_membership(
                db,
                package_id=self.package_id,
                task_id=self.task_ids[0],
                actor_id=self.user_id,
                included=True,
                expected_event_id=0,
            )
            db.commit()
            event_id = db.scalar(select(CasePackageTaskEvent.id).where(
                CasePackageTaskEvent.case_package_id == self.package_id
            ))
        for statement in (
            text("UPDATE case_package_task_event SET action='removed' WHERE id=:id"),
            text("DELETE FROM case_package_task_event WHERE id=:id"),
            text("TRUNCATE case_package_task_event"),
        ):
            with self.subTest(statement=str(statement).split()[0]), engine.begin() as connection:
                with self.assertRaises(DBAPIError):
                    connection.execute(statement, {"id": event_id})

    def test_assignment_membership_is_explicit_and_keeps_same_card_drafts_separate(self) -> None:
        first_id, second_id, card_id = self._create_same_case_assignments()
        with session_scope() as db:
            before = [
                (row.debtor_card_id, row.source_task_id, row.case_number, row.status,
                 dict(row.values or {}), row.version)
                for row in (db.get(AssignmentDraft, first_id), db.get(AssignmentDraft, second_id))
            ]
            self.assertTrue(all(row.debtor_card_id == card_id for row in (
                db.get(AssignmentDraft, first_id), db.get(AssignmentDraft, second_id)
            )))
            for assignment_id in (first_id, second_id):
                self.assertTrue(case_packages.set_assignment_membership(
                    db, package_id=self.package_id, assignment_id=assignment_id,
                    actor_id=self.user_id, included=True, expected_event_id=0,
                ))
            db.commit()
        with session_scope() as db:
            active = case_packages.current_assignment_membership_events(db, self.package_id)
            self.assertEqual({event.assignment_draft_id for event in active}, {first_id, second_id})
            self.assertEqual(
                {package.id for package in case_packages.active_case_packages_for_assignment(db, first_id)},
                {self.package_id},
            )
            after = [
                (row.debtor_card_id, row.source_task_id, row.case_number, row.status,
                 dict(row.values or {}), row.version)
                for row in (db.get(AssignmentDraft, first_id), db.get(AssignmentDraft, second_id))
            ]
            self.assertEqual(after, before)
            first_added_id = next(event.id for event in active if event.assignment_draft_id == first_id)
            events = db.scalars(
                select(CasePackageAssignmentEvent)
                .where(CasePackageAssignmentEvent.case_package_id == self.package_id)
                .order_by(CasePackageAssignmentEvent.id)
            ).all()
            self.assertEqual(
                [(event.assignment_draft_id, event.action, event.actor_id) for event in events],
                [(first_id, "added", self.user_id), (second_id, "added", self.user_id)],
            )
        with session_scope() as db:
            case_packages.set_assignment_membership(
                db, package_id=self.package_id, assignment_id=first_id,
                actor_id=self.user_id, included=False, expected_event_id=first_added_id,
            )
            db.commit()
        with session_scope() as db:
            active_ids = {
                event.assignment_draft_id
                for event in case_packages.current_assignment_membership_events(db, self.package_id)
            }
            self.assertEqual(active_ids, {second_id})
            history = case_packages.assignment_membership_history_page(
                db, self.package_id, offset=0, limit=10
            )
            self.assertEqual(
                [(event.action, event.actor_id) for event in history],
                [("removed", self.user_id), ("added", self.user_id), ("added", self.user_id)],
            )

    def test_stale_assignment_replay_cannot_restore_removed_membership(self) -> None:
        assignment_id, _second_id, _card_id = self._create_same_case_assignments()
        with session_scope() as db:
            case_packages.set_assignment_membership(
                db, package_id=self.package_id, assignment_id=assignment_id,
                actor_id=self.user_id, included=True, expected_event_id=0,
            )
            db.commit()
        with session_scope() as db:
            added_id = db.scalar(select(func.max(CasePackageAssignmentEvent.id)).where(
                CasePackageAssignmentEvent.case_package_id == self.package_id,
                CasePackageAssignmentEvent.assignment_draft_id == assignment_id,
            ))
            case_packages.set_assignment_membership(
                db, package_id=self.package_id, assignment_id=assignment_id,
                actor_id=self.user_id, included=False, expected_event_id=added_id,
            )
            db.commit()
        with session_scope() as db:
            with self.assertRaises(case_packages.CasePackageConflict):
                case_packages.set_assignment_membership(
                    db, package_id=self.package_id, assignment_id=assignment_id,
                    actor_id=self.user_id, included=True, expected_event_id=added_id,
                )
            db.rollback()
            self.assertEqual(db.scalar(select(func.count()).select_from(CasePackageAssignmentEvent).where(
                CasePackageAssignmentEvent.case_package_id == self.package_id,
                CasePackageAssignmentEvent.assignment_draft_id == assignment_id,
            )), 2)

    def test_assignment_package_document_page_uses_current_explicit_memberships(self) -> None:
        assignment_id, _second_id, _card_id = self._create_same_case_assignments()
        with session_scope() as db:
            case_packages.set_assignment_membership(
                db, package_id=self.package_id, assignment_id=assignment_id,
                actor_id=self.user_id, included=True, expected_event_id=0,
            )
            for task_id in self.task_ids:
                case_packages.set_task_membership(
                    db, package_id=self.package_id, task_id=task_id,
                    actor_id=self.user_id, included=True, expected_event_id=0,
                )
            db.get(Task, max(self.task_ids)).document.stored_path = (
                f"/tmp/synthetic-unsupported-{uuid.uuid4().hex}.docx"
            )
            db.commit()
        with session_scope() as db:
            result = case_packages.assignment_package_document_page(
                db, assignment_id=assignment_id, package_id=self.package_id, page=1, page_size=1,
            )
            self.assertTrue(result["package_exists"])
            self.assertTrue(result["assignment_active"])
            self.assertEqual(result["total"], 2)
            self.assertEqual(result["page_count"], 2)
            self.assertEqual(len(result["rows"]), 1)
            self.assertEqual(result["rows"][0]["task_id"], max(self.task_ids))
            self.assertTrue(all(row["filename"].startswith("synthetic-act-") for row in result["rows"]))
            self.assertFalse(result["rows"][0]["preview_supported"])
            expected_event_id = db.scalar(select(func.max(CasePackageTaskEvent.id)).where(
                CasePackageTaskEvent.case_package_id == self.package_id,
                CasePackageTaskEvent.task_id == max(self.task_ids),
            ))
            case_packages.set_task_membership(
                db, package_id=self.package_id, task_id=max(self.task_ids),
                actor_id=self.user_id, included=False, expected_event_id=expected_event_id,
            )
            db.commit()
        with session_scope() as db:
            result = case_packages.assignment_package_document_page(
                db, assignment_id=assignment_id, package_id=self.package_id, page=1,
            )
            self.assertEqual(result["total"], 1)
            self.assertEqual([row["task_id"] for row in result["rows"]], [min(self.task_ids)])
            assignment_event_id = db.scalar(select(func.max(CasePackageAssignmentEvent.id)).where(
                CasePackageAssignmentEvent.case_package_id == self.package_id,
                CasePackageAssignmentEvent.assignment_draft_id == assignment_id,
            ))
            case_packages.set_assignment_membership(
                db, package_id=self.package_id, assignment_id=assignment_id,
                actor_id=self.user_id, included=False, expected_event_id=assignment_event_id,
            )
            db.commit()
        with session_scope() as db:
            result = case_packages.assignment_package_document_page(
                db, assignment_id=assignment_id, package_id=self.package_id, page=1,
            )
            self.assertFalse(result["assignment_active"])
            self.assertEqual(result["total"], 1)

    def test_concurrent_assignment_add_creates_one_membership_event(self) -> None:
        assignment_id, _second_id, _card_id = self._create_same_case_assignments()

        def add_membership(_index: int) -> None:
            with SessionLocal() as db:
                case_packages.set_assignment_membership(
                    db, package_id=self.package_id, assignment_id=assignment_id,
                    actor_id=self.user_id, included=True, expected_event_id=0,
                )
                db.commit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(add_membership, range(2)))
        with session_scope() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(
                CasePackageAssignmentEvent
            ).where(
                CasePackageAssignmentEvent.case_package_id == self.package_id,
                CasePackageAssignmentEvent.assignment_draft_id == assignment_id,
                CasePackageAssignmentEvent.action == "added",
            )), 1)

    def test_assignment_membership_history_rejects_mutation_and_truncate(self) -> None:
        assignment_id, _second_id, _card_id = self._create_same_case_assignments()
        with session_scope() as db:
            case_packages.set_assignment_membership(
                db, package_id=self.package_id, assignment_id=assignment_id,
                actor_id=self.user_id, included=True, expected_event_id=0,
            )
            db.commit()
        for statement in (
            text("UPDATE case_package_assignment_event SET action='removed' WHERE case_package_id=:id"),
            text("DELETE FROM case_package_assignment_event WHERE case_package_id=:id"),
            text("TRUNCATE case_package_assignment_event"),
        ):
            with self.subTest(statement=str(statement).split()[0]), engine.begin() as connection:
                with self.assertRaises(DBAPIError):
                    connection.execute(statement, {"id": self.package_id})

    def test_package_creation_replays_only_exact_owner_and_payload(self) -> None:
        request_key = uuid.uuid4().hex * 2
        with session_scope() as db:
            first, created = case_packages.create_once(
                db,
                user_id=self.user_id,
                request_key=request_key,
                title="Пакет повтора",
                case_number="А00-123/2026",
            )
            db.commit()
            first_id = first.id
        with session_scope() as db:
            replay, created = case_packages.create_once(
                db,
                user_id=self.user_id,
                request_key=request_key,
                title="Пакет повтора",
                case_number="А00-123/2026",
            )
            self.assertFalse(created)
            self.assertEqual(replay.id, first_id)
            with self.assertRaises(case_packages.CasePackageConflict):
                case_packages.create_once(
                    db,
                    user_id=self.user_id,
                    request_key=request_key,
                    title="Другой пакет",
                    case_number="А00-123/2026",
                )
