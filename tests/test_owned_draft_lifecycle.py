"""Synthetic one-shot Add receipt and coherent candidate admission boundaries."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.db.models import TaskRoleAttestation, TaskStatus, User
from app.services import tasks


def task() -> SimpleNamespace:
    return SimpleNamespace(
        id=41, status=TaskStatus.writing, scenario="person_card", dry_run=True,
        preview_mode="durable_closed", preview_purpose="new_record_review",
        preview_save_enabled=False,
        browser_decision="preparing", owned_add_state="not_started",
        owned_add_actor_id=None, owned_add_started_at=None, owned_add_bound_at=None,
        owned_draft_url=None, owned_principal_sha256=None, owned_baseline={},
        owned_baseline_sha256=None, owned_baseline_schema_version=None,
        screenshots=[], recognized={}, values={}, approved_by_id=None,
        preview_source_occurrence={"kind": "review_person_candidate"},
        preview_proposal_sha256="a" * 64, preview_values_sha256="b" * 64,
        preview_schema_version=2, preview_screenshot_sha256=[], preview_destination={},
        preview_refresh_state="idle", preview_refresh_actor_id=None,
        preview_refresh_request_id=None, preview_refresh_previous_token=None,
        preview_refresh_values_sha256=None, preview_refresh_generation=0,
    )


def suggestion(name: str, inn: str = "") -> dict:
    last, first, middle = name.split()
    fields = {
        part: {"value": value, "source_variant": 0, "source_start": 10,
               "source_end": 20, "confidence": 0.8}
        for part, value in (("last_name", last), ("first_name", first), ("middle_name", middle))
    }
    if inn:
        fields["inn"] = {"value": inn}
    return {"kind": "application_person", "copyable_fields": ["last_name", "first_name", "middle_name", "inn"], "fields": fields}


class OwnedDraftLifecycleTests(unittest.TestCase):
    def test_claim_is_one_shot_and_unknown_add_cannot_retry(self) -> None:
        current = task()
        db = MagicMock()
        db.get.return_value = SimpleNamespace(is_active=True, sro_login="staff@example.test")
        db.scalar.return_value = datetime.now(timezone.utc)
        with patch.object(tasks, "lock_task_for_update"):
            self.assertTrue(tasks.claim_owned_sro_add(db, current, 7, " STAFF@example.test "))
            self.assertEqual(current.owned_add_state, "started")
            self.assertFalse(tasks.claim_owned_sro_add(db, current, 7, "staff@example.test"))
            self.assertTrue(tasks.fail_owned_sro_add(db, current, 7))
            self.assertEqual(current.owned_add_state, "unknown")
            self.assertFalse(tasks.claim_owned_sro_add(db, current, 7, "staff@example.test"))

    def test_exact_guid_and_baseline_are_bound_before_fill_and_retained_on_failure(self) -> None:
        current = task()
        current.owned_add_state = "started"
        current.owned_add_actor_id = 7
        current.owned_add_started_at = datetime.now(timezone.utc)
        current.owned_principal_sha256 = tasks._sro_principal_digest("staff@example.test")
        db = MagicMock()
        db.scalar.return_value = datetime.now(timezone.utc)
        url = "https://sroarbitr.sro-service.ru/register/person_card.aspx?id=synthetic"
        baseline = {"last_name": "* Фамилия не указана", "first_name": "", "inn": ""}
        with patch.object(tasks, "lock_task_for_update"):
            self.assertTrue(tasks.complete_owned_sro_add(db, current, 7, "staff@example.test", url, baseline))
            self.assertEqual(current.owned_add_state, "bound")
            self.assertEqual(current.owned_draft_url, url)
            self.assertEqual(current.owned_baseline_sha256, tasks._owned_baseline_digest(baseline))
            self.assertFalse(tasks.fail_owned_sro_add(db, current, 7))
            self.assertFalse(tasks.complete_owned_sro_add(db, current, 7, "staff@example.test", url, baseline))

    def test_other_sro_principal_cannot_create_role_attestation(self) -> None:
        current = task()
        current.status = TaskStatus.awaiting_preview
        current.browser_decision = ""
        db = MagicMock()
        with (patch.object(tasks, "lock_task_for_update"),
              patch.object(tasks, "_preview_snapshot_matches", return_value=True),
              patch.object(tasks, "_owned_principal_matches", return_value=False)):
            self.assertFalse(tasks.confirm_durable_preview(db, current, 8, "a" * 64))
        db.add.assert_not_called()
        self.assertIsNone(current.approved_by_id)

    def test_wrong_principal_does_not_take_correct_principals_one_shot_attestation(self) -> None:
        current = task()
        current.status = TaskStatus.awaiting_preview
        current.browser_decision = ""
        current.preview_values_sha256 = tasks.values_digest(current.values)
        current.owned_principal_sha256 = tasks._sro_principal_digest("correct@example.test")
        db = MagicMock()
        db.get.side_effect = lambda model, identifier: (
            SimpleNamespace(is_active=True, sro_login=(
                "correct@example.test" if identifier == 7 else "wrong@example.test"
            )) if model is User else None
        )
        with (patch.object(tasks, "lock_task_for_update"), patch.object(tasks, "log"),
              patch.object(tasks, "_preview_snapshot_matches", return_value=True),
              patch.object(tasks, "_owned_binding_matches", return_value=True),
              patch.object(tasks, "_save_readiness_errors", return_value=[])):
            self.assertFalse(tasks.confirm_durable_preview(db, current, 8, "a" * 64))
            db.add.assert_not_called()
            self.assertTrue(tasks.confirm_durable_preview(db, current, 7, "a" * 64))
        self.assertEqual(current.approved_by_id, 7)
        self.assertIsInstance(db.add.call_args.args[0], TaskRoleAttestation)

    def test_damaged_role_variant_does_not_veto_one_coherent_review_name(self) -> None:
        current = task()
        current.status = TaskStatus.needs_review
        current.recognized = {
            tasks.MIXED_ROUTE_KEY: {"registry": "persons", "reason": "review_person_candidate"},
            tasks.REVIEW_SUGGESTIONS_KEY: [suggestion("Иванов Иван Иванович")],
            "fio_raw": {"source": "role_ambiguous", "warning": "ФИО должника неполное или повреждено"},
        }
        with (patch("sro_robot.core.models.card_from_fields", return_value=SimpleNamespace()),
              patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(
                  registry="persons", reason="review_person_candidate"))):
            self.assertIsNotNone(tasks._mixed_preview_proposal(current))

    def test_explicit_multiple_debtors_stay_held_even_with_one_review_suggestion(self) -> None:
        current = task()
        current.status = TaskStatus.needs_review
        current.recognized = {
            tasks.MIXED_ROUTE_KEY: {"registry": "persons", "reason": "review_person_candidate"},
            tasks.REVIEW_SUGGESTIONS_KEY: [suggestion("Иванов Иван Иванович")],
            "fio_raw": {"source": "role_ambiguous", "warning": "Несколько лиц в секции должника"},
        }
        with (patch("sro_robot.core.models.card_from_fields", return_value=SimpleNamespace()),
              patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(
                  registry="persons", reason="review_person_candidate"))):
            self.assertIsNone(tasks._mixed_preview_proposal(current))

    def test_multiple_identity_groups_are_held_before_form_admission(self) -> None:
        current = task()
        current.status = TaskStatus.needs_review
        current.recognized = {
            tasks.MIXED_ROUTE_KEY: {"registry": "persons", "reason": "review_person_candidate"},
            tasks.REVIEW_SUGGESTIONS_KEY: [
                suggestion("Иванов Иван Иванович"), suggestion("Петров Пётр Петрович"),
            ],
        }
        with (patch("sro_robot.core.models.card_from_fields", return_value=SimpleNamespace()),
              patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(
                  registry="persons", reason="review_person_candidate"))):
            self.assertIsNone(tasks._mixed_preview_proposal(current))

    def test_partial_role_name_uses_its_own_source_when_suggestion_differs(self) -> None:
        current = task()
        current.status = TaskStatus.needs_review
        name_fields = {
            attr: {"value": value, "source": "role_partial", "source_variant": 0,
                   "source_start": 4, "source_end": 28}
            for attr, value in (("last_name", "Иванов"), ("first_name", "Пётр"),
                                ("middle_name", "Иванович"))
        }
        current.recognized = {
            tasks.MIXED_ROUTE_KEY: {"registry": "persons", "reason": "review_person_candidate"},
            tasks.REVIEW_SUGGESTIONS_KEY: [suggestion("Иванов Иван Иванович")],
            "fio_raw": {"value": "Иванов Пётр Иванович", "source": "role_partial",
                        "warning": "Один вариант OCR поврежден", "source_variant": 0,
                        "source_start": 4, "source_end": 28},
            **name_fields,
        }
        with patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(
            registry="persons", reason="review_person_candidate")):
            proposal = tasks._mixed_preview_proposal(current)
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal[0]["first_name"], "Пётр")
        self.assertEqual(proposal[1]["kind"], "role_partial_person_candidate")
        self.assertEqual(proposal[1]["source_start"], 4)

    def test_partial_role_fallback_requires_shared_exact_name_span(self) -> None:
        current = task()
        current.status = TaskStatus.needs_review
        current.recognized = {
            tasks.MIXED_ROUTE_KEY: {"registry": "persons", "reason": "review_person_candidate"},
            tasks.REVIEW_SUGGESTIONS_KEY: [suggestion("Иванов Иван Иванович")],
            "fio_raw": {"value": "Иванов Пётр Иванович", "source": "role_partial",
                        "source_variant": 0, "source_start": 4, "source_end": 28},
            "last_name": {"value": "Иванов", "source_variant": 0, "source_start": 4, "source_end": 28},
            "first_name": {"value": "Пётр", "source_variant": 0, "source_start": 5, "source_end": 28},
            "middle_name": {"value": "Иванович", "source_variant": 0, "source_start": 4, "source_end": 28},
        }
        with patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(
            registry="persons", reason="review_person_candidate")):
            self.assertIsNone(tasks._mixed_preview_proposal(current))

    def test_partial_role_source_survives_two_unmatched_ocr_name_cohorts(self) -> None:
        current = task()
        current.status = TaskStatus.needs_review
        current.recognized = {
            tasks.MIXED_ROUTE_KEY: {"registry": "persons", "reason": "review_person_candidate"},
            tasks.REVIEW_SUGGESTIONS_KEY: [
                suggestion("Иванов Иван Иванович"), suggestion("Иванов Пётр Петрович"),
            ],
            "fio_raw": {"value": "Иванов Пётр Иванович", "source": "role_partial",
                        "source_variant": 0, "source_start": 4, "source_end": 28},
            "last_name": {"value": "Иванов", "source_variant": 0, "source_start": 4, "source_end": 28},
            "first_name": {"value": "Пётр", "source_variant": 0, "source_start": 4, "source_end": 28},
            "middle_name": {"value": "Иванович", "source_variant": 0, "source_start": 4, "source_end": 28},
            "inn": {"value": "1234567894", "source": "role_partial"},
        }
        with patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(
            registry="persons", reason="review_person_candidate")):
            proposal = tasks._mixed_preview_proposal(current)
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal[1]["kind"], "role_partial_person_candidate")
        self.assertEqual(proposal[0]["first_name"], "Пётр")
        self.assertEqual(proposal[0]["inn"], "")
        current.recognized["inn"] = {
            "source": "role_ambiguous", "warning": "Варианты OCR расходятся по ИНН",
        }
        with patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(
            registry="persons", reason="review_person_candidate")):
            self.assertIsNone(tasks._mixed_preview_proposal(current))

    def test_entity_review_candidate_uses_one_exact_whole_card_occurrence(self) -> None:
        current = task()
        current.status = TaskStatus.needs_review
        current.scenario = "entity_review"
        source = "debtor_entity_candidate:v1"
        fields = {
            "entity_kind": {"value": "legal_entity", "source": source, "warning": "Проверьте", "source_variant": 0,
                            "source_start": 0, "source_end": 5},
            "legal_name": {"value": "Тестовая организация", "source": source, "warning": "Проверьте", "source_variant": 0,
                           "source_start": 6, "source_end": 26},
            "inn": {"value": "1234567894", "source": source, "warning": "Проверьте", "source_variant": 0,
                    "source_start": 27, "source_end": 37},
        }
        candidate_fields = {key: {name: value for name, value in payload.items() if name in
                            ("value", "source_variant", "source_start", "source_end")}
                            for key, payload in fields.items()}
        current.recognized = {
            tasks.MIXED_ROUTE_KEY: {"registry": "organizations", "reason": "review_entity_candidate"},
            tasks.REVIEW_SUGGESTIONS_KEY: [{"kind": "entity_candidate", "fields": candidate_fields}],
            **fields,
        }
        with patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(
            registry="organizations", reason="review_entity_candidate")):
            proposal = tasks._mixed_preview_proposal(current)
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal[1]["kind"], "review_entity_candidate")
        self.assertEqual(proposal[0]["inn"], "1234567894")

    def test_distinct_variant_four_to_one_near_name_is_uncertain_photo_proposal(self) -> None:
        current = task()
        current.status = TaskStatus.needs_review
        items = [suggestion("Иванов Иван Иванович") for _ in range(4)]
        items.append(suggestion("Иванов Иван Ивановна"))
        for index, item in enumerate(items):
            for field in item["fields"].values():
                field["source_variant"] = index
        current.recognized = {
            tasks.MIXED_ROUTE_KEY: {"registry": "persons", "reason": "review_person_candidate"},
            tasks.REVIEW_SUGGESTIONS_KEY: items,
        }
        with patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(
            registry="persons", reason="review_person_candidate")):
            proposal = tasks._mixed_preview_proposal(current)
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal[0]["middle_name"], "Иванович")

    def test_separate_applicant_evidence_does_not_hide_explicit_debtor(self) -> None:
        current = task()
        current.status = TaskStatus.needs_review
        current.recognized = {
            tasks.MIXED_ROUTE_KEY: {"registry": "persons", "reason": "explicit_person_debtor"},
            tasks.APPLICANT_EVIDENCE_KEY: {"role_source": "applicant_role:v1", "name": "Synthetic Applicant"},
        }
        with (patch("sro_robot.core.models.card_from_fields", return_value=SimpleNamespace()),
              patch("sro_robot.core.mixed_classification.classify_card", return_value=SimpleNamespace(
                  registry="persons", reason="explicit_person_debtor"))):
            self.assertIsNotNone(tasks._mixed_preview_proposal(current))

    def test_missing_save_fields_cannot_consume_role_attestation(self) -> None:
        current = task()
        current.status = TaskStatus.awaiting_preview
        current.browser_decision = ""
        db = MagicMock()
        with (patch.object(tasks, "lock_task_for_update"),
              patch.object(tasks, "_preview_snapshot_matches", return_value=True),
              patch.object(tasks, "_owned_principal_matches", return_value=True),
              patch.object(tasks, "_save_readiness_errors", return_value=["case_number"])):
            self.assertFalse(tasks.confirm_durable_preview(db, current, 7, "a" * 64))
        db.add.assert_not_called()
        self.assertIsNone(current.approved_by_id)

    def test_person_inn_omitted_from_external_form_blocks_save_attestation(self) -> None:
        current = task()
        card = SimpleNamespace(inn=SimpleNamespace(value="1234567890"),
                               address=SimpleNamespace(value=""))
        from sro_robot.scenarios.person_card import PersonCardScenario
        with (patch("app.services.fields.scenario_of", return_value=SimpleNamespace(
                  validate=lambda _: [], save_blockers=PersonCardScenario.save_blockers)),
              patch("app.services.fields.card_of", return_value=card)):
            self.assertIn("12-значным", tasks._save_readiness_errors(current)[0])

    def test_same_guid_refresh_request_claim_and_new_snapshot_are_one_shot(self) -> None:
        current = task()
        current.status = TaskStatus.awaiting_preview
        current.browser_decision = ""
        current.values = {"last_name": "Иванов", "first_name": "Иван", "case_number": ""}
        current.screenshots = ["/synthetic/old.png"]
        db = MagicMock()
        db.get.return_value = None
        new_values = {"last_name": "Петров", "first_name": "Пётр", "case_number": "А99-1/2099"}
        scenario = SimpleNamespace(fields=[SimpleNamespace(attr=name) for name in new_values])
        with (patch.object(tasks, "lock_task_for_update"), patch.object(tasks, "log"),
              patch.object(tasks, "_owned_principal_matches", return_value=True),
              patch.object(tasks, "_preview_snapshot_matches", return_value=True),
              patch.object(tasks, "_refresh_identity", return_value=("petrov", "petr")),
              patch("app.services.fields.scenario_of", return_value=scenario),
              patch("app.services.artifacts.read_screenshot", return_value=b"new screenshot")):
            token = "a" * 64
            request_id = "b" * 32
            self.assertTrue(tasks.request_owned_preview_refresh(db, current, 7, token, new_values, request_id))
            self.assertTrue(tasks.request_owned_preview_refresh(db, current, 7, token, new_values, request_id))
            self.assertFalse(tasks.request_owned_preview_refresh(db, current, 7, token, new_values, "c" * 32))
            self.assertEqual(current.preview_refresh_state, "requested")
            self.assertTrue(tasks.claim_owned_preview_refresh(db, current, 7, request_id))
            self.assertFalse(tasks.claim_owned_preview_refresh(db, current, 7, request_id))
            self.assertTrue(tasks.complete_owned_preview_refresh(db, current, 7, ["/synthetic/new.png"]))
            self.assertEqual(current.preview_refresh_generation, 1)
            self.assertEqual(current.preview_refresh_state, "idle")
            self.assertEqual(current.preview_values_sha256, tasks.values_digest(new_values))
            self.assertNotEqual(tasks.preview_token(current), token)
            self.assertFalse(tasks.complete_owned_preview_refresh(db, current, 7, ["/synthetic/new.png"]))

    def test_uncertain_refresh_is_held_without_new_add_or_save(self) -> None:
        current = task()
        current.status = TaskStatus.awaiting_preview
        current.preview_refresh_state = "started"
        current.preview_refresh_actor_id = 7
        db = MagicMock()
        with (patch.object(tasks, "lock_task_for_update"), patch.object(tasks, "log")):
            self.assertTrue(tasks.fail_owned_preview_refresh(db, current, 7))
            self.assertEqual(current.preview_refresh_state, "unknown")
            self.assertFalse(tasks.fail_owned_preview_refresh(db, current, 7))


if __name__ == "__main__":
    unittest.main()
