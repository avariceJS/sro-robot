from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from app.db.models import TaskStatus
from app.api.routes_tasks import task_approve, task_decision, task_restore_review_proposals, task_save
from app.api.review_conflicts import review_conflict_response, review_recovery_response


class TaskOperatorAuditRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_review_conflict_marks_different_submitted_values_as_unsaved(self) -> None:
        request = SimpleNamespace()
        task = SimpleNamespace(
            id=31, document=None, scenario="person_card", status="needs_review",
            recognized={}, values={},
        )
        user = SimpleNamespace(id=73)
        db = MagicMock()
        fields = [SimpleNamespace(
            attr="last_name", value="Иванов", warning="Варианты расходятся", uncertain=True,
        )]
        rendered = SimpleNamespace(headers={})

        with (
            patch("app.api.review_conflicts.fields.review_fields", return_value=fields),
            patch("app.api.review_conflicts.tasks.status_label", return_value="Проверка"),
            patch("app.api.review_conflicts.tasks.approval_matches", return_value=False),
            patch("app.api.review_conflicts.templates.TemplateResponse", return_value=rendered) as render,
        ):
            response = review_conflict_response(
                request, task, user, db, {"last_name": "Петров"},
            )

        self.assertIs(response, rendered)
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        context = render.call_args.args[2]
        self.assertTrue(context["review_conflict_dirty"])
        self.assertTrue(context["has_review_uncertainty"])
        self.assertEqual(context["submitted_review_values"], {"last_name": "Петров"})
        self.assertEqual(context["current_review_values"], {"last_name": "Иванов"})
        self.assertEqual(context["review_token"], "")

    async def test_stale_review_conflict_does_not_mark_identical_values_as_unsaved(self) -> None:
        request = SimpleNamespace()
        task = SimpleNamespace(
            id=31, document=None, scenario="person_card", status="needs_review",
            recognized={}, values={},
        )
        db = MagicMock()
        with (
            patch(
                "app.api.review_conflicts.fields.review_fields",
                return_value=[SimpleNamespace(attr="last_name", value="Иванов")],
            ),
            patch("app.api.review_conflicts.tasks.status_label", return_value="Проверка"),
            patch("app.api.review_conflicts.tasks.approval_matches", return_value=False),
            patch("app.api.review_conflicts.templates.TemplateResponse", return_value=SimpleNamespace(headers={})) as render,
        ):
            review_conflict_response(
                request, task, SimpleNamespace(id=73), db, {"last_name": "Иванов"},
            )

        self.assertFalse(render.call_args.args[2]["review_conflict_dirty"])

    async def test_review_recovery_keeps_current_values_and_filters_unknown_or_invalid_select_proposals(self) -> None:
        request = SimpleNamespace()
        task = SimpleNamespace(
            id=31, status=TaskStatus.needs_review, document=None, recognized={},
        )
        user = SimpleNamespace(id=73)
        db = MagicMock()
        current_fields = [
            SimpleNamespace(attr="applicant", value="Сохранено сейчас", choices=()),
            SimpleNamespace(
                attr="entity_kind", value="person",
                choices=(("person", "Физическое лицо"), ("legal_entity", "Юридическое лицо")),
            ),
        ]
        rendered = SimpleNamespace(headers={})

        with (
            patch("app.api.review_conflicts.fields.review_fields", return_value=current_fields),
            patch("app.api.review_conflicts.tasks.status_label", return_value="Проверка"),
            patch("app.api.review_conflicts.tasks.approval_matches", return_value=False),
            patch("app.api.review_conflicts.tasks.review_token", return_value="fresh-current-token") as token,
            patch("app.api.review_conflicts.templates.TemplateResponse", return_value=rendered) as render,
        ):
            response = review_recovery_response(
                request, task, user, db,
                {
                    "applicant": "Предложено ранее",
                    "entity_kind": "unknown-kind",
                    "private_metadata": "must be dropped",
                },
                operation_ids=[9],
            )

        context = render.call_args.args[2]
        self.assertIs(response, rendered)
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertFalse(context["review_conflict"])
        self.assertTrue(context["review_recovery"])
        self.assertEqual(context["review_token"], "fresh-current-token")
        self.assertEqual(context["current_review_values"], {
            "applicant": "Сохранено сейчас", "entity_kind": "person",
        })
        self.assertEqual(context["submitted_review_values"], {
            "applicant": "Предложено ранее", "entity_kind": "unknown-kind",
        })
        self.assertEqual(context["copyable_review_values"], {"applicant": "Предложено ранее"})
        self.assertTrue(context["review_conflict_dirty"])
        token.assert_called_once_with(task)
        db.add.assert_not_called()
        db.commit.assert_not_called()
        db.delete.assert_not_called()

    async def test_review_recovery_for_noneditable_task_has_no_save_token(self) -> None:
        task = SimpleNamespace(
            id=31, status=TaskStatus.failed, document=None, recognized={},
        )
        db = MagicMock()
        current_fields = [SimpleNamespace(attr="applicant", value="Текущее значение", choices=())]
        with (
            patch("app.api.review_conflicts.fields.review_fields", return_value=current_fields),
            patch("app.api.review_conflicts.tasks.status_label", return_value="Ошибка"),
            patch("app.api.review_conflicts.tasks.review_token") as token,
            patch("app.api.review_conflicts.templates.TemplateResponse", return_value=SimpleNamespace(headers={})) as render,
        ):
            response = review_recovery_response(
                SimpleNamespace(), task, SimpleNamespace(id=73), db,
                {"applicant": "Прежнее предложение"},
            )

        self.assertEqual(render.call_args.args[2]["review_token"], "")
        self.assertFalse(render.call_args.args[2]["review_conflict"])
        self.assertTrue(render.call_args.args[2]["review_recovery"])
        self.assertTrue(render.call_args.args[2]["review_conflict_dirty"])
        token.assert_not_called()
        db.add.assert_not_called()
        db.commit.assert_not_called()

    async def test_review_recovery_post_filters_fields_and_preserves_intake_scope_without_writes(self) -> None:
        request = SimpleNamespace(form=AsyncMock(return_value={"applicant": "Synthetic proposal"}))
        task = SimpleNamespace(id=31)
        user = SimpleNamespace(id=73)
        db = MagicMock()
        response = SimpleNamespace(status_code=200)

        with (
            patch("app.api.routes_tasks._require_task", return_value=task),
            patch("app.api.routes_tasks._values_from_form", new=AsyncMock(return_value={"applicant": "Synthetic proposal"})),
            patch("app.api.routes_tasks._review_operation_scope", return_value=[9]) as scope,
            patch("app.api.routes_tasks.review_recovery_response", return_value=response) as render,
        ):
            actual = await task_restore_review_proposals(request, 31, user, db)

        self.assertIs(actual, response)
        scope.assert_called_once_with(db, task, None)
        render.assert_called_once_with(
            request, task, user, db, {"applicant": "Synthetic proposal"},
            operation_ids=[9], ocr_review_operation_id=None,
        )
        db.add.assert_not_called()
        db.commit.assert_not_called()

    async def test_field_save_passes_actor_and_request_id_to_audit_service(self) -> None:
        request_id = "a" * 32
        request = SimpleNamespace(
            scope={"state": {"request_id": request_id}}, query_params={},
            form=AsyncMock(return_value={"review_token": "synthetic-token"}),
        )
        task = SimpleNamespace(id=31)
        user = SimpleNamespace(id=73)
        db = MagicMock()
        db.get.return_value = task

        with (
            patch(
                "app.api.routes_tasks._values_from_form",
                new=AsyncMock(return_value={"debtor_name": "Synthetic Person"}),
            ),
            patch("app.api.routes_tasks.tasks.assert_review_token") as assert_token,
            patch("app.api.routes_tasks.tasks.save_values") as save_values,
        ):
            response = await task_save(request, task.id, user, db)

        self.assertEqual(response.status_code, 303)
        assert_token.assert_called_once_with(db, task, "synthetic-token")
        save_values.assert_called_once_with(
            db,
            task,
            {"debtor_name": "Synthetic Person"},
            user.id,
            request_id=request_id,
        )
        db.commit.assert_called_once_with()

    async def test_stale_field_save_commits_audit_without_recording_submitted_values(self) -> None:
        request_id = "d" * 32
        request = SimpleNamespace(
            scope={"state": {"request_id": request_id}}, query_params={},
            form=AsyncMock(return_value={"review_token": "synthetic-token"}),
        )
        task = SimpleNamespace(id=34)
        user = SimpleNamespace(id=76)
        db = MagicMock()
        db.get.return_value = task
        submitted_values = {"debtor_name": "Synthetic Private Name"}
        conflict_response = SimpleNamespace(status_code=409)

        with (
            patch(
                "app.api.routes_tasks._values_from_form",
                new=AsyncMock(return_value=submitted_values),
            ),
            patch("app.api.routes_tasks.tasks.assert_review_token") as assert_token,
            patch(
                "app.api.routes_tasks.tasks.save_values",
                side_effect=ValueError("synthetic state conflict"),
            ),
            patch(
                "app.api.routes_tasks.review_conflict_response",
                return_value=conflict_response,
            ) as render_conflict,
        ):
            response = await task_save(request, task.id, user, db)

        self.assertIs(response, conflict_response)
        render_conflict.assert_called_once_with(
            request, task, user, db, submitted_values, operation_ids=None,
            ocr_review_operation_id=None,
        )
        assert_token.assert_called_once_with(db, task, "synthetic-token")
        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "operator_action_not_applied")
        self.assertEqual(event.user_id, user.id)
        self.assertEqual(
            event.payload,
            {
                "action": "save_fields",
                "reason": "precondition_not_met",
                "request_id": request_id,
            },
        )
        self.assertNotIn("Synthetic Private Name", repr(event.payload))
        db.commit.assert_called_once_with()

    async def test_approval_and_field_save_share_actor_and_request_id(self) -> None:
        request_id = "b" * 32
        request = SimpleNamespace(
            scope={"state": {"request_id": request_id}}, query_params={},
            form=AsyncMock(return_value={"review_token": "synthetic-token"}),
        )
        task = SimpleNamespace(id=32)
        user = SimpleNamespace(id=74)
        db = MagicMock()
        db.get.return_value = task

        with (
            patch(
                "app.api.routes_tasks._values_from_form",
                new=AsyncMock(return_value={"debtor_name": "Synthetic Person"}),
            ),
            patch("app.api.routes_tasks.tasks.assert_review_token") as assert_token,
            patch("app.api.routes_tasks.tasks.save_values") as save_values,
            patch("app.api.routes_tasks.tasks.approve") as approve,
        ):
            response = await task_approve(request, task.id, user, db)

        self.assertEqual(response.status_code, 303)
        assert_token.assert_called_once_with(db, task, "synthetic-token")
        self.assertEqual(
            save_values.call_args,
            call(db, task, {"debtor_name": "Synthetic Person"}, user.id, request_id=request_id),
        )
        approve.assert_called_once_with(db, task, user.id, request_id=request_id)
        db.commit.assert_called_once_with()

    async def test_preview_decision_passes_actor_and_request_id_to_audit_service(self) -> None:
        request_id = "c" * 32
        request = SimpleNamespace(
            scope={"state": {"request_id": request_id}}, query_params={},
            form=AsyncMock(return_value={"decision": "close"}),
        )
        task = SimpleNamespace(id=33, preview_mode="live_session")
        user = SimpleNamespace(id=75)
        db = MagicMock()
        db.get.return_value = task

        with patch(
            "app.api.routes_tasks.tasks.decide_preview",
            return_value=True,
        ) as decide_preview:
            response = await task_decision(request, task.id, user, db)

        self.assertEqual(response.status_code, 303)
        decide_preview.assert_called_once_with(db, task, "close", user.id, request_id, expected_token="")
        db.commit.assert_called_once_with()

    async def test_save_preview_decision_passes_exact_screenshot_token(self) -> None:
        request = SimpleNamespace(scope={"state": {"request_id": "c" * 32}},
                                  form=AsyncMock(return_value={"decision": "save", "preview_token": "d" * 64}))
        task, user, db = SimpleNamespace(id=33, preview_mode="live_session"), SimpleNamespace(id=75), MagicMock()
        db.get.return_value = task
        with patch("app.api.routes_tasks.tasks.decide_preview", return_value=True) as decide:
            response = await task_decision(request, task.id, user, db)
        self.assertEqual(response.status_code, 303)
        decide.assert_called_once_with(db, task, "save", user.id, "c" * 32, expected_token="d" * 64)
        db.commit.assert_called_once()

    async def test_denied_save_has_get_link_instead_of_reposting_stale_decision(self) -> None:
        request = SimpleNamespace(scope={"state": {"request_id": "c" * 32}},
                                  form=AsyncMock(return_value={"decision": "save", "preview_token": "stale"}))
        task, user, db = SimpleNamespace(id=33, preview_mode="live_session"), SimpleNamespace(id=75), MagicMock()
        db.get.return_value = task
        with patch("app.api.routes_tasks.tasks.decide_preview", return_value=False), patch("app.api.routes_tasks.tasks.log_operator_action_not_applied"):
            response = await task_decision(request, task.id, user, db)
        self.assertEqual(response.status_code, 409)
        self.assertIn(b'href="/tasks/33"', response.body)
        db.commit.assert_called_once()

    async def test_stale_preview_decision_is_audited_without_changing_task(self) -> None:
        request_id = "e" * 32
        request = SimpleNamespace(
            scope={"state": {"request_id": request_id}}, query_params={},
            form=AsyncMock(return_value={"decision": "close"}),
        )
        task = SimpleNamespace(id=35, preview_mode="live_session")
        user = SimpleNamespace(id=77)
        db = MagicMock()
        db.get.return_value = task

        with patch("app.api.routes_tasks.tasks.decide_preview", return_value=False):
            response = await task_decision(request, task.id, user, db)

        self.assertEqual(response.status_code, 303)
        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "operator_action_not_applied")
        self.assertEqual(event.user_id, user.id)
        self.assertEqual(
            event.payload,
            {
                "action": "preview_decision",
                "reason": "precondition_not_met",
                "request_id": request_id,
            },
        )
        db.commit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
