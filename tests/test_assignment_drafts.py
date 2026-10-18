from __future__ import annotations

import unittest
from datetime import datetime
import re
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
from sro_robot.core.entity_extract import EntityDebtorCard


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
            patch.object(assignments, "create_assignment_draft") as create_assignment,
        ):
            created = assignments.create_debtor_card_from_task(self.db, 41, 7)

        self.assertEqual(created.display_name, "Иванов Иван Иванович")
        self.assertIsNone(created.inn)
        self.assertEqual(created.source_task_id, 41)
        self.assertEqual(created.created_by_id, 7)
        self.db.add.assert_called_once_with(created)
        self.db.flush.assert_called_once()
        create_assignment.assert_not_called()

    def test_task_to_card_creation_rejects_invalid_person_snils(self):
        self.db.scalar.side_effect = [self.task, None]
        self.card.snils = Field(value="11223344596")
        with (
            patch.object(assignments.task_service, "approval_matches", return_value=True),
            patch.object(assignments, "card_of", return_value=self.card),
        ):
            with self.assertRaisesRegex(assignments.DraftNotReady, "контрольной суммы"):
                assignments.create_debtor_card_from_task(self.db, 41, 7)

        self.db.add.assert_not_called()
        self.db.flush.assert_not_called()

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
              patch.object(assignments, "card_of", return_value=self.card),
              patch.object(assignments, "create_assignment_draft") as create_assignment):
            self.assertIs(assignments.create_debtor_card_from_task(self.db, 41, 8), existing)
        self.db.add.assert_not_called()
        create_assignment.assert_not_called()

    def test_creates_reviewed_organization_card_from_approved_entity_task(self):
        self.task.scenario = "entity_review"
        self.db.scalar.side_effect = [self.task, None, None, None]
        card = EntityDebtorCard(
            entity_kind=Field("legal_entity", 1, "operator"),
            legal_name=Field("ООО Синтетика", 1, "operator"),
            inn=Field("7707083893", 1, "operator"),
            ogrn=Field("1027700132195", 0.65, "debtor_entity_role:v1"),
            case_number=Field("А40-123/2026", 0.8, "ocr"),
        )
        with (
            patch.object(assignments.task_service, "approval_matches", return_value=True),
            patch.object(assignments, "card_of", return_value=card),
            patch.object(assignments, "_lock_inn"),
            patch("app.services.assignments.card_to_fields", return_value={
                "legal_name": {"value": "ООО Синтетика"},
                "inn": {"value": "7707083893"},
                "ogrn": {"value": "1027700132195"},
                "case_number": {"value": "А40-123/2026"},
            }),
        ):
            created = assignments.create_debtor_card_from_task(self.db, 41, 7)

        self.assertEqual(created.entity_kind, "legal_entity")
        self.assertEqual(created.display_name, "ООО Синтетика")
        self.assertEqual(created.inn, "7707083893")
        self.assertEqual(created.ogrn, "1027700132195")
        self.assertEqual(created.source_task_id, 41)
        self.db.add.assert_called_once_with(created)

    def test_manual_organization_card_validates_and_normalizes_registry_ids(self):
        debtor = SimpleNamespace(
            id=29, entity_kind="legal_entity", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.side_effect = [debtor, None]
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
        values.update(
            legal_name="ООО Синтетика", inn="", ogrn="1 0277 0013 2195",
            kpp="770 001 001",
        )
        with patch.object(assignments, "_lock_inn"):
            updated = assignments.update_debtor_card(self.db, 29, 7, 1, values, confirm=True)
        self.assertEqual(updated.values["ogrn"]["value"], "1027700132195")
        self.assertEqual(updated.values["kpp"]["value"], "770001001")

    def test_manual_organization_rejects_duplicate_ogrn_even_without_matching_inn(self):
        debtor = SimpleNamespace(
            id=29, entity_kind="legal_entity", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.side_effect = [debtor, SimpleNamespace(id=40)]
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
        values.update(legal_name="ООО Синтетика", ogrn="1027700132195")

        with patch.object(assignments, "_lock_inn"):
            with self.assertRaises(assignments.DuplicateDraft):
                assignments.update_debtor_card(self.db, 29, 7, 1, values)

        query = self.db.scalar.call_args_list[1].args[0]
        self.assertIn("1027700132195", query.compile().params.values())
        self.assertEqual(debtor.version, 1)
        self.db.flush.assert_not_called()

    def test_card_display_name_limit_fails_before_database_write(self):
        debtor = SimpleNamespace(
            id=36, entity_kind="legal_entity", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.return_value = debtor
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
        values["legal_name"] = "А" * (assignments.MAX_DISPLAY_NAME_CHARACTERS + 1)

        with self.assertRaisesRegex(assignments.DraftNotReady, "512 символов"):
            assignments.update_debtor_card(self.db, debtor.id, 7, 1, values)

        self.assertEqual(debtor.display_name, "Новая карточка")
        self.assertEqual(debtor.version, 1)
        self.db.flush.assert_not_called()

    def test_approved_entity_task_rejects_oversized_name_before_card_creation(self):
        self.task.scenario = "entity_review"
        self.db.scalar.side_effect = [self.task, None]
        card = EntityDebtorCard(
            entity_kind=Field("legal_entity", 1, "operator"),
            legal_name=Field("О" * (assignments.MAX_DISPLAY_NAME_CHARACTERS + 1), 1, "operator"),
            inn=Field("7707083893", 1, "operator"),
        )

        with (
            patch.object(assignments.task_service, "approval_matches", return_value=True),
            patch.object(assignments, "card_of", return_value=card),
        ):
            with self.assertRaisesRegex(assignments.DraftNotReady, "512 символов"):
                assignments.create_debtor_card_from_task(self.db, 41, 7)

        self.db.add.assert_not_called()
        self.db.flush.assert_not_called()

    def test_address_type_is_saved_and_corrected_text_loses_stale_ocr_location(self):
        debtor = SimpleNamespace(
            id=31, entity_kind="legal_entity", version=1,
            values={
                "address": {"value": "101000, синтетический адрес", "source": "debtor_entity_role:v1",
                            "source_variant": 0, "source_start": 30, "source_end": 55,
                            "source_excerpt": "Адрес регистрации: 101000, синтетический адрес"},
                "address_kind": {"value": "registration", "source": "debtor_entity_role:v1"},
            },
            inn=None, display_name="ООО Учебное дело", preparation_state="draft",
        )
        self.db.scalar.side_effect = [debtor, None]
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
        values.update(legal_name="ООО Учебное дело", address="Исправленный синтетический адрес",
                      address_kind="actual")

        assignments.update_debtor_card(self.db, debtor.id, 7, 1, values, confirm=True)

        self.assertEqual(debtor.values["address"]["source"], "employee")
        self.assertNotIn("source_start", debtor.values["address"])
        self.assertEqual(debtor.values["address_kind"]["value"], "actual")

    def test_manual_organization_card_rejects_bad_ogrn_control_digit(self):
        debtor = SimpleNamespace(
            id=30, entity_kind="legal_entity", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.return_value = debtor
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
        values.update(legal_name="ООО Синтетика", ogrn="1027700132194")
        with self.assertRaisesRegex(assignments.DraftNotReady, "контрольную цифру"):
            assignments.update_debtor_card(self.db, 30, 7, 1, values, confirm=True)
        self.db.flush.assert_not_called()

    def test_manual_organization_card_rejects_checksum_valid_non_ogrn_type(self):
        debtor = SimpleNamespace(
            id=32, entity_kind="legal_entity", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.return_value = debtor
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
        values.update(legal_name="ООО Синтетика", ogrn="2027700132194")
        with self.assertRaisesRegex(assignments.DraftNotReady, "длину и контрольную цифру"):
            assignments.update_debtor_card(self.db, 32, 7, 1, values, confirm=True)
        self.db.flush.assert_not_called()

    def test_manual_registry_ids_reject_letters_instead_of_silently_dropping_them(self):
        debtor = SimpleNamespace(
            id=33, entity_kind="legal_entity", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.return_value = debtor
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
        values.update(legal_name="ООО Синтетика", ogrn="1027700132195X")
        with self.assertRaisesRegex(assignments.DraftNotReady, "формат ОГРН"):
            assignments.update_debtor_card(self.db, 33, 7, 1, values, confirm=True)
        self.db.flush.assert_not_called()

    def test_manual_person_inn_rejects_letter_instead_of_silently_dropping_it(self):
        debtor = SimpleNamespace(
            id=34, entity_kind="person", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.return_value = debtor
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["person"]}
        values.update(last_name="Иванов", first_name="Иван", inn="50010000114X6")
        with self.assertRaisesRegex(assignments.DraftNotReady, "только цифрами"):
            assignments.update_debtor_card(self.db, 34, 7, 1, values, confirm=True)
        self.db.flush.assert_not_called()

    def test_creates_separate_assignment_and_prefills_only_case_and_court(self):
        debtor = SimpleNamespace(
            id=9,
            preparation_state="reviewed",
            values={
                "case_number": {"value": "А40-123/2026", "confidence": 0.9},
                "court": {"value": "Арбитражный суд города Москвы", "confidence": 0.9},
                "inn": {"value": "synthetic"},
            },
        )
        self.db.scalar.return_value = debtor
        created = assignments.create_assignment_draft(self.db, 9, 7)

        self.assertEqual(created.debtor_card_id, 9)
        self.assertEqual(created.case_number, "А40-123/2026")
        self.assertEqual(created.values["arbitration_court"], "Арбитражный суд города Москвы")
        self.assertEqual(created.values["practitioner"], "")
        self.assertEqual(created.status, "draft")
        self.assertEqual(created.created_by_id, 7)
        self.db.add.assert_called_once_with(created)

    def test_source_task_assignment_is_idempotent(self):
        debtor = SimpleNamespace(
            id=9, source_task_id=41, preparation_state="reviewed",
            values={"case_number": {"value": "А40-123/2026", "confidence": 0.9}, "court": {"value": "Суд", "confidence": 0.9}},
        )
        task = SimpleNamespace(id=41, status=TaskStatus.approved, values={})
        existing = SimpleNamespace(id=18, debtor_card_id=9, source_task_id=41)
        self.db.scalar.side_effect = [task, debtor, None, task, debtor, existing]
        with patch.object(assignments.task_service, "approval_matches", return_value=True):
            first = assignments.create_assignment_draft(self.db, 9, 7, source_task_id=41)
            replay = assignments.create_assignment_draft(self.db, 9, 8, source_task_id=41)
        self.assertEqual(first.source_task_id, 41)
        self.assertIs(replay, existing)
        self.db.add.assert_called_once_with(first)

    def test_same_debtor_and_case_can_have_distinct_task_assignments(self):
        debtor = SimpleNamespace(
            id=9, source_task_id=41, preparation_state="reviewed",
            values={"case_number": {"value": "А40-123/2026", "confidence": 0.9}, "court": {"value": "Суд", "confidence": 0.9}},
        )
        self.db.scalar.side_effect = [
            SimpleNamespace(id=41, status=TaskStatus.approved, values={}), debtor, None,
            SimpleNamespace(id=42, status=TaskStatus.approved, values={}), debtor, None,
        ]
        with patch.object(assignments.task_service, "approval_matches", return_value=True):
            first = assignments.create_assignment_draft(self.db, 9, 7, source_task_id=41)
            debtor.source_task_id = 42
            second = assignments.create_assignment_draft(self.db, 9, 7, source_task_id=42)
        self.assertIsNot(first, second)
        self.assertEqual(first.case_number, second.case_number)
        self.assertEqual(first.debtor_card_id, second.debtor_card_id)
        self.assertEqual([first.source_task_id, second.source_task_id], [41, 42])
        self.assertEqual(self.db.add.call_count, 2)

    def test_assignment_requires_an_employee_reviewed_debtor_card(self):
        self.db.scalar.return_value = SimpleNamespace(id=9, preparation_state="prepared", values={})
        with self.assertRaisesRegex(assignments.DraftNotReady, "проверьте и сохраните"):
            assignments.create_assignment_draft(self.db, 9, 7)
        self.db.add.assert_not_called()

    def test_manual_card_starts_as_unlinked_draft_without_a_source_task(self):
        card = assignments.create_manual_debtor_card(self.db, 17)
        self.assertEqual(card.preparation_state, "draft")
        self.assertIsNone(card.source_task_id)
        self.assertEqual(card.display_name, "Новая карточка")
        self.assertEqual(card.created_by_id, 17)
        self.db.add.assert_called_once_with(card)
        self.db.flush.assert_called_once()

    def test_optimistic_version_rejects_overwrite_by_parallel_editor(self):
        current = SimpleNamespace(id=5, version=3, values={}, case_number="A", status="draft")
        self.db.scalar.return_value = current
        with self.assertRaises(assignments.DraftConflict):
            assignments.update_assignment_draft(
                self.db, 5, 12, 2, "А40-123/2026", {}, "draft"
            )
        self.assertEqual(current.version, 3)
        self.db.flush.assert_not_called()

    def test_stale_replay_of_already_applied_assignment_edit_is_idempotent(self):
        saved_values = {name: "" for name, _label in assignments.ASSIGNMENT_FIELDS}
        saved_values["practitioner"] = "Иванов"
        current = SimpleNamespace(
            id=5,
            version=3,
            values=saved_values,
            case_number="А40-123/2026",
            status="awaiting_information",
        )
        self.db.scalar.return_value = current
        updated = assignments.update_assignment_draft(
            self.db,
            5,
            12,
            2,
            "А40-123/2026",
            saved_values,
            "awaiting_information",
        )
        self.assertIs(updated, current)
        self.assertEqual(current.version, 3)
        self.db.flush.assert_not_called()

    def test_stale_replay_of_existing_sole_proprietor_link_is_idempotent(self):
        debtor = SimpleNamespace(
            id=31,
            entity_kind="sole_proprietor",
            preparation_state="reviewed",
            version=3,
            values={"related_person_card_id": {"value": "42"}},
        )
        person = SimpleNamespace(id=42, entity_kind="person", preparation_state="reviewed")
        another_person = SimpleNamespace(id=43, entity_kind="person", preparation_state="reviewed")
        self.db.scalar.side_effect = [debtor, person, debtor, another_person]

        replay = assignments.set_sole_proprietor_person_link(self.db, 31, 12, 2, 42)

        self.assertIs(replay, debtor)
        self.assertEqual(debtor.version, 3)
        with self.assertRaises(assignments.DraftConflict):
            assignments.set_sole_proprietor_person_link(self.db, 31, 12, 2, 43)
        self.db.flush.assert_not_called()

    def test_incomplete_draft_can_be_saved_without_case_or_practitioner(self):
        current = SimpleNamespace(id=6, version=1, values={}, case_number="", status="draft")
        self.db.scalar.return_value = current
        updated = assignments.update_assignment_draft(self.db, 6, 13, 1, "", {}, "awaiting_information")
        self.assertIs(updated, current)
        self.assertEqual(updated.case_number, "")
        self.assertEqual(updated.values["practitioner"], "")
        self.assertEqual(updated.status, "awaiting_information")
        self.assertEqual(updated.updated_by_id, 13)
        self.assertEqual(updated.version, 2)
        self.db.flush.assert_called_once()

    def test_assignment_creation_rejects_missing_action_key(self):
        with self.assertRaises(assignments.AssignmentRequestConflict):
            assignments.create_assignment_draft_once(self.db, 88, 7, "")
        self.db.execute.assert_not_called()


class AssignmentDraftRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(routes_assignments.router)
        self.app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
        self.app.add_api_route("/", lambda: {}, name="tasks_page")
        self.app.add_api_route("/debtors/intake", lambda: {}, name="debtor_intake_page")
        self.app.add_api_route("/logout", lambda: {}, name="logout")
        self.app.add_api_route("/tasks/{task_id}", lambda task_id: {}, name="task_page")
        self.app.add_api_route("/tasks/{task_id}/document", lambda task_id: {}, name="task_document")
        self.app.add_api_route("/tasks/{task_id}/document/preview", lambda task_id: {}, name="task_document_preview")
        self.db = MagicMock()
        self.db.scalars.return_value.all.return_value = []
        self.db.scalar.return_value = 0
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(
            id=7, login="synthetic", full_name="Synthetic employee"
        )
        self.app.dependency_overrides[get_session] = lambda: self.db
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def test_assignment_creation_key_is_random_hidden_form_data(self):
        card = SimpleNamespace(
            id=88, display_name="Synthetic Debtor", inn="990000000000",
            values={"case_number": {"value": "А99-1/2099"}, "court": {"value": "Синтетический суд"}},
        )
        self.db.scalars.return_value.all.return_value = [card]
        first = self.client.get("/assignments/new")
        second = self.client.get("/assignments/new")
        self.assertEqual(first.status_code, 200)
        first_keys = re.findall(r'name="request_key" value="([0-9a-f]{64})"', first.text)
        second_keys = re.findall(r'name="request_key" value="([0-9a-f]{64})"', second.text)
        self.assertEqual(len(first_keys), 1)
        self.assertEqual(len(second_keys), 1)
        self.assertNotEqual(first_keys[0], second_keys[0])
        self.assertNotIn(first_keys[0], str(first.url))
        self.assertIn('name="debtor_id" value="88"', first.text)

    def test_each_assignment_choice_form_has_its_own_idempotency_key(self):
        self.db.scalars.return_value.all.return_value = [
            SimpleNamespace(
                id=88, display_name="Synthetic Debtor A", inn="990000000000",
                values={"case_number": {"value": "А99-1/2099"}, "court": {"value": "Синтетический суд"}},
            ),
            SimpleNamespace(
                id=89, display_name="Synthetic Debtor B", inn="990000000001",
                values={"case_number": {"value": "А99-2/2099"}, "court": {"value": "Другой суд"}},
            ),
        ]
        response = self.client.get("/assignments/new")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            re.findall(r'name="debtor_id" value="(\d+)"', response.text), ["88", "89"],
        )
        keys = re.findall(r'name="request_key" value="([0-9a-f]{64})"', response.text)
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)

    def test_internal_cards_page_renders_for_authenticated_employee(self):
        response = self.client.get("/debtors")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Карточки должников", response.text)
        self.assertIn("Создать карточку вручную", response.text)
        self.assertIn("/debtors/intake#document-upload", response.text)
        self.assertIn("не создаёт запись в СРОАрбитр", response.text)

    def test_manual_card_form_offers_sources_and_three_explicit_actions(self):
        response = self.client.get("/debtors/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Найти дело в КАД", response.text)
        self.assertIn("Загрузить документ", response.text)
        self.assertIn('value="draft"', response.text)
        self.assertIn('value="confirm"', response.text)
        self.assertIn('value="cancel"', response.text)

    def test_debtor_page_previews_supported_source_photos(self):
        card = SimpleNamespace(
            id=5, display_name="Synthetic Debtor", inn="", values={}, version=1,
            preparation_state="prepared", source_task_id=55,
            created_by=SimpleNamespace(full_name="Synthetic employee", login="synthetic"),
            updated_by=SimpleNamespace(full_name="Synthetic employee", login="synthetic"),
        )
        for filename, expected in (
            ("synthetic.jpg", "source-image"),
            ("synthetic.jpeg", "source-image"),
            ("synthetic.png", "source-image"),
            ("synthetic.pdf", "document-preview"),
            ("synthetic.gif", "Встроенный просмотр поддерживает PDF, JPEG и PNG"),
        ):
            with self.subTest(filename=filename):
                task = SimpleNamespace(
                    id=55, status=TaskStatus.needs_review,
                    document=SimpleNamespace(
                        filename=filename, stored_path=f"/private/{filename}",
                    ),
                )
                self.db.get.side_effect = [card, task]
                self.db.scalars.return_value.all.return_value = []
                self.db.scalar.return_value = None
                self.db.execute.return_value.all.return_value = []

                response = self.client.get("/debtors/5")

                self.assertEqual(response.status_code, 200)
                self.assertIn(expected, response.text)
                if expected == "source-image":
                    self.assertIn('src="http://testserver/tasks/55/document/preview"', response.text)
                    self.assertIn("staff-controls.js", response.text)

    def test_assignment_history_query_omits_personal_snapshot_payload(self):
        self.db.get.return_value = SimpleNamespace(
            id=5, debtor_card_id=9, source_task_id=None, case_number="А99-5/2099",
            status="draft", version=1, values={},
            debtor_card=SimpleNamespace(id=9, display_name="Synthetic debtor", source_task_id=None),
        )
        self.db.execute.return_value.all.return_value = [(
            3, "ready_for_review", datetime(2026, 10, 18, 12, 0),
            "Synthetic reviewer", "synthetic",
        )]

        response = self.client.get("/assignments/5")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Версия 3 · Готово к проверке", response.text)
        self.assertIn("Synthetic reviewer", response.text)
        query = str(self.db.execute.call_args.args[0])
        self.assertIn("assignment_draft_revision.revision", query)
        self.assertIn("assignment_draft_revision.status", query)
        self.assertNotIn("assignment_draft_revision.snapshot", query)

    def test_debtor_history_query_selects_only_displayed_snapshot_metadata(self):
        employee = SimpleNamespace(full_name="Synthetic employee", login="synthetic")
        self.db.get.return_value = SimpleNamespace(
            id=91, display_name="Synthetic company", inn="", source_task_id=None,
            preparation_state="reviewed", version=1, values={}, entity_kind="legal_entity",
            created_by=employee, updated_by=employee,
        )
        self.db.execute.return_value.all.return_value = [(
            4, datetime(2026, 10, 18, 12, 0), "prepared",
            "Synthetic reviewer", "synthetic",
        )]
        self.db.scalars.return_value.all.return_value = []

        response = self.client.get("/debtors/91")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Версия 4", response.text)
        self.assertIn("автоматическая подготовка", response.text)
        self.assertIn("Synthetic reviewer", response.text)
        query = str(self.db.execute.call_args.args[0])
        self.assertIn("debtor_card_draft_revision.snapshot ->>", query)
        self.assertIn("AS preparation_state", query)
        self.assertNotIn("debtor_card_draft_revision.snapshot,", query)

    def test_empty_assignment_fields_are_informational_not_validation_errors(self):
        self.db.get.return_value = SimpleNamespace(
            id=1, debtor_card_id=2, case_number="", status="draft", values={},
            source_task_id=None,
            debtor_card=SimpleNamespace(id=2, display_name="Synthetic Debtor", source_task_id=None),
            created_by=SimpleNamespace(full_name="Synthetic employee", login="synthetic"),
            updated_by=SimpleNamespace(full_name="Synthetic employee", login="synthetic"),
        )
        self.db.scalars.return_value.all.return_value = []

        response = self.client.get("/assignments/1")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="notice info" role="status"', response.text)
        self.assertIn("Некоторые сведения пока не указаны", response.text)
        self.assertIn("Показать незаполненные поля", response.text)
        self.assertIn('<details class="missing-field-list">', response.text)
        self.assertIn("Арбитражный управляющий", response.text)
        self.assertNotIn('class="field-warning"><strong>Не заполнено', response.text)

    def test_cancelled_manual_card_does_not_create_database_row(self):
        response = self.client.post("/debtors/new", data={"action": "cancel"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/debtors")
        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

    def test_manual_draft_creation_is_internal_and_resumable(self):
        created = SimpleNamespace(id=42, version=1)
        with patch.object(assignments, "create_manual_debtor_card_once", return_value=created) as create:
            response = self.client.post("/debtors/new", data={
                "action": "draft", "first_name": "Иван", "request_key": "a" * 64,
            }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/debtors/42?saved=1")
        self.assertEqual(create.call_args.args[:3], (self.db, 7, "person"))
        self.assertFalse(create.call_args.args[4])
        self.assertEqual(create.call_args.args[5], "a" * 64)
        self.db.commit.assert_called_once()

    def test_new_manual_card_form_exposes_distinct_person_company_and_ip_fields(self):
        response = self.client.get("/debtors/new")
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.text, r'name="request_key" value="[0-9a-f]{64}"')
        for kind in assignments.ENTITY_CARD_FIELDS:
            self.assertIn(f'data-entity-kind-fields="{kind}"', response.text)
        self.assertIn('name="legal_name"', response.text)
        self.assertIn('name="legal_form"', response.text)
        self.assertIn('name="debtor_name"', response.text)
        self.assertIn('src="http://testserver/static/debtor-entity-kind.js"', response.text)

    def test_legal_entity_manual_draft_uses_organization_specific_fields(self):
        created = SimpleNamespace(id=44, version=1)
        form_values = {
            "action": "draft", "entity_kind": "legal_entity",
            "legal_name": "ООО Учебное дело", "legal_form": "ООО", "inn": "7707083893",
            "request_key": "b" * 64,
        }
        with patch.object(assignments, "create_manual_debtor_card_once", return_value=created) as create:
            response = self.client.post("/debtors/new", data=form_values, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/debtors/44?saved=1")
        create.assert_called_once()
        submitted = create.call_args.args[3]
        self.assertEqual(submitted["legal_name"], "ООО Учебное дело")
        self.assertEqual(submitted["legal_form"], "ООО")
        self.assertNotIn("snils", submitted)

    def test_incomplete_manual_confirmation_shows_error_without_saving_and_keeps_values(self):
        created = SimpleNamespace(id=43, version=1)
        with patch.object(assignments, "create_manual_debtor_card_once", side_effect=assignments.DraftNotReady("Фамилия и имя обязательны")):
            response = self.client.post("/debtors/new", data={
                "action": "confirm", "first_name": "Анна", "request_key": "c" * 64,
            }, follow_redirects=False)
        self.assertEqual(response.status_code, 422)
        self.assertIn("Фамилия и имя обязательны", response.text)
        self.assertIn('name="first_name" maxlength="512" value="Анна"', response.text)
        self.assertIn('name="request_key" value="', response.text)
        self.db.rollback.assert_called_once()
        self.db.commit.assert_not_called()

    def test_new_assignment_starts_with_reviewed_card_selection(self):
        self.db.scalars.return_value.all.return_value = [SimpleNamespace(
            id=9, display_name="Synthetic debtor", inn="123456789012", updated_at=None,
            values={"case_number": {"value": "А00-1/2026"}, "court": {"value": "Суд"}},
        )]
        response = self.client.get("/assignments/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Выберите карточку должника", response.text)
        self.assertIn("Импортировать данные и создать", response.text)
        self.assertIn("Арбитражный управляющий", response.text)
        self.assertIn("заявитель, даты", response.text)
        self.assertIn("preparation_state", str(self.db.scalars.call_args.args[0]))

    def test_source_task_selection_lists_only_reviewed_debtor_cards(self):
        self.db.get.return_value = SimpleNamespace(id=41, status=TaskStatus.approved)
        with patch.object(routes_assignments.tasks, "approval_matches", return_value=True):
            response = self.client.get("/debtors?source_task_id=41")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Выберите проверенную карточку", response.text)
        self.assertIn("preparation_state", str(self.db.scalars.call_args.args[0]))

    def test_assignment_list_has_shared_search_and_status_filters(self):
        response = self.client.get("/assignments?status=awaiting_information&q=Иванов")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="status"', response.text)
        self.assertIn('value="Иванов"', response.text)
        self.assertIn("Все", response.text)

    def test_worklist_search_treats_like_metacharacters_as_literal_text(self):
        query = r"A%_\B"
        expected = r"%A\%\_\\B%"
        self.assertEqual(routes_assignments._literal_contains_pattern(query), expected)

        response = self.client.get("/assignments", params={"q": query})

        self.assertEqual(response.status_code, 200)
        statement = self.db.scalar.call_args.args[0]
        compiled = statement.compile()
        self.assertIn("ESCAPE", str(compiled).upper())
        self.assertIn(expected, compiled.params.values())

    def test_assignment_editor_explains_internal_only_and_unfilled_fields(self):
        self.db.get.return_value = SimpleNamespace(
            id=5, debtor_card_id=9, source_task_id=41, case_number="А40-123/2026",
            status="draft", version=1, values={},
            debtor_card=SimpleNamespace(id=9, display_name="Synthetic debtor", source_task_id=41),
        )
        self.db.scalars.return_value.all.return_value = []
        response = self.client.get("/assignments/5")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Исходное судебное задание 41", response.text)
        self.assertIn("не выполняется", response.text)
        self.assertIn("Арбитражный управляющий", response.text)
        self.assertIn("Пустое значение не считается ошибкой", response.text)

    def test_debtor_card_assignment_history_is_bounded_and_keyset_paginated(self):
        employee = SimpleNamespace(full_name="Synthetic employee", login="synthetic")
        card = SimpleNamespace(
            id=6, entity_kind="person", version=1, values={}, inn=None,
            display_name="Synthetic debtor", preparation_state="reviewed", source_task_id=None,
            created_by=employee, updated_by=employee,
        )
        self.db.get.return_value = card
        self.db.scalar.return_value = 101
        self.db.scalars.return_value.all.return_value = [
            SimpleNamespace(id=item_id, case_number=f"A99-{item_id}/2099", status="draft")
            for item_id in range(101, 51, -1)
        ]
        self.db.execute.return_value.all.return_value = []

        first = self.client.get("/debtors/6")

        self.assertEqual(first.status_code, 200)
        self.assertIn("50 из 101", first.text)
        self.assertIn('href="/debtors/6?before=52"', first.text)
        first_query = self.db.scalars.call_args.args[0]
        self.assertIn("LIMIT", str(first_query).upper())
        self.assertIn("debtor_card_id", str(first_query))

        self.db.reset_mock()
        self.db.get.return_value = card
        self.db.scalar.side_effect = [101, 50]
        self.db.scalars.return_value.all.return_value = [
            SimpleNamespace(id=item_id, case_number=f"A99-{item_id}/2099", status="draft")
            for item_id in range(51, 1, -1)
        ]
        self.db.execute.return_value.all.return_value = []

        second = self.client.get("/debtors/6?before=52")

        self.assertEqual(second.status_code, 200)
        self.assertIn("Страница 2 из 3", second.text)
        self.assertIn('href="/debtors/6?after=51"', second.text)
        self.assertIn('href="/debtors/6?before=2"', second.text)

    def test_invalid_assignment_save_keeps_submitted_values_for_correction(self):
        draft = SimpleNamespace(
            id=5, debtor_card_id=9, source_task_id=None, case_number="А40-1/2026",
            status="draft", version=2, values={"practitioner": ""},
            debtor_card=SimpleNamespace(id=9, display_name="Synthetic debtor", source_task_id=None),
        )
        self.db.get.return_value = draft
        self.db.scalars.return_value.all.return_value = []
        with patch.object(assignments, "update_assignment_draft", side_effect=assignments.DraftNotReady("Поле слишком длинное")):
            response = self.client.post("/assignments/5", data={
                "version": "1", "case_number": "А40-исправить/2026", "status": "awaiting_information",
                "practitioner": "Синтетический управляющий", "applicant_address": "Синтетический адрес",
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="version" value="1"', response.text)
        self.assertIn('value="А40-исправить/2026"', response.text)
        self.assertIn('value="awaiting_information" selected', response.text)
        self.assertIn("Синтетический управляющий", response.text)
        self.assertIn("Синтетический адрес", response.text)
        self.assertIn("Поле слишком длинное", response.text)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.db.rollback.assert_called_once()
        self.db.commit.assert_not_called()

    def test_invalid_debtor_card_save_keeps_submitted_values_for_correction(self):
        employee = SimpleNamespace(full_name="Synthetic employee", login="synthetic")
        card = SimpleNamespace(
            id=5, entity_kind="legal_entity", version=2, values={
                "legal_name": {"value": "ООО Сохранённое"}, "inn": {"value": ""},
            }, inn=None, display_name="ООО Сохранённое", preparation_state="draft",
            source_task_id=None, created_by=employee, updated_by=employee,
        )
        self.db.get.return_value = card
        self.db.scalars.return_value.all.return_value = []
        self.db.execute.return_value.all.return_value = []
        with patch.object(assignments, "update_debtor_card", side_effect=assignments.DraftNotReady("ИНН не прошёл контрольную сумму")):
            response = self.client.post("/debtors/5", data={
                "version": "1", "action": "confirm", "legal_name": "ООО Исправляемое",
                "inn": "7700000018", "ogrn": "1027700132194", "address": "Синтетический адрес",
                "address_kind": "registration", "case_number": "А99-5/2099", "court": "Синтетический суд",
                "doc_date": "14.10.2099", "doc_type": "определение", "short_name": "ООО Испр.",
                "legal_form": "ООО", "kpp": "770001001",
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="version" value="1"', response.text)
        self.assertIn('value="ООО Исправляемое"', response.text)
        self.assertIn('value="7700000018"', response.text)
        self.assertIn("Синтетический адрес", response.text)
        self.assertIn("Синтетический суд", response.text)
        self.assertIn("ИНН не прошёл контрольную сумму", response.text)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.db.rollback.assert_called_once()
        self.db.commit.assert_not_called()

    def test_oversized_card_name_is_rendered_with_a_correctable_validation_error(self):
        employee = SimpleNamespace(full_name="Synthetic employee", login="synthetic")
        card = SimpleNamespace(
            id=12, entity_kind="legal_entity", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft", source_task_id=None,
            created_by=employee, updated_by=employee,
        )
        self.db.get.return_value = card
        self.db.scalars.return_value.all.return_value = []
        self.db.execute.return_value.all.return_value = []
        submitted_name = "О" * (assignments.MAX_DISPLAY_NAME_CHARACTERS + 1)
        message = "Наименование карточки не должно превышать 512 символов"
        with patch.object(assignments, "update_debtor_card", side_effect=assignments.DraftNotReady(message)):
            response = self.client.post("/debtors/12", data={
                "version": "1", "action": "confirm", "legal_name": submitted_name,
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(message, response.text)
        self.assertIn(f'value="{submitted_name}"', response.text)
        self.assertIn('name="legal_name" maxlength="512"', response.text)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.db.rollback.assert_called_once()
        self.db.commit.assert_not_called()

    def test_invalid_manual_snils_stays_visible_for_correction(self):
        employee = SimpleNamespace(full_name="Synthetic employee", login="synthetic")
        card = SimpleNamespace(
            id=6, entity_kind="person", version=2, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft", source_task_id=None,
            created_by=employee, updated_by=employee,
        )
        self.db.get.return_value = card
        self.db.scalars.return_value.all.return_value = []
        self.db.execute.return_value.all.return_value = []
        with patch.object(
            assignments, "update_debtor_card",
            side_effect=assignments.DraftNotReady("СНИЛС должен пройти проверку контрольной суммы"),
        ):
            response = self.client.post("/debtors/6", data={
                "version": "2", "action": "confirm", "last_name": "Синтетический",
                "first_name": "Должник", "snils": "11223344596",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="snils" maxlength="2000" value="11223344596"', response.text)
        self.assertIn("СНИЛС должен пройти проверку", response.text)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.db.rollback.assert_called_once()
        self.db.commit.assert_not_called()

    def test_absent_optional_fields_render_empty_instead_of_literal_none(self):
        employee = SimpleNamespace(full_name="Synthetic employee", login="synthetic")
        self.db.get.return_value = SimpleNamespace(
            id=91, display_name="Synthetic person", inn=None, source_task_id=None,
            preparation_state="prepared", version=1,
            created_by=employee, updated_by=employee,
            values={"snils": {"value": None}, "court": {"value": None}},
        )
        self.db.execute.return_value.all.return_value = []
        response = self.client.get("/debtors/91")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="snils" maxlength="2000" value=""', response.text)
        self.assertIn('name="court" rows="2" maxlength="2000"></textarea>', response.text)
        self.assertNotIn('value="None"', response.text)

    def test_card_creation_route_only_redirects_to_internal_record(self):
        created = SimpleNamespace(id=91)
        with patch.object(assignments, "create_debtor_card_from_task", return_value=created) as create:
            response = self.client.post("/tasks/55/internal-debtor-card", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/debtors/91?created=1")
        create.assert_called_once_with(self.db, 55, 7)
        self.db.commit.assert_called_once()

    def test_confirmed_task_can_be_explicitly_linked_to_a_chosen_debtor(self):
        task = SimpleNamespace(
            id=41, status=TaskStatus.approved, case_number="А40-123/2026",
            values={"case_number": "А40-123/2026"},
            recognized={"court": {"value": "Синтетический арбитражный суд", "confidence": 0.9}},
        )
        self.db.get.return_value = task
        created = SimpleNamespace(id=21)
        with (patch.object(assignments.task_service, "approval_matches", return_value=True),
              patch.object(assignments, "create_assignment_draft", return_value=created) as create):
            response = self.client.post("/tasks/41/debtors/9/assignments", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/assignments/21?created=1")
        create.assert_called_once_with(
            self.db, 9, 7, source_task_id=41,
            source_case_number="А40-123/2026", source_court="Синтетический арбитражный суд",
        )
        self.db.commit.assert_called_once()

    def test_unconfirmed_task_cannot_be_linked_to_an_existing_debtor(self):
        self.db.get.return_value = SimpleNamespace(id=41, status=TaskStatus.needs_review)
        with patch.object(assignments, "create_assignment_draft") as create:
            response = self.client.post("/tasks/41/debtors/9/assignments")
        self.assertEqual(response.status_code, 409)
        create.assert_not_called()

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
            last_name="Петров", first_name="Иван", middle_name="Иванович",
            inn="772881234508".translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")),
        )
        updated = assignments.update_debtor_card(self.db, 9, 22, 2, corrected_values)
        self.assertIs(updated, card)
        self.assertEqual(card.display_name, "Петров Иван Иванович")
        self.assertEqual(card.inn, "772881234508")
        self.assertEqual(card.values["inn"]["value"], "772881234508")
        self.assertEqual(card.values["last_name"]["source"], "employee")
        self.assertEqual(card.updated_by_id, 22)
        self.assertEqual(card.version, 3)
        self.db.flush.assert_called_once()

    def test_stale_replay_of_already_applied_debtor_card_edit_is_idempotent(self):
        card = SimpleNamespace(
            id=9,
            version=3,
            values={
                "last_name": {
                    "value": "Петров", "source": "employee",
                    "warning": None, "confidence": 1.0,
                },
                "first_name": {
                    "value": "Иван", "source": "employee",
                    "warning": None, "confidence": 1.0,
                },
            },
            inn=None,
            display_name="Петров Иван",
            preparation_state="reviewed",
        )
        self.db.scalar.return_value = card
        values = {name: "" for name in assignments.DEBTOR_CARD_FIELDS}
        values.update(last_name="Петров", first_name="Иван")

        replay = assignments.update_debtor_card(self.db, 9, 22, 2, values)

        self.assertIs(replay, card)
        self.assertEqual(card.version, 3)
        self.db.flush.assert_not_called()
        changed = dict(values, first_name="Мария")
        with self.assertRaises(assignments.DraftConflict):
            assignments.update_debtor_card(self.db, 9, 22, 2, changed)
        self.assertEqual(card.version, 3)
        self.db.flush.assert_not_called()

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

    def test_manual_person_card_rejects_invalid_snils_without_mutation(self):
        for snils in ("11223344596", "1122334459x"):
            with self.subTest(snils=snils):
                card = SimpleNamespace(
                    id=25, entity_kind="person", version=1, values={}, inn=None,
                    display_name="Новая карточка", preparation_state="draft",
                )
                self.db.scalar.return_value = card
                values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["person"]}
                values.update(last_name="Синтетический", first_name="Должник", snils=snils)

                with self.assertRaises(assignments.DraftNotReady):
                    assignments.update_debtor_card(self.db, card.id, 7, 1, values)

                self.assertEqual(card.values, {})
                self.assertEqual(card.display_name, "Новая карточка")
                self.assertEqual(card.version, 1)
                self.db.flush.assert_not_called()
                self.db.reset_mock()

    def test_manual_person_card_normalizes_valid_snils(self):
        card = SimpleNamespace(
            id=26, entity_kind="person", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.return_value = card
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["person"]}
        values.update(last_name="Синтетический", first_name="Должник", snils="11223344595")

        assignments.update_debtor_card(self.db, card.id, 7, 1, values)

        self.assertEqual(card.values["snils"]["value"], "112-233-445 95")
        self.assertEqual(card.preparation_state, "reviewed")
        self.assertEqual(card.version, 2)

    def test_manual_cards_reject_all_zero_inn_for_person_and_organization(self):
        for entity_kind, value_field, inn in (
            ("person", "last_name", "000000000000"),
            ("legal_entity", "legal_name", "0000000000"),
        ):
            with self.subTest(entity_kind=entity_kind):
                card = SimpleNamespace(
                    id=24, entity_kind=entity_kind, version=1, values={}, inn=None,
                    display_name="Новая карточка", preparation_state="draft",
                )
                self.db.scalar.return_value = card
                field_names = assignments.ENTITY_CARD_FIELDS[entity_kind]
                values = {name: "" for name in field_names}
                values.update({value_field: "Синтетическое имя", "inn": inn})

                with self.assertRaises(assignments.DraftNotReady):
                    assignments.update_debtor_card(self.db, card.id, 7, 1, values)

                self.assertEqual(card.display_name, "Новая карточка")
                self.assertEqual(card.version, 1)
                self.db.flush.assert_not_called()
                self.db.reset_mock()

    def test_legal_entity_card_accepts_ten_digit_inn_and_uses_organization_name(self):
        card = SimpleNamespace(
            id=21, entity_kind="legal_entity", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.side_effect = [card, None]
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
        values.update(legal_name="ООО Учебное дело", legal_form="ООО", inn="7707083893")

        updated = assignments.update_debtor_card(self.db, 21, 7, 1, values)

        self.assertIs(updated, card)
        self.assertEqual(card.entity_kind, "legal_entity")
        self.assertEqual(card.display_name, "ООО Учебное дело")
        self.assertEqual(card.inn, "7707083893")
        self.assertEqual(card.values["legal_form"]["value"], "ООО")
        self.assertEqual(card.preparation_state, "reviewed")

    def test_legal_entity_card_rejects_person_length_inn(self):
        card = SimpleNamespace(
            id=22, entity_kind="legal_entity", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.return_value = card
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
        values.update(legal_name="ООО Учебное дело", inn="772881234508")

        with self.assertRaisesRegex(assignments.DraftNotReady, "10 цифр"):
            assignments.update_debtor_card(self.db, 22, 7, 1, values)

        self.assertEqual(card.display_name, "Новая карточка")
        self.assertEqual(card.version, 1)
        self.db.flush.assert_not_called()

    def test_sole_proprietor_card_uses_its_own_name_field_and_twelve_digit_inn(self):
        card = SimpleNamespace(
            id=23, entity_kind="sole_proprietor", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.side_effect = [card, None]
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["sole_proprietor"]}
        values.update(debtor_name="Иванов Иван Иванович", inn="772881234508")

        assignments.update_debtor_card(self.db, 23, 7, 1, values)

        self.assertEqual(card.display_name, "Иванов Иван Иванович")
        self.assertEqual(card.inn, "772881234508")
        self.assertNotIn("snils", card.values)

    def test_incomplete_card_can_be_saved_as_draft_but_not_confirmed(self):
        card = SimpleNamespace(
            id=10, version=1, values={}, inn=None, display_name="Новая карточка",
            preparation_state="draft",
        )
        self.db.scalar.return_value = card
        values = {name: "" for name in assignments.DEBTOR_CARD_FIELDS}
        values["first_name"] = "Иван"
        updated = assignments.update_debtor_card(self.db, 10, 18, 1, values, confirm=False)
        self.assertIs(updated, card)
        self.assertEqual(card.preparation_state, "draft")
        self.assertEqual(card.display_name, "Иван")
        self.assertEqual(card.version, 2)
        self.db.flush.assert_called_once()

        card.version = 2
        with self.assertRaisesRegex(assignments.DraftNotReady, "Фамилия и имя"):
            assignments.update_debtor_card(self.db, 10, 18, 2, values, confirm=True)

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
        localized_inn = "772881234508".translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
        values.update(last_name="Петров", first_name="Иван", inn=localized_inn)
        with self.assertRaises(assignments.DuplicateDraft):
            assignments.update_debtor_card(self.db, 9, 22, 2, values)
        self.assertEqual(card.display_name, "Иванов Иван")
        self.assertEqual(card.version, 2)
        duplicate_query = self.db.scalar.call_args_list[1].args[0]
        self.assertIn("772881234508", duplicate_query.compile().params.values())
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
