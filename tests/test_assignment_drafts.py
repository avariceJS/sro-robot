from __future__ import annotations

import unittest
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
        self.db.scalar.side_effect = [self.task, None, None]
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
        self.assertEqual(created.source_task_id, 41)
        self.db.add.assert_called_once_with(created)

    def test_manual_organization_card_validates_and_normalizes_registry_ids(self):
        debtor = SimpleNamespace(
            id=29, entity_kind="legal_entity", version=1, values={}, inn=None,
            display_name="Новая карточка", preparation_state="draft",
        )
        self.db.scalar.return_value = debtor
        values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["legal_entity"]}
        values.update(
            legal_name="ООО Синтетика", inn="", ogrn="1 0277 0013 2195",
            kpp="770 001 001",
        )
        with patch.object(assignments, "_lock_inn"):
            updated = assignments.update_debtor_card(self.db, 29, 7, 1, values, confirm=True)
        self.assertEqual(updated.values["ogrn"]["value"], "1027700132195")
        self.assertEqual(updated.values["kpp"]["value"], "770001001")

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
        task = SimpleNamespace(id=41)
        existing = SimpleNamespace(id=18, debtor_card_id=9, source_task_id=41)
        self.db.scalar.side_effect = [task, debtor, None, task, debtor, existing]
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
        self.db.scalar.side_effect = [SimpleNamespace(id=41), debtor, None,
                                      SimpleNamespace(id=42), debtor, None]
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
        self.assertIn('name="first_name" maxlength="2000" value="Анна"', response.text)
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
        self.assertIn("незаполненное не считается ошибкой", response.text)

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
