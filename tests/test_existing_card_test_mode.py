from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.api import routes_assignments, routes_tasks
from app.db.models import TaskStatus
from app.settings import Settings
from app.web.templates import templates


class ExistingCardTestModeSettingsTests(unittest.TestCase):
    def load_settings(self, **environment: str) -> Settings:
        import tempfile

        with tempfile.TemporaryDirectory() as upload_dir:
            isolated_environment = {
                "SRO_UPLOAD_DIR": upload_dir,
                "DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:55432/sro",
                "REDIS_URL": "redis://synthetic:synthetic@127.0.0.1:56379/0",
                "APP_DEBUG": "1",
                **environment,
            }
            with patch("app.settings.load_env"), patch.dict(os.environ, isolated_environment, clear=True):
                return Settings.load()

    def test_existing_card_test_mode_defaults_off_and_accepts_explicit_enable(self) -> None:
        self.assertFalse(self.load_settings().sro_existing_card_test_mode)
        self.assertTrue(self.load_settings(SRO_EXISTING_CARD_TEST_MODE="1").sro_existing_card_test_mode)

    def test_existing_card_test_mode_rejects_non_binary_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "SRO_EXISTING_CARD_TEST_MODE"):
            self.load_settings(SRO_EXISTING_CARD_TEST_MODE="true")


class ExistingCardTestModeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_tampered_save_post_is_rejected_before_preview_decision(self) -> None:
        request = SimpleNamespace(
            form=AsyncMock(return_value={"decision": "save", "preview_token": "valid-looking-token"}),
        )
        task = SimpleNamespace(id=31, preview_mode="live_session")
        db = MagicMock()

        with (
            patch("app.api.routes_tasks.settings.sro_existing_card_test_mode", True),
            patch("app.api.routes_tasks._require_task", return_value=task),
            patch("app.api.routes_tasks.tasks.decide_preview") as decide_preview,
        ):
            with self.assertRaises(HTTPException) as caught:
                await routes_tasks.task_decision(request, task.id, SimpleNamespace(id=8), db)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("Тестовый режим", caught.exception.detail)
        decide_preview.assert_not_called()
        db.commit.assert_not_called()

    def test_task_page_disables_save_and_passes_test_mode_to_review_template(self) -> None:
        request = SimpleNamespace(query_params={}, scope={})
        user = SimpleNamespace(id=8)
        task = SimpleNamespace(
            id=31, document=None, scenario="person_card", preview_mode="live_session",
            status=TaskStatus.awaiting_preview,
            intake_item_id=None, intake_operation_id=None,
            recognized={}, values={}, browser_decision=None, error="", screenshots=[],
        )
        db = MagicMock()
        db.query.return_value.filter_by.return_value.one_or_none.return_value = None
        response = SimpleNamespace(headers={})
        with (
            patch("app.api.routes_tasks.settings.sro_existing_card_test_mode", True),
            patch("app.api.routes_tasks._require_task", return_value=task),
            patch("app.api.routes_tasks._review_operation_scope", return_value=[]),
            patch("app.api.routes_tasks._ocr_retry_review_scope", return_value=None),
            patch("app.api.routes_tasks.case_packages.active_case_packages_for_task", return_value=[]),
            patch("app.api.routes_tasks.fields.review_fields", return_value=[]),
            patch("app.api.routes_tasks.tasks.status_label", return_value="Ожидает просмотра"),
            patch("app.api.routes_tasks.tasks.approval_matches", return_value=False),
            patch("app.api.routes_tasks.tasks.review_token", return_value="review-token"),
            patch("app.api.routes_tasks.tasks.preview_token", return_value="preview-token"),
            patch("app.api.routes_tasks.tasks.can_save_preview", return_value=True),
            patch("app.api.routes_tasks.ocr_retries.context_for_task", return_value={}),
            patch("app.api.routes_tasks.templates.TemplateResponse", return_value=response) as render,
        ):
            routes_tasks.task_page(request, task.id, user, db)

        context = render.call_args.args[2]
        self.assertTrue(context["existing_card_test_mode"])
        self.assertFalse(context["preview_can_save"])

    def test_person_card_page_passes_test_mode_to_template(self) -> None:
        request = SimpleNamespace(query_params={})
        card = SimpleNamespace(
            id=41, entity_kind="person", source_task_id=None, values={},
            preparation_state="draft", version=1, display_name="Тестовый человек",
        )
        db = MagicMock()
        db.get.side_effect = lambda model, _id: card if model.__name__ == "DebtorCardDraft" else None
        response = SimpleNamespace(headers={})
        page = ([], 0, 1, 1, None, None)
        with (
            patch("app.api.routes_assignments.settings.sro_existing_card_test_mode", True),
            patch("app.api.routes_assignments._worklist_page", return_value=page),
            patch("app.api.routes_assignments._card_revision_page", return_value=page),
            patch("app.api.routes_assignments.assignments.debtor_card_progress", return_value={"record_url": ""}),
            patch("app.api.routes_assignments.templates.TemplateResponse", return_value=response) as render,
        ):
            routes_assignments._render_debtor_card_page(request, card.id, SimpleNamespace(id=8), db)

        context = render.call_args.args[2]
        self.assertTrue(context["sro_existing_card_test_mode"])


class ExistingCardTestModeTemplateTests(unittest.TestCase):
    @staticmethod
    def _request() -> Request:
        return Request({
            "type": "http", "method": "GET", "path": "/synthetic",
            "raw_path": b"/synthetic", "query_string": b"", "headers": [],
            "scheme": "http", "server": ("localhost", 80),
            "client": ("127.0.0.1", 1),
        })

    @staticmethod
    def _url_for(_endpoint: str, **_values: object) -> str:
        class Url(str):
            def include_query_params(self, **_params: object) -> "Url":
                return self

        return Url("/synthetic")

    def test_review_preview_hides_save_but_keeps_close_and_explains_test_mode(self) -> None:
        task = SimpleNamespace(
            id=31, status=TaskStatus.awaiting_preview, browser_decision=None,
            error="", screenshots=[], scenario="person_card", document=None,
        )
        request = self._request()
        html = templates.env.get_template("review.html").render(
            task=task, request=request, url_for=self._url_for,
            TaskStatus=TaskStatus, fields=[], preview_token="token",
            preview_can_save=False, existing_card_test_mode=True,
            ocr_review_operation_id=None, review_intake_query="",
            ocr_retry_context=SimpleNamespace(incoming_run=None, outgoing_run=None),
            review_conflict=False, review_recovery=False,
            review_asset_versions={"field-source.js": "v", "staff-controls.js": "v"},
            user=SimpleNamespace(full_name="Тест", login="test"),
        )
        self.assertNotIn("Сохранить карточку в СРОАрбитре", html)
        self.assertIn("Закрыть без сохранения", html)
        self.assertIn("проверка дубликатов отключена", html)
        self.assertNotIn("Сохранение доступно сотруднику, который запустил", html)

    def test_person_card_sro_panel_shows_test_mode_warning(self) -> None:
        card = SimpleNamespace(
            id=41, entity_kind="person", preparation_state="draft", version=1,
            display_name="Тестовый человек", values={}, source_task_id=None,
        )
        html = templates.env.get_template("debtor_card.html").render(
            request=self._request(), url_for=self._url_for,
            card=card, user=SimpleNamespace(full_name="Тест", login="test"),
            card_progress={"state": "draft", "label": "Черновик", "badge": "draft", "record_url": ""},
            source_task=None, source_document_available=False,
            source_task_approved=False, source_review_token="", sro_source_matches_card=False,
            source_assignment=None, assignments=[], assignment_total=0, assignment_page=1,
            assignment_pages=1, assignment_previous_url=None, assignment_next_url=None,
            revision_total=0, revision_page=1, revision_pages=1,
            revision_previous_url=None, revision_next_url=None,
            fields=[], entity_fields=[], entity_kind_label="Физическое лицо",
            related_person_card=None, person_card_choices=[], person_card_query="",
            error="", notice="", conflict_values=None, conflict_rows=[], form_values={},
            form_initially_dirty=False, validation_message="", assignment_request_key="key",
            sro_existing_card_test_mode=True,
        )
        self.assertIn("Тестовый режим: проверка дубликатов отключена", html)
        self.assertIn("сохранение в СРОАрбитр недоступно", html)


if __name__ == "__main__":
    unittest.main()
