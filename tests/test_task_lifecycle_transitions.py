from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from app.db.models import TaskStatus
from app.services import tasks


class TaskLifecycleTransitionTests(unittest.TestCase):
    def test_rejection_accepts_only_queued_or_operator_review_states(self) -> None:
        for status in (TaskStatus.new, TaskStatus.needs_review, TaskStatus.approved):
            with self.subTest(status=status):
                task = SimpleNamespace(
                    id=17,
                    status=status,
                    approved_by_id=9 if status == TaskStatus.approved else None,
                )
                db = MagicMock()

                tasks.reject(db, task, user_id=7, request_id="a" * 32)

                self.assertEqual(task.status, TaskStatus.rejected)
                self.assertIsNone(task.approved_by_id)
                event = db.add.call_args.args[0]
                self.assertEqual(event.kind, "rejected")
                self.assertEqual(event.user_id, 7)
                self.assertEqual(event.payload, {"request_id": "a" * 32})

    def test_rejection_refuses_active_recognition(self) -> None:
        task = SimpleNamespace(id=17, status=TaskStatus.recognizing, approved_by_id=None)
        db = MagicMock()

        with self.assertRaises(tasks.InvalidTaskTransition):
            tasks.reject(db, task, user_id=7)

        self.assertEqual(task.status, TaskStatus.recognizing)
        db.add.assert_not_called()

    def test_expired_preview_is_recorded_only_while_still_waiting(self) -> None:
        task = SimpleNamespace(
            id=17,
            status=TaskStatus.awaiting_preview,
            browser_decision="",
            error="",
        )
        db = MagicMock()

        self.assertTrue(tasks.expire_preview(db, task))
        self.assertEqual(task.error, "Оператор не ответил, черновик закрыт")
        self.assertEqual(db.add.call_args.args[0].kind, "preview_timeout")

        task.status = TaskStatus.done
        db.reset_mock()
        self.assertFalse(tasks.expire_preview(db, task))
        db.add.assert_not_called()

    def test_terminal_tasks_reject_unguarded_service_transitions(self) -> None:
        attempts = (
            lambda db, task: tasks.reject(db, task, user_id=7),
            lambda db, task: tasks.mark_writing(db, task, dry_run=False),
            lambda db, task: tasks.finish(db, task, record_url="/synthetic"),
            lambda db, task: tasks.mark_duplicate(db, task, ["synthetic.pdf"]),
            lambda db, task: tasks.finish_local_validation(db, task, user_id=7),
        )
        for transition in attempts:
            with self.subTest(transition=transition):
                task = SimpleNamespace(
                    id=19,
                    status=TaskStatus.done,
                    dry_run=True,
                    record_url="",
                    error="",
                    values={},
                )
                db = MagicMock()

                with self.assertRaises(tasks.InvalidTaskTransition):
                    transition(db, task)

                self.assertEqual(task.status, TaskStatus.done)
                db.add.assert_not_called()

