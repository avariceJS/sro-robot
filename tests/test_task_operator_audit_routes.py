from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from fastapi import HTTPException

from app.api.routes_tasks import task_approve, task_decision, task_demo_card, task_save
from app.db.models import TaskStatus


class TaskOperatorAuditRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_card_is_read_only_and_uses_approved_values(self) -> None:
        task = SimpleNamespace(
            id=81,
            status=TaskStatus.approved,
            scenario="person_card",
            recognized={
                "last_name": {"value": "Тестов", "confidence": 0.99},
                "first_name": {"value": "Иван", "confidence": 0.99},
                "middle_name": {"value": "Синтетич", "confidence": 0.99},
                "case_number": {"value": "А99-12345/2099", "confidence": 0.99},
                "inn": {"value": "", "confidence": 0.0},
                "address": {"value": "", "confidence": 0.0},
                "address_kind": {"value": "", "confidence": 0.0},
            },
            values={},
        )
        request = SimpleNamespace()
        user = SimpleNamespace(id=9)
        db = MagicMock()
        db.get.return_value = task
        expected_response = object()

        with patch(
            "app.api.routes_tasks.templates.TemplateResponse",
            return_value=expected_response,
        ) as render:
            response = task_demo_card(request, task.id, user, db)

        self.assertIs(response, expected_response)
        context = render.call_args.args[2]
        values = dict(context["values"])
        self.assertEqual(values["Фамилия"], "Тестов")
        self.assertEqual(values["Имя"], "Иван")
        self.assertEqual(
            values["ИНН (или номер дела, если ИНН отсутствует)"],
            "А99-12345/2099",
        )
        self.assertEqual(values["Комментарий"], "А99-12345/2099")
        render.assert_called_once_with(request, "demo_card.html", context)
        db.commit.assert_not_called()
        db.add.assert_not_called()

    async def test_demo_card_rejects_unapproved_task_without_writes(self) -> None:
        task = SimpleNamespace(id=82, status=TaskStatus.needs_review)
        db = MagicMock()
        db.get.return_value = task

        with self.assertRaises(HTTPException) as raised:
            task_demo_card(SimpleNamespace(), task.id, SimpleNamespace(id=9), db)

        self.assertEqual(raised.exception.status_code, 409)
        db.commit.assert_not_called()

    async def test_field_save_passes_actor_and_request_id_to_audit_service(self) -> None:
        request_id = "a" * 32
        request = SimpleNamespace(scope={"state": {"request_id": request_id}})
        task = SimpleNamespace(id=31)
        user = SimpleNamespace(id=73)
        db = MagicMock()
        db.get.return_value = task

        with (
            patch(
                "app.api.routes_tasks._values_from_form",
                new=AsyncMock(return_value={"debtor_name": "Synthetic Person"}),
            ),
            patch("app.api.routes_tasks.tasks.save_values") as save_values,
        ):
            response = await task_save(request, task.id, user, db)

        self.assertEqual(response.status_code, 303)
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
        request = SimpleNamespace(scope={"state": {"request_id": request_id}})
        task = SimpleNamespace(id=34)
        user = SimpleNamespace(id=76)
        db = MagicMock()
        db.get.return_value = task
        submitted_values = {"debtor_name": "Synthetic Private Name"}

        with (
            patch(
                "app.api.routes_tasks._values_from_form",
                new=AsyncMock(return_value=submitted_values),
            ),
            patch(
                "app.api.routes_tasks.tasks.save_values",
                side_effect=ValueError("synthetic state conflict"),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await task_save(request, task.id, user, db)

        self.assertEqual(raised.exception.status_code, 409)
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
        request = SimpleNamespace(scope={"state": {"request_id": request_id}})
        task = SimpleNamespace(id=32)
        user = SimpleNamespace(id=74)
        db = MagicMock()
        db.get.return_value = task

        with (
            patch(
                "app.api.routes_tasks._values_from_form",
                new=AsyncMock(return_value={"debtor_name": "Synthetic Person"}),
            ),
            patch("app.api.routes_tasks.tasks.save_values") as save_values,
            patch("app.api.routes_tasks.tasks.approve") as approve,
        ):
            response = await task_approve(request, task.id, user, db)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            save_values.call_args,
            call(db, task, {"debtor_name": "Synthetic Person"}, user.id, request_id=request_id),
        )
        approve.assert_called_once_with(db, task, user.id, request_id=request_id)
        db.commit.assert_called_once_with()

    async def test_preview_decision_passes_actor_and_request_id_to_audit_service(self) -> None:
        request_id = "c" * 32
        request = SimpleNamespace(
            scope={"state": {"request_id": request_id}},
            form=AsyncMock(return_value={"decision": "close"}),
        )
        task = SimpleNamespace(id=33)
        user = SimpleNamespace(id=75)
        db = MagicMock()
        db.get.return_value = task

        with patch(
            "app.api.routes_tasks.tasks.decide_preview",
            return_value=True,
        ) as decide_preview:
            response = await task_decision(request, task.id, user, db)

        self.assertEqual(response.status_code, 303)
        decide_preview.assert_called_once_with(db, task, "close", user.id, request_id)
        db.commit.assert_called_once_with()

    async def test_stale_preview_decision_is_audited_without_changing_task(self) -> None:
        request_id = "e" * 32
        request = SimpleNamespace(
            scope={"state": {"request_id": request_id}},
            form=AsyncMock(return_value={"decision": "close"}),
        )
        task = SimpleNamespace(id=35)
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
