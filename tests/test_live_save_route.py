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

        with patch("app.api.routes_tasks.tasks.log") as log:
            response = await task_decision(request, 12, user, db)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(task.browser_decision, "")
        log.assert_called_once()
        args = log.call_args.args
        self.assertEqual(args[2], "live_save_blocked")
        self.assertEqual(log.call_args.kwargs["payload"], {"saved": False})
        db.commit.assert_called_once()

    async def test_local_validation_request_is_audited_before_queue_admission(self) -> None:
        task = type("ApprovedTask", (), {})()
        task.id = 12
        task.status = TaskStatus.approved
        db = MagicMock()
        db.get.return_value = task
        user = type("User", (), {"id": 7})()
        call_order: list[str] = []
        db.commit.side_effect = lambda: call_order.append("commit")

        with (
            patch("app.api.routes_tasks.tasks.log") as log,
            patch(
                "app.api.routes_tasks.enqueue_write",
                side_effect=lambda *args, **kwargs: call_order.append("enqueue"),
            ) as enqueue,
        ):
            response = task_run(12, user, db)

        self.assertEqual(response.status_code, 303)
        log.assert_called_once_with(
            db,
            task,
            "local_validation_requested",
            "Оператор запросил локальную проверку задания",
            7,
            payload={"execution_mode": "local_validation", "saved": False},
        )
        db.commit.assert_called_once()
        enqueue.assert_called_once_with(12, live=False, user_id=7)
        self.assertEqual(call_order, ["commit", "enqueue"])


if __name__ == "__main__":
    unittest.main()
