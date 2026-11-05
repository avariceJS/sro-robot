from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.api import routes_assignments, routes_tasks
from app.api.deps import current_user
from app.db.base import get_session
from app.db.models import TaskStatus
from app.services import assignments


def _person_values(**overrides):
    values = {name: "" for name in assignments.ENTITY_CARD_FIELDS["person"]}
    values.update(last_name="Иванов", first_name="Иван", case_number="А99-1/2099")
    values.update(overrides)
    return values


def _person_card(*, state="reviewed", source_task_id=55, version=3):
    employee = SimpleNamespace(full_name="Synthetic employee", login="synthetic")
    values = _person_values()
    return SimpleNamespace(
        id=5, entity_kind="person", display_name="Иванов Иван", inn="",
        values={name: {"value": value} for name, value in values.items()},
        version=version, preparation_state=state, source_task_id=source_task_id,
        created_by=employee, updated_by=employee,
    )


def _person_task(*, status=TaskStatus.approved, browser_decision="", dry_run=True, record_url=""):
    return SimpleNamespace(
        id=55, scenario="person_card", status=status, recognized={}, values={},
        approved_by_id=7 if status == TaskStatus.approved else None,
        updated_at=datetime(2026, 11, 5), browser_decision=browser_decision,
        dry_run=dry_run, record_url=record_url, document=None,
    )


class PersonCardActionRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(routes_assignments.router)
        self.app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
        self.app.add_api_route("/", lambda: {}, name="tasks_page")
        self.app.add_api_route("/debtors/intake", lambda: {}, name="debtor_intake_page")
        self.app.add_api_route("/logout", lambda: {}, name="logout")
        self.app.add_api_route("/tasks/{task_id}", lambda task_id: {}, name="task_page")
        self.app.add_api_route("/tasks/{task_id}/prepare-sro-form", lambda task_id: {}, methods=["POST"], name="task_prepare_sro_form")
        self.app.add_api_route("/tasks/{task_id}/document", lambda task_id: {}, name="task_document")
        self.app.add_api_route("/tasks/{task_id}/document/preview", lambda task_id: {}, name="task_document_preview")
        self.db = MagicMock()
        self.db.scalar.return_value = None
        self.db.scalars.return_value.all.return_value = []
        self.db.execute.return_value.all.return_value = []
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(
            id=7, login="synthetic", full_name="Synthetic employee",
        )
        self.app.dependency_overrides[get_session] = lambda: self.db
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def test_reopened_draft_has_three_actions_and_keeps_source_review_token(self):
        card = _person_card(state="draft")
        task = _person_task()
        self.db.get.side_effect = [card, task]
        with patch.object(routes_assignments.tasks, "approval_matches", return_value=True):
            response = self.client.get("/debtors/5")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count('name="action" value="'), 3)
        for action in ("close", "assignment", "preview"):
            self.assertIn(f'name="action" value="{action}"', response.text)
        self.assertIn("Заполнить форму СРО", response.text)
        self.assertIn('name="request_key" value="', response.text)
        self.assertIn('name="source_review_token" value="', response.text)
        self.assertNotIn('name="action" value="draft"', response.text)
        self.assertNotIn('name="action" value="confirm"', response.text)
        self.assertNotIn('action="http://testserver/tasks/55/prepare-sro-form"', response.text)

    def test_close_saves_dirty_card_fields_and_redirects_without_confirmation_action(self):
        card = _person_card(state="reviewed", source_task_id=None)
        self.db.get.return_value = card
        submitted = _person_values(last_name="Петров", address="Синтетический адрес")
        with patch.object(assignments, "update_debtor_card", return_value=card) as update:
            response = self.client.post("/debtors/5", data={
                "version": "3", "action": "close", **submitted,
            }, follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/debtors?registry=persons")
        update.assert_called_once_with(self.db, 5, 7, 3, submitted, confirm=True)
        self.db.commit.assert_called_once()

    def test_assignment_action_passes_version_values_and_review_token_to_idempotent_helper(self):
        card = _person_card(state="draft")
        task = _person_task()
        self.db.get.side_effect = [card, task]
        self.db.scalar.return_value = None
        created = SimpleNamespace(id=81)
        values = _person_values(first_name="Павел")
        submitted = {"version": "3", "action": "assignment", "request_key": "a" * 64,
                     "source_review_token": "review-token", **values}
        with (patch.object(routes_assignments.tasks, "lock_task_for_update"),
              patch.object(routes_assignments.tasks, "approval_matches", return_value=True),
              patch.object(assignments, "create_assignment_draft_once", return_value=created) as create):
            response = self.client.post("/debtors/5", data=submitted, follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/assignments/81")
        self.assertEqual(create.call_args.args, (self.db, 5, 7, "a" * 64))
        self.assertEqual(create.call_args.kwargs["expected_version"], 3)
        self.assertEqual(create.call_args.kwargs["values"], values)
        self.assertIs(create.call_args.kwargs["source_task"], task)
        self.assertEqual(create.call_args.kwargs["source_review_token"], "review-token")
        self.db.commit.assert_called_once()

    def test_approved_preview_saves_and_syncs_before_delegating_to_native_preview(self):
        card = _person_card(state="draft")
        task = _person_task()
        self.db.get.side_effect = [card, task]
        values = _person_values(first_name="Павел")
        updated = _person_card(state="reviewed")
        events = []

        def save(*args, **kwargs):
            events.append("save")
            return updated

        def sync(*args, **kwargs):
            events.append("sync")

        def preview(request, task_id, user, db):
            events.append("preview")
            self.assertEqual(task_id, 55)
            return RedirectResponse("/tasks/55?preview=ready", status_code=303)

        with (patch.object(routes_assignments.tasks, "lock_task_for_update"),
              patch.object(routes_assignments.tasks, "approval_matches", return_value=True),
              patch.object(routes_assignments.tasks, "assert_review_token"),
              patch.object(assignments, "update_debtor_card", side_effect=save) as save_card,
              patch.object(assignments, "sync_card_to_approved_task", side_effect=sync) as sync_card,
              patch.object(routes_tasks, "task_prepare_sro_form", side_effect=preview)):
            response = self.client.post("/debtors/5", data={
                "version": "3", "action": "preview", "source_review_token": "review-token", **values,
            }, follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/tasks/55?preview=ready")
        self.assertEqual(events, ["save", "sync", "preview"])
        save_card.assert_called_once_with(self.db, 5, 7, 3, values, confirm=True)
        sync_card.assert_called_once_with(self.db, updated, task, 7, unittest.mock.ANY)

    def test_unknown_and_closed_preview_results_save_edits_and_return_to_source_without_retry(self):
        for decision in ("save_unknown", "closed"):
            with self.subTest(decision=decision):
                card = _person_card(state="reviewed")
                task = _person_task(status=TaskStatus.needs_review, browser_decision=decision)
                self.db.get.side_effect = [card, task]
                values = _person_values(last_name="Сидоров")
                with (patch.object(routes_assignments.tasks, "lock_task_for_update"),
                      patch.object(routes_assignments.tasks, "approval_matches", return_value=False),
                      patch.object(assignments, "update_debtor_card", return_value=card) as save,
                      patch.object(routes_tasks, "task_prepare_sro_form") as native_preview):
                    response = self.client.post("/debtors/5", data={
                        "version": "3", "action": "preview", **values,
                    }, follow_redirects=False)

                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/tasks/55#sro-preview")
                save.assert_called_once_with(self.db, 5, 7, 3, values, confirm=True)
                native_preview.assert_not_called()
                self.db.commit.assert_called_once()
                self.db.reset_mock()


class PersonCardProgressTests(unittest.TestCase):
    def test_created_requires_verified_trusted_saved_record_receipt(self):
        card = SimpleNamespace(preparation_state="draft")
        trusted_url = "https://sroarbitr.sro-service.ru/register/person_card.aspx?id=123"
        verified = SimpleNamespace(
            scenario="person_card", browser_decision="saved", dry_run=False, record_url=trusted_url,
        )
        progress = assignments.debtor_card_progress(card, verified)
        self.assertEqual(progress, {
            "state": "created", "label": "Карточка создана", "badge": "reviewed", "record_url": trusted_url,
        })

        for task in (
            SimpleNamespace(scenario="person_card", browser_decision="saved", dry_run=True, record_url=trusted_url),
            SimpleNamespace(scenario="person_card", browser_decision="closed", dry_run=False, record_url=trusted_url),
            SimpleNamespace(scenario="person_card", browser_decision="saved", dry_run=False,
                            record_url="https://attacker.example/register/person_card.aspx?id=123"),
        ):
            with self.subTest(task=task):
                self.assertNotEqual(assignments.debtor_card_progress(card, task)["state"], "created")
                self.assertEqual(assignments.debtor_card_progress(card, task)["record_url"], "")

    def test_unreviewed_ocr_card_is_prepared_and_unknown_save_requires_reconciliation(self):
        prepared_card = SimpleNamespace(preparation_state="prepared")
        self.assertEqual(assignments.debtor_card_progress(prepared_card, None)["state"], "prepared")

        unknown_task = SimpleNamespace(browser_decision="save_unknown", dry_run=True, record_url="")
        progress = assignments.debtor_card_progress(SimpleNamespace(preparation_state="reviewed"), unknown_task)
        self.assertEqual(progress["state"], "reconcile")
        self.assertEqual(progress["label"], "Требуется сверка")
        self.assertEqual(progress["record_url"], "")


if __name__ == "__main__":
    unittest.main()
