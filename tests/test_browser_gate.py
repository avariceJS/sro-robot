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
            patch.object(browser_gate.time, "monotonic", side_effect=(0.0, 0.1)),
            patch.object(browser_gate.time, "sleep", side_effect=complete_elsewhere) as sleep,
        ):
            decision = browser_gate.DbReviewGate(task.id, timeout_s=10).ask(["synthetic-shot"])

        self.assertEqual(decision, "close")
        sleep.assert_called_once_with(browser_gate.POLL_S)
        expire_preview.assert_not_called()


if __name__ == "__main__":
    unittest.main()
