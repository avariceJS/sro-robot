"""Disposable PostgreSQL boundaries for mixed preview and applicant provenance."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import os
import unittest

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.base import session_scope
from app.db.models import (
    AssignmentDraft, AssignmentDraftRevision, DebtorCardDraft, Document,
    IntakeSource, Task, TaskLocalFinalizationAttempt, TaskRoleAttestation, TaskStatus, User,
)
from app.services import tasks
from tests.postgres_safety import assert_disposable_postgres


@unittest.skipUnless(os.environ.get("SRO_TEST_POSTGRES") == "1", "requires disposable PostgreSQL")
class MixedLifecyclePostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert_disposable_postgres()
        suffix = os.urandom(8).hex()
        with session_scope() as db:
            user = User(login=f"mixed-{suffix}", full_name="Synthetic staff",
                        password_hash="synthetic-only", sro_login="synthetic", is_active=True)
            db.add(user)
            db.flush()
            document = Document(filename="synthetic.png", stored_path="/synthetic/never-read.png",
                                content_hash=os.urandom(32).hex(), source=IntakeSource.batch,
                                uploaded_by_id=user.id)
            db.add(document)
            db.flush()
            self.user_id = user.id
            self.document_id = document.id

    def make_task(self, *, status: TaskStatus = TaskStatus.needs_review,
                  mode: str = "live_session", purpose: str = "legacy") -> int:
        with session_scope() as db:
            task = Task(
                document_id=self.document_id, scenario="person_card", profile="court_ruling",
                status=status, recognized={"_mixed_route_v1": {"registry": "persons",
                    "reason": "explicit_person_debtor"}}, values={}, assignee_id=self.user_id,
                approved_by_id=self.user_id if status == TaskStatus.awaiting_preview else None,
                preview_mode=mode, preview_purpose=purpose, dry_run=True,
                preview_values_sha256=tasks.values_digest({}) if mode == "durable_closed" else None,
                preview_schema_version=2 if mode == "durable_closed" else None,
                preview_screenshot_sha256=[], screenshots=[], browser_decision="",
            )
            db.add(task)
            db.flush()
            return task.id

    def test_migration_guard_keeps_legacy_unapproved_transition_closed(self) -> None:
        legacy_id = self.make_task()
        with self.assertRaises(IntegrityError):
            with session_scope() as db:
                db.execute(text("UPDATE task SET status='writing' WHERE id=:id"), {"id": legacy_id})
        durable_id = self.make_task(mode="durable_closed", purpose="new_record_review")
        with self.assertRaises(IntegrityError):
            with session_scope() as db:
                db.execute(text("UPDATE task SET status='writing' WHERE id=:id"), {"id": durable_id})
        with session_scope() as db:
            task = db.get(Task, durable_id)
            task.preview_proposal_sha256 = "a" * 64
            task.preview_source_occurrence = {"kind": "explicit_person_debtor"}
            db.flush()
            db.execute(text("UPDATE task SET status='writing' WHERE id=:id"), {"id": durable_id})
        with session_scope() as db:
            self.assertEqual(db.get(Task, durable_id).status, TaskStatus.writing)

    def test_concurrent_employee_save_request_and_worker_claim_are_single_use(self) -> None:
        task_id = self.make_task(status=TaskStatus.awaiting_preview,
                                 mode="durable_closed", purpose="new_record_review")
        token = "a" * 64
        request_id = "b" * 32

        def request() -> bool:
            with session_scope() as db:
                task = db.get(Task, task_id)
                return tasks.request_durable_sro_save(db, task, self.user_id, token, request_id)

        with (patch.object(tasks, "_preview_snapshot_matches", return_value=True),
              patch.object(tasks, "_owned_principal_matches", return_value=True),
              patch.object(tasks, "_save_readiness_errors", return_value=[]),
              patch.object(tasks, "_role_attestation_matches", return_value=True),
              patch.object(tasks, "approval_matches", return_value=True)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(lambda _: request(), range(2))), [False, True])

            def claim() -> bool:
                with session_scope() as db:
                    task = db.get(Task, task_id)
                    return tasks.claim_durable_sro_save(db, task, self.user_id, token)

            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(lambda _: claim(), range(2))), [False, True])
        with session_scope() as db:
            task = db.get(Task, task_id)
            self.assertEqual(task.browser_decision, "save_started")
            self.assertFalse(request())

    def test_source_bound_review_candidates_can_enter_technical_preparation(self) -> None:
        for scenario, registry, reason in (
            ("person_card", "persons", "review_person_candidate"),
            ("entity_review", "organizations", "review_entity_candidate"),
        ):
            with self.subTest(reason=reason):
                task_id = self.make_task(mode="durable_closed", purpose="new_record_review")
                with session_scope() as db:
                    task = db.get(Task, task_id)
                    task.scenario = scenario
                    task.recognized = {"_mixed_route_v1": {"registry": registry, "reason": reason}}
                    task.preview_proposal_sha256 = "a" * 64
                    task.preview_source_occurrence = {"kind": reason, "evidence_sha256": "b" * 64}
                    db.flush()
                    task.status = TaskStatus.writing
                    db.flush()
                with session_scope() as db:
                    self.assertEqual(db.get(Task, task_id).status, TaskStatus.writing)

    def test_applicant_revision_preserves_role_and_allows_later_debtor_binding(self) -> None:
        task_id = self.make_task()
        with session_scope() as db:
            draft = AssignmentDraft(
                source_task_id=task_id, source_role="applicant", debtor_card_id=None,
                case_number="А99-1/2099", status="awaiting_information",
                values={"applicant": "Synthetic Applicant", "practitioner": ""},
                created_by_id=self.user_id, updated_by_id=self.user_id,
            )
            db.add(draft)
            db.flush()
            draft_id = draft.id
            revisions = db.scalars(select(AssignmentDraftRevision).where(
                AssignmentDraftRevision.assignment_draft_id == draft_id)).all()
            self.assertEqual(len(revisions), 1)
            self.assertEqual(revisions[0].snapshot["source_role"], "applicant")
            self.assertIsNone(revisions[0].snapshot["debtor_card_id"])
        with self.assertRaises(IntegrityError):
            with session_scope() as db:
                db.add(AssignmentDraft(
                    source_task_id=task_id, source_role="applicant", debtor_card_id=None,
                    case_number="А99-1/2099", status="draft", values={},
                    created_by_id=self.user_id, updated_by_id=self.user_id,
                ))
                db.flush()
        with session_scope() as db:
            card = DebtorCardDraft(
                entity_kind="person", display_name="Synthetic Debtor", inn=None,
                values={}, preparation_state="reviewed", source_task_id=None,
                created_by_id=self.user_id, updated_by_id=self.user_id,
            )
            db.add(card)
            db.flush()
            draft = db.get(AssignmentDraft, draft_id)
            draft.debtor_card_id = card.id
            draft.version += 1
            db.flush()
            self.assertEqual(draft.source_role, "applicant")
            revisions = db.scalars(select(AssignmentDraftRevision).where(
                AssignmentDraftRevision.assignment_draft_id == draft_id)
                .order_by(AssignmentDraftRevision.revision)).all()
            self.assertEqual(len(revisions), 2)
            self.assertEqual(revisions[1].snapshot["debtor_card_id"], card.id)

    def test_local_finalization_attempt_is_attributed_and_append_only(self) -> None:
        task_id = self.make_task()
        with session_scope() as db:
            attempt = TaskLocalFinalizationAttempt(
                task_id=task_id, actor_id=self.user_id, outcome="failed",
            )
            db.add(attempt)
            db.flush()
            attempt_id = attempt.id
        with self.assertRaises(IntegrityError):
            with session_scope() as db:
                db.execute(text("UPDATE task_local_finalization_attempt SET outcome='succeeded' WHERE id=:id"),
                           {"id": attempt_id})
        with session_scope() as db:
            attempt = db.get(TaskLocalFinalizationAttempt, attempt_id)
            self.assertEqual((attempt.task_id, attempt.actor_id, attempt.outcome),
                             (task_id, self.user_id, "failed"))

    def test_concurrent_refresh_request_claim_and_unknown_hold(self) -> None:
        task_id = self.make_task(status=TaskStatus.writing,
                                 mode="durable_closed", purpose="new_record_review")
        with session_scope() as db:
            task = db.get(Task, task_id)
            task.browser_decision = "preparing"
            db.flush()
            self.assertTrue(tasks.claim_owned_sro_add(db, task, self.user_id, "synthetic"))
        with session_scope() as db:
            task = db.get(Task, task_id)
            self.assertTrue(tasks.complete_owned_sro_add(
                db, task, self.user_id, "synthetic",
                f"https://sroarbitr.sro-service.ru/register/person_card.aspx?id=pg-refresh-{task_id}",
                {"last_name": "* Фамилия не указана", "first_name": ""},
            ))
            task.status = TaskStatus.awaiting_preview
            task.browser_decision = ""
        new_values = {"last_name": "Петров", "first_name": "Пётр", "case_number": "А99-1/2099"}
        token = "a" * 64
        first_request = "b" * 32

        def request(request_id: str) -> bool:
            with session_scope() as db:
                return tasks.request_owned_preview_refresh(
                    db, db.get(Task, task_id), self.user_id, token, new_values, request_id,
                )

        with (patch.object(tasks, "_owned_principal_matches", return_value=True),
              patch.object(tasks, "_preview_snapshot_matches", return_value=True),
              patch.object(tasks, "_refresh_identity", return_value=("петров", "пётр"))):
            self.assertTrue(request(first_request))
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(request, (first_request, "c" * 32))), [False, True])
            self.assertTrue(request(first_request))

            def claim() -> bool:
                with session_scope() as db:
                    return tasks.claim_owned_preview_refresh(
                        db, db.get(Task, task_id), self.user_id, first_request,
                    )

            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(lambda _: claim(), range(2))), [False, True])
            with session_scope() as db:
                task = db.get(Task, task_id)
                self.assertTrue(tasks.fail_owned_preview_refresh(db, task, self.user_id))
        with session_scope() as db:
            self.assertEqual(db.get(Task, task_id).preview_refresh_state, "unknown")
        with self.assertRaises(IntegrityError):
            with session_scope() as db:
                db.execute(text("UPDATE task SET preview_refresh_state='requested' WHERE id=:id"), {"id": task_id})

    def test_proposal_and_role_attestation_custody_are_immutable(self) -> None:
        task_id = self.make_task(mode="durable_closed", purpose="new_record_review")
        with session_scope() as db:
            task = db.get(Task, task_id)
            task.preview_proposal_sha256 = "a" * 64
            task.preview_source_occurrence = {"kind": "explicit_person_debtor"}
            db.add(TaskRoleAttestation(
                task_id=task_id, actor_id=self.user_id, role="debtor",
                preview_token="b" * 64, proposal_sha256="a" * 64,
                occurrence_sha256="c" * 64, values_sha256="d" * 64,
            ))
        with self.assertRaises(IntegrityError):
            with session_scope() as db:
                db.execute(text("UPDATE task SET preview_proposal_sha256=:digest WHERE id=:id"),
                           {"digest": "e" * 64, "id": task_id})
        with self.assertRaises(IntegrityError):
            with session_scope() as db:
                db.execute(text("UPDATE task_role_attestation SET values_sha256=:digest WHERE task_id=:id"),
                           {"digest": "e" * 64, "id": task_id})
