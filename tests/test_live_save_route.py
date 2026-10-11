"""Legacy preview decisions cannot turn into real SRO writes."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from starlette.requests import Request

from app.api.routes_tasks import task_decision, task_run
from app.db.models import TaskStatus


class LiveSaveRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_decision_is_logged_but_not_sent_to_waiting_writer(self) -> None:
        body = b"decision=save"

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/tasks/12/decision",
                "raw_path": b"/tasks/12/decision",
                "query_string": b"",
                "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 8000),
            },
            receive,
        )
        task = type("PendingPreview", (), {})()
        task.id = 12
        task.status = TaskStatus.awaiting_preview
        task.browser_decision = ""
        db = MagicMock()
        db.get.return_value = task
        user = type("User", (), {"id": 7})()
        request_id = "c" * 32
        request.scope["state"] = {"request_id": request_id}

        with patch("app.api.routes_tasks.tasks.log") as log:
            response = await task_decision(request, 12, user, db)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(task.browser_decision, "save")
        log.assert_called_once()
        args = log.call_args.args
        self.assertEqual(args[2], "live_save_blocked")
        self.assertEqual(args[4], user.id)
        self.assertEqual(
            log.call_args.kwargs["payload"],
            {"saved": False, "request_id": request_id},
        )
        db.commit.assert_called_once()

    async def test_local_validation_request_is_audited_before_queue_admission(self) -> None:
        request_id = "a" * 32
        request = Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/tasks/12/run",
                "raw_path": b"/tasks/12/run",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("robot.example", 443),
                "state": {"request_id": request_id},
            }
        )
        task = type("ApprovedTask", (), {})()
        task.id = 12
        task.status = TaskStatus.approved
        db = MagicMock()
        db.get.return_value = task
        user = type("User", (), {"id": 7})()
        call_order: list[str] = []
        db.commit.side_effect = lambda: call_order.append("commit")

        with (
            patch(
                "app.api.routes_tasks.tasks.request_local_validation",
                side_effect=lambda *_args, **_kwargs: call_order.append("admit") or 41,
            ) as admit,
            patch(
                "app.api.routes_tasks.outbox.dispatch_required",
                side_effect=lambda *args, **kwargs: call_order.append("enqueue"),
            ) as enqueue,
        ):
            response = task_run(request, 12, user, db)

        self.assertEqual(response.status_code, 303)
        admit.assert_called_once_with(db, task, 7, request_id)
        db.commit.assert_called_once()
        enqueue.assert_called_once_with(db, 41)
        self.assertEqual(call_order, ["admit", "commit", "enqueue"])

    def test_stale_local_validation_request_is_not_enqueued(self) -> None:
        request = Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/tasks/12/run",
                "raw_path": b"/tasks/12/run",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("robot.example", 443),
                "state": {"request_id": "c" * 32},
            }
        )
        task = type("RejectedTask", (), {"id": 12, "status": TaskStatus.rejected})()
        db = MagicMock()
        db.get.return_value = task
        user = type("User", (), {"id": 7})()

        with (
            patch(
                "app.api.routes_tasks.tasks.request_local_validation",
                return_value=None,
            ) as admit,
            patch("app.api.routes_tasks.outbox.dispatch_required") as enqueue,
        ):
            response = task_run(request, 12, user, db)

        self.assertEqual(response.status_code, 303)
        admit.assert_called_once_with(db, task, 7, "c" * 32)
        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "operator_action_not_applied")
        self.assertEqual(event.user_id, user.id)
        self.assertEqual(
            event.payload,
            {
                "action": "local_validation",
                "reason": "precondition_not_met",
                "request_id": "c" * 32,
            },
        )
        db.commit.assert_called_once_with()
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
