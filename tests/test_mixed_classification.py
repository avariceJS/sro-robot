"""Synthetic role-routing and exact batch-occurrence checks."""

from __future__ import annotations

import unittest

from app.db.models import Task, TaskStatus
from app.services.mixed_intake import group_planned_rows, route_for_task
from sro_robot.core.entity_extract import EntityDebtorCard
from sro_robot.core.mixed_classification import classify_card
from sro_robot.core.models import DebtorCard, Field


def person(*, role_source: str = "", name: bool = True) -> DebtorCard:
    card = DebtorCard()
    card.fio_raw = Field("Тестова Анна", source=role_source)
    if name:
        card.last_name = Field("Тестова")
        card.first_name = Field("Анна")
    card.case_number = Field("А40-123/2026")
    return card


def entity(*, role_source: str = "") -> EntityDebtorCard:
    card = EntityDebtorCard()
    card.entity_kind = Field("legal_entity", source=role_source)
    card.legal_name = Field("ООО Учебный пример")
    card.case_number = Field("А40-123/2026")
    return card


APPLICANT = {
    "role_source": "applicant_role:v1",
    "name": "ООО Заявитель",
    "case_number": "А40-123/2026",
}


class MixedClassificationTests(unittest.TestCase):
    def test_explicit_debtor_roles_choose_review_folder_without_approval(self) -> None:
        self.assertEqual(classify_card(person(role_source="debtor_role:v1")).registry, "persons")
        result = classify_card(entity(role_source="debtor_entity_role:v1"))
        self.assertEqual((result.registry, result.subject_kind), ("organizations", "legal_entity"))

    def test_unbound_identity_still_gets_useful_folder_with_review_reason(self) -> None:
        self.assertEqual(classify_card(person()).reason, "review_person_candidate")
        self.assertEqual(classify_card(entity()).reason, "review_entity_candidate")
        card = person(name=False)
        card.review_suggestions = [{"kind": "application_person", "fields": {}}]
        self.assertEqual(classify_card(card).registry, "persons")

    def test_applicant_requires_separate_role_and_matching_case(self) -> None:
        card = person(name=False)
        self.assertEqual(classify_card(card, applicant_evidence=APPLICANT).registry, "assignments")
        for changed in (
            {**APPLICANT, "role_source": ""},
            {**APPLICANT, "name": ""},
            {**APPLICANT, "case_number": "А40-999/2026"},
        ):
            self.assertEqual(classify_card(card, applicant_evidence=changed).registry, "review")
        card.case_number = Field()
        self.assertEqual(classify_card(card, applicant_evidence=APPLICANT).registry, "review")

    def test_court_act_can_name_both_roles_and_still_route_debtor(self) -> None:
        for card, expected in (
            (person(role_source="debtor_role:v1"), "persons"),
            (entity(role_source="debtor_entity_role:v1"), "organizations"),
        ):
            result = classify_card(card, applicant_evidence=APPLICANT)
            self.assertEqual(result.registry, expected)

    def test_applicant_cannot_displace_incomplete_explicit_debtor(self) -> None:
        card = person(role_source="debtor_role:v1", name=False)
        self.assertEqual(classify_card(card, applicant_evidence=APPLICANT).registry, "review")
        card.case_number = Field()
        self.assertEqual(classify_card(card, applicant_evidence=APPLICANT).registry, "review")

    def test_identifiers_and_review_suggestions_do_not_prove_applicant(self) -> None:
        card = person(name=False)
        card.inn = Field("772881234508", source="ocr")
        card.review_suggestions = [{"kind": "application_person", "fields": {}}]
        self.assertNotEqual(classify_card(card).registry, "assignments")

    def test_stored_route_and_legacy_fallback_are_stable(self) -> None:
        task = Task(id=1, scenario="person_card", status=TaskStatus.needs_review,
                    recognized={"last_name": {"value": "Тестова"},
                                "first_name": {"value": "Анна"}})
        self.assertEqual(route_for_task(task).registry, "persons")
        task.recognized = {"_mixed_route_v1": {
            "registry": "assignments", "subject_kind": "applicant", "reason": "explicit_applicant",
        }, "case_number": {"value": "А40-123/2026"}}
        self.assertEqual(route_for_task(task).registry, "review")
        task.recognized["_applicant_evidence_v1"] = APPLICANT
        self.assertEqual(route_for_task(task).registry, "assignments")
        task.recognized["_mixed_route_v1"]["reason"] = "changed"
        self.assertEqual(route_for_task(task).reason, "routing_evidence_drift")
        task.status = TaskStatus.failed
        self.assertEqual(route_for_task(task).reason, "ocr_failed")

    def test_exact_occurrences_keep_duplicates_and_pending_rows(self) -> None:
        first = Task(id=1, scenario="person_card", status=TaskStatus.needs_review,
                     recognized={"last_name": {"value": "Тестова"},
                                 "first_name": {"value": "Анна"}})
        second = Task(id=2, scenario="person_card", status=TaskStatus.needs_review,
                      recognized=first.recognized)
        result = group_planned_rows([(2, "repeat.heic", second),
                                     (0, "repeat.heic", first),
                                     (1, "pending.heic", None)])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["counts"]["persons"], 2)
        self.assertEqual(result["counts"]["pending"], 1)
        self.assertEqual([row["file_index"] for row in result["groups"]["persons"]], [0, 2])
        self.assertEqual([row["task_id"] for row in result["groups"]["persons"]], [1, 2])
        with self.assertRaises(ValueError):
            group_planned_rows([(0, "a", first), (0, "b", second)])


if __name__ == "__main__":
    unittest.main()
