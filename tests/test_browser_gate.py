from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.db.models import TaskStatus
from app.services import browser_gate


class BrowserGateTests(unittest.TestCase):
    def test_preview_wait_exits_when_another_process_finishes_the_task(self) -> None:
        task = SimpleNamespace(
            id=42,
            status=TaskStatus.writing,
            browser_decision="",
        )
        db = MagicMock()
        db.get.return_value = task

        def begin_preview(_db, current_task, _screenshots, _user_id):
            current_task.status = TaskStatus.awaiting_preview

        def complete_elsewhere(_seconds):
            task.status = TaskStatus.done

        with (
            patch.object(browser_gate, "session_scope", return_value=nullcontext(db)),
            patch.object(browser_gate.task_service, "begin_preview", side_effect=begin_preview),
            patch.object(browser_gate.task_service, "expire_preview") as expire_preview,
            patch.object(browser_gate.task_service, "preview_token", return_value="synthetic-token"),
            patch.object(browser_gate.time, "monotonic", side_effect=(0.0, 0.1)),
            patch.object(browser_gate.time, "sleep", side_effect=complete_elsewhere) as sleep,
        ):
            decision = browser_gate.DbReviewGate(task.id, timeout_s=10).ask(["synthetic-shot"])

        self.assertEqual(decision, "close")
        sleep.assert_called_once_with(browser_gate.POLL_S)
        expire_preview.assert_not_called()


    def _save_gate(self, *, enabled: bool, consumed: bool = True):
        task = SimpleNamespace(id=42, status=TaskStatus.writing, browser_decision="")
        db = MagicMock()
        db.get.return_value = task
        order = []

        from contextlib import contextmanager
        @contextmanager
        def transaction():
            yield db
            order.append("committed")

        def begin(_db, current, _screenshots, actor):
            current.status = TaskStatus.awaiting_preview
            self.assertEqual(actor, 7)

        def operator_choice(_seconds):
            task.browser_decision = "save_requested"

        def consume(_db, current, actor, token):
            self.assertEqual(actor, 7)
            self.assertEqual(token, "exact-snapshot")
            if consumed:
                current.browser_decision = "save_started"
            order.append("consumed")
            return consumed

        with (
            patch.object(browser_gate, "session_scope", side_effect=transaction),
            patch.object(browser_gate.task_service, "begin_preview", side_effect=begin),
            patch.object(browser_gate.task_service, "preview_token", return_value="exact-snapshot"),
            patch.object(browser_gate.task_service, "consume_preview_save", side_effect=consume) as consume_mock,
            patch.object(browser_gate.task_service, "expire_preview") as expire,
            patch.object(browser_gate.time, "monotonic", side_effect=(0.0, 0.1)),
            patch.object(browser_gate.time, "sleep", side_effect=operator_choice),
        ):
            gate = browser_gate.DbReviewGate(task.id, timeout_s=10, user_id=7, operator_save=enabled)
            decision = gate.ask(["synthetic-shot"])
            order.append("returned")
        return decision, consume_mock, expire, order

    def test_default_gate_never_consumes_a_save_request(self) -> None:
        self.assertFalse(browser_gate.DbReviewGate(42).operator_save)
        decision, consume, expire, _ = self._save_gate(enabled=False)
        self.assertEqual(decision, "close")
        consume.assert_not_called()
        expire.assert_called_once()

    def test_enabled_gate_commits_one_shot_marker_before_returning_save(self) -> None:
        decision, consume, expire, order = self._save_gate(enabled=True)
        self.assertEqual(decision, "save")
        consume.assert_called_once()
        expire.assert_not_called()
        self.assertEqual(order, ["committed", "consumed", "committed", "returned"])

    def test_rejected_consume_closes_without_permitting_save(self) -> None:
        decision, consume, expire, _ = self._save_gate(enabled=True, consumed=False)
        self.assertEqual(decision, "close")
        consume.assert_called_once()
        expire.assert_called_once()


if __name__ == "__main__":
    unittest.main()
