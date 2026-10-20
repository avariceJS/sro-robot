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
    CasePackage,
    CasePackageTaskEvent,
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
                "d12f6a84c3e1",
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
                    scenario="person",
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
