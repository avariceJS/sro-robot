"""The operator route admits only a current approval to the safe SRO preview queue."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.api.routes_tasks import task_prepare_sro_form
from app.db.models import TaskStatus


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/tasks/19/prepare-sro-form",
            "raw_path": b"/tasks/19/prepare-sro-form",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "state": {"request_id": "b" * 32},
        }
    )


class PrepareSroFormRouteTests(unittest.TestCase):
    def test_current_approval_is_marked_before_queue_dispatch(self) -> None:
        task = type("ApprovedTask", (), {"id": 19, "status": TaskStatus.approved})()
        db = MagicMock()
        db.get.return_value = task
        user = type("User", (), {"id": 6})()
        order: list[str] = []

        with (
            patch("app.api.routes_tasks.tasks.approval_matches", return_value=True),
            patch(
                "app.api.routes_tasks.tasks.mark_writing",
                side_effect=lambda *_a, **_kw: order.append("mark"),
            ) as mark,
            patch(
                "app.api.routes_tasks.enqueue_sro_form_preview",
                side_effect=lambda *_a: order.append("enqueue"),
            ) as enqueue,
        ):
            db.commit.side_effect = lambda: order.append("commit")
            response = task_prepare_sro_form(_request(), task.id, user, db)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(order, ["mark", "commit", "enqueue"])
        mark.assert_called_once_with(db, task, dry_run=True)
        enqueue.assert_called_once_with(task.id, user.id)

    def test_stale_or_unapproved_task_is_not_dispatched(self) -> None:
        task = type("Task", (), {"id": 19, "status": TaskStatus.needs_review})()
        db = MagicMock()
        db.get.return_value = task
        user = type("User", (), {"id": 6})()

        with (
            patch("app.api.routes_tasks.tasks.approval_matches", return_value=False),
            patch("app.api.routes_tasks.tasks.mark_writing") as mark,
            patch("app.api.routes_tasks.enqueue_sro_form_preview") as enqueue,
        ):
            with self.assertRaises(HTTPException) as caught:
                task_prepare_sro_form(_request(), task.id, user, db)

        self.assertEqual(caught.exception.status_code, 409)
        mark.assert_not_called()
        enqueue.assert_not_called()
        db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
