from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from app.api.routes_tasks import task_approve, task_decision, task_save


class TaskOperatorAuditRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_field_save_passes_actor_and_request_id_to_audit_service(self) -> None:
        request_id = "a" * 32
        request = SimpleNamespace(
            scope={"state": {"request_id": request_id}},
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
            scope={"state": {"request_id": request_id}},
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
                "app.api.routes_tasks.stale_review_response",
                return_value=conflict_response,
            ) as render_conflict,
        ):
            response = await task_save(request, task.id, user, db)

        self.assertIs(response, conflict_response)
        render_conflict.assert_called_once_with(request, task, user, db, submitted_values)
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
            scope={"state": {"request_id": request_id}},
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
