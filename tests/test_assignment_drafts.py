from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.api import routes_assignments
from app.api.deps import current_user
from app.db.base import get_session
from app.db.models import TaskStatus
from app.services import assignments
from sro_robot.core.models import DebtorCard, Field


class AssignmentDraftServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.task = SimpleNamespace(
            id=41,
            scenario="person_card",
            status=TaskStatus.approved,
            values={"case_number": "А40-123/2026"},
        )
        self.card = DebtorCard(
            last_name=Field(value="Иванов"),
            first_name=Field(value="Иван"),
            middle_name=Field(value="Иванович"),
            case_number=Field(value="А40-123/2026"),
            court=Field(value="Арбитражный суд города Москвы"),
        )

    def test_creates_internal_card_only_from_approved_person_task(self):
        self.db.scalar.side_effect = [self.task, None, None]
        with (
            patch.object(assignments.task_service, "approval_matches", return_value=True),
            patch.object(assignments, "card_of", return_value=self.card),
            patch("app.services.assignments.card_to_fields", return_value={"case_number": {"value": "А40-123/2026"}}),
        ):
            created = assignments.create_debtor_card_from_task(self.db, 41, 7)

        self.assertEqual(created.display_name, "Иванов Иван Иванович")
        self.assertIsNone(created.inn)
        self.assertEqual(created.source_task_id, 41)
        self.assertEqual(created.created_by_id, 7)
        self.db.add.assert_called_once_with(created)
        self.db.flush.assert_called_once()

    def test_refuses_unapproved_or_unverified_source(self):
        self.task.status = TaskStatus.needs_review
        self.db.scalar.side_effect = [self.task, None]
        with patch.object(assignments.task_service, "approval_matches", return_value=False):
            with self.assertRaises(assignments.DraftNotReady):
                assignments.create_debtor_card_from_task(self.db, 41, 7)
        self.db.add.assert_not_called()

    def test_existing_source_card_is_idempotent(self):
        from sro_robot.core.models import card_to_fields
        existing = SimpleNamespace(id=9, preparation_state="reviewed", values=card_to_fields(self.card))
        self.db.scalar.side_effect = [self.task, existing]
        with (patch.object(assignments.task_service, "approval_matches", return_value=True),
              patch.object(assignments, "card_of", return_value=self.card)):
            self.assertIs(assignments.create_debtor_card_from_task(self.db, 41, 8), existing)
        self.db.add.assert_not_called()

    def test_creates_separate_assignment_and_prefills_only_case_and_court(self):
        debtor = SimpleNamespace(
            id=9,
            values={
                "case_number": {"value": "А40-123/2026"},
                "court": {"value": "Арбитражный суд города Москвы"},
                "inn": {"value": "synthetic"},
            },
        )
        self.db.get.return_value = debtor
        created = assignments.create_assignment_draft(self.db, 9, 7)

        self.assertEqual(created.debtor_card_id, 9)
        self.assertEqual(created.case_number, "А40-123/2026")
        self.assertEqual(created.values["arbitration_court"], "Арбитражный суд города Москвы")
        self.assertEqual(created.values["practitioner"], "")
        self.assertEqual(created.status, "draft")
        self.assertEqual(created.created_by_id, 7)
        self.db.add.assert_called_once_with(created)

    def test_optimistic_version_rejects_overwrite_by_parallel_editor(self):
        current = SimpleNamespace(id=5, version=3, values={}, case_number="A", status="draft")
        self.db.scalar.return_value = current
        with self.assertRaises(assignments.DraftConflict):
            assignments.update_assignment_draft(
                self.db, 5, 12, 2, "А40-123/2026", {}, "draft"
            )
        self.assertEqual(current.version, 3)
        self.db.flush.assert_not_called()


class AssignmentDraftRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(routes_assignments.router)
        self.app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
        self.app.add_api_route("/", lambda: {}, name="tasks_page")
        self.app.add_api_route("/logout", lambda: {}, name="logout")
        self.app.add_api_route("/tasks/{task_id}", lambda task_id: {}, name="task_page")
        self.db = MagicMock()
        self.db.scalars.return_value.all.return_value = []
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(
            id=7, login="synthetic", full_name="Synthetic employee"
        )
        self.app.dependency_overrides[get_session] = lambda: self.db
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def test_internal_cards_page_renders_for_authenticated_employee(self):
        response = self.client.get("/debtors")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Внутренние карточки должников", response.text)
        self.assertIn("не создаёт запись в СРОАрбитр", response.text)

    def test_absent_optional_fields_render_empty_instead_of_literal_none(self):
        employee = SimpleNamespace(full_name="Synthetic employee", login="synthetic")
        self.db.get.return_value = SimpleNamespace(
            id=91, display_name="Synthetic person", inn=None, source_task_id=55,
            preparation_state="prepared", version=1,
            created_by=employee, updated_by=employee,
            values={"snils": {"value": None}, "court": {"value": None}},
        )
        self.db.execute.return_value.all.return_value = []
        response = self.client.get("/debtors/91")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="snils" maxlength="2000" value=""', response.text)
        self.assertIn('name="court" maxlength="2000" value=""', response.text)
        self.assertNotIn('value="None"', response.text)

    def test_card_creation_route_only_redirects_to_internal_record(self):
        created = SimpleNamespace(id=91)
        with patch.object(assignments, "create_debtor_card_from_task", return_value=created) as create:
            response = self.client.post("/tasks/55/internal-debtor-card", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/debtors/91?created=1")
        create.assert_called_once_with(self.db, 55, 7)
        self.db.commit.assert_called_once()

    def test_updates_record_editor_and_advance_version(self):
        current = SimpleNamespace(id=5, version=3, values={}, case_number="A", status="draft")
        self.db.scalar.return_value = current
        updated = assignments.update_assignment_draft(
            self.db, 5, 12, 3, " А40-123/2026 ", {"practitioner": "Петров"}, "awaiting_information"
        )
        self.assertIs(updated, current)
        self.assertEqual(current.version, 4)
        self.assertEqual(current.updated_by_id, 12)
        self.assertEqual(current.case_number, "А40-123/2026")
        self.assertEqual(current.values["practitioner"], "Петров")
        self.assertEqual(current.status, "awaiting_information")
        self.db.flush.assert_called_once()

    def test_debtor_card_correction_validates_inn_and_preserves_operator_attribution(self):
        card = SimpleNamespace(
            id=9,
            version=2,
            values={
                "last_name": {"value": "Иванов", "source": "ocr"},
                "first_name": {"value": "Иван", "source": "ocr"},
                "middle_name": {"value": "Иванович", "source": "ocr"},
                "inn": {"value": "", "source": "not_found"},
            },
            inn=None,
            display_name="Иванов Иван Иванович",
        )
        self.db.scalar.side_effect = [card, None]
        corrected_values = {name: "" for name in assignments.DEBTOR_CARD_FIELDS}
        corrected_values.update(
            last_name="Петров", first_name="Иван", middle_name="Иванович", inn="772881234508"
        )
        updated = assignments.update_debtor_card(self.db, 9, 22, 2, corrected_values)
        self.assertIs(updated, card)
        self.assertEqual(card.display_name, "Петров Иван Иванович")
        self.assertEqual(card.inn, "772881234508")
        self.assertEqual(card.values["last_name"]["source"], "employee")
        self.assertEqual(card.updated_by_id, 22)
        self.assertEqual(card.version, 3)
        self.db.flush.assert_called_once()

    def test_debtor_card_rejects_invalid_inn_without_mutation(self):
        card = SimpleNamespace(
            id=9,
            version=2,
            values={"last_name": {"value": "Иванов"}, "first_name": {"value": "Иван"}},
            inn=None,
            display_name="Иванов Иван",
        )
        self.db.scalar.return_value = card
        values = {name: "" for name in assignments.DEBTOR_CARD_FIELDS}
        values.update(last_name="Петров", first_name="Иван", inn="772881234509")
        with self.assertRaises(assignments.DraftNotReady):
            assignments.update_debtor_card(self.db, 9, 22, 2, values)
        self.assertEqual(card.display_name, "Иванов Иван")
        self.assertEqual(card.version, 2)
        self.db.flush.assert_not_called()

    def test_debtor_card_rejects_existing_inn_as_possible_duplicate(self):
        card = SimpleNamespace(
            id=9,
            version=2,
            values={"last_name": {"value": "Иванов"}, "first_name": {"value": "Иван"}},
            inn=None,
            display_name="Иванов Иван",
        )
        existing = SimpleNamespace(id=10)
        self.db.scalar.side_effect = [card, existing]
        values = {name: "" for name in assignments.DEBTOR_CARD_FIELDS}
        values.update(last_name="Петров", first_name="Иван", inn="772881234508")
        with self.assertRaises(assignments.DuplicateDraft):
            assignments.update_debtor_card(self.db, 9, 22, 2, values)
        self.assertEqual(card.display_name, "Иванов Иван")
        self.assertEqual(card.version, 2)
        self.db.flush.assert_not_called()

    def test_rejects_oversized_values_without_mutating(self):
        current = SimpleNamespace(id=5, version=3, values={}, case_number="A", status="draft")
        self.db.scalar.return_value = current
        with self.assertRaises(assignments.DraftNotReady):
            assignments.update_assignment_draft(
                self.db, 5, 12, 3, "А40-123/2026", {"practitioner": "x" * 2001}, "draft"
            )
        self.assertEqual(current.version, 3)
        self.db.flush.assert_not_called()


if __name__ == "__main__":
    unittest.main()
