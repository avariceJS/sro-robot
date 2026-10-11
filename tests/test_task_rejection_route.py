from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from fastapi import HTTPException

from app.api.routes_tasks import task_reject
from app.db.models import TaskStatus


class TaskRejectionRouteTests(unittest.TestCase):
    def test_rejection_is_attributed_and_committed(self) -> None:
        task = SimpleNamespace(id=27, status=TaskStatus.new, approved_by_id=None)
        db = MagicMock()
        db.get.return_value = task
        user = SimpleNamespace(id=8)
        request = SimpleNamespace(scope={"state": {"request_id": "b" * 32}})

        response = task_reject(request, task.id, user, db)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/tasks/27")
        self.assertEqual(task.status, TaskStatus.rejected)
        event = db.add.call_args.args[0]
        self.assertEqual(event.user_id, user.id)
        self.assertEqual(event.kind, "rejected")
        self.assertEqual(event.payload, {"request_id": "b" * 32})
        db.commit.assert_called_once_with()

    def test_stale_rejection_returns_conflict_without_commit(self) -> None:
        task = SimpleNamespace(id=27, status=TaskStatus.recognizing, approved_by_id=None)
        db = MagicMock()
        db.get.return_value = task
        user = SimpleNamespace(id=8)
        request = SimpleNamespace(scope={"state": {"request_id": "b" * 32}})

        with self.assertRaises(HTTPException) as raised:
            task_reject(request, task.id, user, db)

        self.assertEqual(raised.exception.status_code, 409)
        db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
