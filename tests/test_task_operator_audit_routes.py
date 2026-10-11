from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from app.api.routes_tasks import task_approve, task_decision, task_save


class TaskOperatorAuditRouteTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
