from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.db.models import TaskStatus
from app.services import tasks


class TaskLifecycleTransitionTests(unittest.TestCase):
    def test_transition_graph_covers_all_states_and_keeps_terminals_closed(self) -> None:
        expected = {
            TaskStatus.new: {TaskStatus.recognizing, TaskStatus.failed, TaskStatus.rejected},
            TaskStatus.recognizing: {
                TaskStatus.recognizing,
                TaskStatus.needs_review,
                TaskStatus.failed,
                TaskStatus.duplicate,
            },
            TaskStatus.needs_review: {TaskStatus.approved, TaskStatus.rejected},
            TaskStatus.approved: {
                TaskStatus.needs_review,
                TaskStatus.approved,
                TaskStatus.rejected,
                TaskStatus.writing,
                TaskStatus.done,
                TaskStatus.failed,
            },
            TaskStatus.writing: {TaskStatus.done, TaskStatus.awaiting_preview},
            TaskStatus.awaiting_preview: set(),
            TaskStatus.done: set(),
            TaskStatus.failed: set(),
            TaskStatus.duplicate: set(),
            TaskStatus.rejected: set(),
        }
        self.assertEqual(set(tasks.TASK_STATUS_TRANSITIONS), set(TaskStatus))
        self.assertEqual(
            tasks.TASK_STATUS_TRANSITIONS,
            {source: frozenset(targets) for source, targets in expected.items()},
        )
        for terminal in (
            TaskStatus.done,
            TaskStatus.failed,
            TaskStatus.duplicate,
            TaskStatus.rejected,
        ):
            with self.subTest(terminal=terminal):
                self.assertEqual(tasks.TASK_STATUS_TRANSITIONS[terminal], frozenset())

    def test_transition_helper_applies_declared_edges_and_rejects_undeclared_edges(self) -> None:
        task = SimpleNamespace(status=TaskStatus.approved)
        tasks.transition_task_status(task, TaskStatus.needs_review)
        self.assertEqual(task.status, TaskStatus.needs_review)

        task.status = TaskStatus.done
        with self.assertRaisesRegex(tasks.InvalidTaskTransition, "done -> failed"):
            tasks.transition_task_status(task, TaskStatus.failed)
        self.assertEqual(task.status, TaskStatus.done)

    def test_rejected_operator_action_is_attributed_without_submitted_values(self) -> None:
        task = SimpleNamespace(id=18, status=TaskStatus.writing)
        db = MagicMock()

        tasks.log_operator_action_not_applied(
            db,
            task,
            action="save_fields",
            user_id=12,
            request_id="e" * 32,
        )

        event = db.add.call_args.args[0]
        self.assertEqual(event.task_id, task.id)
        self.assertEqual(event.user_id, 12)
        self.assertEqual(event.kind, "operator_action_not_applied")
        self.assertEqual(
            event.payload,
            {
                "action": "save_fields",
                "reason": "precondition_not_met",
                "request_id": "e" * 32,
            },
        )
        self.assertNotIn("Synthetic Private Name", repr(event.payload))
        db.flush.assert_called_once_with()

    def test_rejected_operator_action_omits_invalid_request_id_and_rejects_unknown_action(self) -> None:
        task = SimpleNamespace(id=18, status=TaskStatus.writing)
        db = MagicMock()

        tasks.log_operator_action_not_applied(
            db,
            task,
            action="reject",
            user_id=12,
            request_id="caller-controlled-value",
        )
        event = db.add.call_args.args[0]
        self.assertEqual(
            event.payload,
            {"action": "reject", "reason": "precondition_not_met"},
        )

        with self.assertRaisesRegex(ValueError, "Unsupported operator audit action"):
            tasks.log_operator_action_not_applied(
                db,
                task,
                action="arbitrary",
                user_id=12,
            )
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

    def test_local_validation_admission_requires_current_approval(self) -> None:
        task = SimpleNamespace(id=17, status=TaskStatus.approved, values={})
        db = MagicMock()
        with patch.object(tasks, "approval_matches", return_value=True), patch(
            "app.services.outbox.create_local_validation_intent", return_value=41
        ):
            self.assertTrue(tasks.request_local_validation(db, task, 7, "d" * 32))
        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "local_validation_requested")
        self.assertEqual(
            event.payload,
            {"execution_mode": "local_validation", "saved": False, "request_id": "d" * 32},
        )

        task.status = TaskStatus.rejected
        db.reset_mock()
        with patch.object(tasks, "approval_matches") as matches:
            self.assertFalse(tasks.request_local_validation(db, task, 7, "d" * 32))
        matches.assert_not_called()
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

