"""Synthetic boundaries for closed previews and applicant-only assignments."""
from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone
import hashlib
import unittest
from unittest.mock import MagicMock, patch

from app.db.models import TaskStatus
from app.services import assignments, tasks


def _task(*, route: str = "persons", reason: str = "explicit_person_debtor") -> SimpleNamespace:
    return SimpleNamespace(
        id=7, status=TaskStatus.needs_review, scenario="person_card",
        recognized={tasks.MIXED_ROUTE_KEY: {"registry": route, "reason": reason}},
        values={}, approved_by_id=None, browser_decision="", screenshots=[],
        dry_run=True, preview_mode="live_session", preview_save_enabled=False,
        preview_purpose="legacy",
        preview_values_sha256=None, preview_schema_version=None,
        preview_destination={}, preview_screenshot_sha256=[],
        preview_refresh_state="idle", preview_refresh_actor_id=None,
        preview_refresh_request_id=None, preview_refresh_previous_token=None,
        preview_refresh_values_sha256=None, preview_refresh_generation=0,
        owned_add_state="not_started", owned_add_actor_id=None,
        owned_add_started_at=None, owned_add_bound_at=None,
        owned_draft_url=None, owned_principal_sha256=None,
        owned_baseline={}, owned_baseline_sha256=None,
        owned_baseline_schema_version=None,
        preview_proposal_sha256=None, preview_source_occurrence={},
    )


class MixedLifecycleTests(unittest.TestCase):
    def test_explicit_applicant_ocr_creates_internal_draft_without_prepare_intent(self) -> None:
        task = _task(route="assignments", reason="explicit_applicant")
        task.status = TaskStatus.recognizing
        task.assignee_id = 1
        task.case_number = ""
        card = SimpleNamespace(
            case_number=SimpleNamespace(value="А99-1/2099"),
            applicant_evidence={"role_source": "applicant_role:v1", "name": "Synthetic Applicant",
                                "case_number": "А99-1/2099"},
            review_suggestions=[],
        )
        db = MagicMock()
        with (patch.object(tasks, "lock_task_for_update"),
              patch.object(tasks, "transition_task_status", side_effect=lambda current, status: setattr(current, "status", status)),
              patch.object(tasks, "log"),
              patch.object(tasks, "card_to_fields", return_value={}),
              patch("app.services.kad_import.check_case"),
              patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(
                  as_dict=lambda: {"registry": "assignments", "subject_kind": "applicant", "reason": "explicit_applicant"})),
              patch.object(assignments, "prepare_debtor_card_from_ocr"),
              patch.object(assignments, "create_applicant_assignment_draft") as create):
            self.assertTrue(tasks.save_recognized(db, task, card))
        create.assert_called_once_with(db, task.id, 1)

    def test_only_explicit_person_debtor_is_admitted_to_unsaved_preview(self) -> None:
        for route, reason, expected in (
            ("persons", "explicit_person_debtor", True),
            ("persons", "review_person_candidate", False),
            ("organizations", "explicit_entity_debtor", False),
            ("assignments", "explicit_applicant", False),
        ):
            with self.subTest(reason=reason):
                task = _task(route=route, reason=reason)
                with (patch.object(tasks, "lock_task_for_update"), patch.object(tasks, "log"),
                      patch.object(tasks, "_mixed_preview_proposal", return_value=({}, {"kind": reason}, reason) if expected else None),
                      patch("app.services.fields.scenario_of", return_value=SimpleNamespace(
                          supports_prepare=True, validate=lambda card: [],
                          card_class=object, card_from_values=lambda values, base: base)),
                      patch("sro_robot.core.models.card_from_fields")):
                    self.assertEqual(tasks.admit_mixed_preview(MagicMock(), task), expected)
                self.assertEqual(task.status, TaskStatus.writing if expected else TaskStatus.needs_review)
                self.assertEqual(task.preview_save_enabled, False)

    def test_save_requires_exact_closed_snapshot_and_prior_employee_confirmation(self) -> None:
        task = _task()
        task.status = TaskStatus.awaiting_preview
        task.preview_mode = "durable_closed"
        task.preview_purpose = "new_record_review"
        token = "a" * 64
        db = MagicMock()
        with (patch.object(tasks, "lock_task_for_update"),
              patch.object(tasks, "_preview_snapshot_matches", return_value=True),
              patch.object(tasks, "_owned_principal_matches", return_value=True),
              patch.object(tasks, "_save_readiness_errors", return_value=[]),
              patch.object(tasks, "_role_attestation_matches", return_value=True),
              patch.object(tasks, "approval_matches", return_value=False),
              patch.object(tasks, "log")):
            self.assertFalse(tasks.request_durable_sro_save(db, task, 1, token, "b" * 32))
            self.assertFalse(task.preview_save_enabled)
        with (patch.object(tasks, "lock_task_for_update"),
              patch.object(tasks, "_preview_snapshot_matches", return_value=True),
              patch.object(tasks, "_owned_principal_matches", return_value=True),
              patch.object(tasks, "_save_readiness_errors", return_value=[]),
              patch.object(tasks, "_role_attestation_matches", return_value=True),
              patch.object(tasks, "approval_matches", return_value=True),
              patch.object(tasks, "log")):
            task.approved_by_id = 1
            self.assertTrue(tasks.request_durable_sro_save(db, task, 1, token, "b" * 32))
            self.assertFalse(tasks.request_durable_sro_save(db, task, 1, token, "b" * 32))
            self.assertEqual(task.browser_decision, "save_requested")
            self.assertTrue(task.preview_save_enabled)

    def test_new_record_purpose_is_required_for_durable_save_request_and_claim(self) -> None:
        task = _task()
        task.status = TaskStatus.awaiting_preview
        task.preview_mode = "durable_closed"
        db = MagicMock()
        with patch.object(tasks, "lock_task_for_update"):
            self.assertFalse(tasks.request_durable_sro_save(db, task, 1, "a" * 64, "b" * 32))
            task.preview_save_enabled = True
            task.browser_decision = "save_requested"
            self.assertFalse(tasks.claim_durable_sro_save(db, task, 1, "a" * 64))

    def test_closed_preview_rejects_changed_screenshot_bytes(self) -> None:
        task = _task()
        task.status = TaskStatus.awaiting_preview
        task.preview_mode = "durable_closed"
        task.preview_purpose = "new_record_review"
        task.screenshots = ["/synthetic/shot.png"]
        task.preview_values_sha256 = tasks.values_digest({})
        task.preview_schema_version = 2
        task.preview_screenshot_sha256 = [hashlib.sha256(b"original").hexdigest()]
        token = tasks.preview_token(task)
        event = SimpleNamespace(user_id=1, payload={"preview_token": token},
                                created_at=datetime.now(timezone.utc))
        db = MagicMock()
        db.scalar.side_effect = [event, datetime.now(timezone.utc)]
        with patch("app.services.artifacts.read_screenshot", return_value=b"changed"):
            self.assertFalse(tasks._preview_snapshot_matches(db, task, 1, token))
        self.assertEqual(db.scalar.call_count, 0)

    def test_applicant_draft_is_internal_cardless_and_source_bound(self) -> None:
        task = _task(route="assignments", reason="explicit_applicant")
        task.recognized[tasks.APPLICANT_EVIDENCE_KEY] = {
            "role_source": "applicant_role:v1", "name": "Макетова Анна",
            "case_number": "А99-1/2099",
        }
        task.case_number = "А99-1/2099"
        db = MagicMock()
        db.scalar.side_effect = [task, None]
        with (patch.object(assignments, "_checked_case_number", side_effect=lambda value: value),
              patch("sro_robot.core.models.card_from_fields"),
              patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(registry="assignments", reason="explicit_applicant"))):
            draft = assignments.create_applicant_assignment_draft(db, 7, 1)
        self.assertIsNone(draft.debtor_card_id)
        self.assertEqual(draft.source_role, "applicant")
        self.assertEqual(draft.source_task_id, 7)
        self.assertEqual(draft.status, "awaiting_information")
        self.assertEqual(draft.values["applicant"], "Макетова Анна")

    def test_local_projection_failure_preserves_verified_external_saved_receipt(self) -> None:
        task = _task()
        task.status = TaskStatus.awaiting_preview
        task.preview_mode = "durable_closed"
        task.preview_purpose = "new_record_review"
        task.browser_decision = "save_started"
        task.record_url = ""
        task.owned_draft_url = "https://synthetic.invalid/card?id=1"
        task.error = ""
        task.local_finalization_state = "not_applicable"
        result = SimpleNamespace(ok=True, saved=True, record_url="https://synthetic.invalid/card?id=1")
        db = MagicMock()
        with (patch.object(tasks, "lock_task_for_update"), patch.object(tasks, "log"),
              patch.object(tasks, "_verified_person_record_url", return_value=True),
              patch.object(tasks, "_owned_binding_matches", return_value=True),
              patch.object(tasks, "_role_attestation_matches", return_value=True),
              patch.object(assignments, "finalize_saved_sro_card_and_assignment", side_effect=RuntimeError("local failure"))):
            self.assertTrue(tasks.complete_sro_preview(db, task, result, 1))
        self.assertEqual(task.browser_decision, "saved")
        self.assertFalse(task.dry_run)
        self.assertEqual(task.record_url, result.record_url)
        self.assertEqual(task.local_finalization_state, "failed")
        self.assertIn("Повтор Save запрещён", task.error)
        attempt = db.add.call_args.args[0]
        self.assertEqual((attempt.task_id, attempt.actor_id, attempt.outcome), (task.id, 1, "failed"))

    def test_saved_receipt_url_is_bound_to_exact_card_scenario(self) -> None:
        person = _task()
        firm = _task()
        firm.scenario = "entity_review"
        person_url = "https://sroarbitr.sro-service.ru/register/person_card.aspx?id=synthetic"
        firm_url = "https://sroarbitr.sro-service.ru/register/firm_card.aspx?id=synthetic"
        self.assertTrue(tasks._verified_task_record_url(person, person_url))
        self.assertTrue(tasks._verified_task_record_url(firm, firm_url))
        self.assertFalse(tasks._verified_task_record_url(person, firm_url))
        self.assertFalse(tasks._verified_task_record_url(firm, person_url))
        self.assertFalse(tasks._verified_task_record_url(firm, firm_url + "&action=edit"))

    def test_firm_destination_is_bound_to_durable_preview_token(self) -> None:
        firm = _task(route="organizations", reason="explicit_entity_debtor")
        firm.scenario = "entity_review"
        firm.preview_mode = "durable_closed"
        firm.preview_purpose = "new_record_review"
        firm.preview_schema_version = 2
        firm.preview_screenshot_sha256 = []
        firm.preview_destination = {"opf_label": "Фирма", "opf_value": "1"}
        original = tasks.preview_token(firm)
        firm.preview_destination = {"opf_label": "Фирма", "opf_value": "2"}
        self.assertNotEqual(tasks.preview_token(firm), original)


if __name__ == "__main__":
    unittest.main()
