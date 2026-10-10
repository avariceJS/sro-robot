"""Tests for failure reporting around the external SRO boundary."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.db.models import TaskStatus
from app.services import runner


class SroRunnerTests(unittest.TestCase):
    def test_scenario_exception_is_logged_and_saved_as_failure(self) -> None:
        task = SimpleNamespace(id=93, status=TaskStatus.approved)
        db = MagicMock()
        scenario = MagicMock()
        scenario.validate.return_value = []
        scenario.preflight.return_value = SimpleNamespace(duplicates=[])
        scenario.run.side_effect = RuntimeError("synthetic platform failure")
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False

        with self.assertLogs(runner.logger, level="ERROR") as captured:
            with (
                patch.object(runner, "scenario_of", return_value=scenario),
                patch.object(runner, "card_of", return_value=object()),
                patch.object(runner.Config, "load", return_value=object()),
                patch.object(runner, "SroClient", return_value=client),
                patch.object(runner.task_service, "mark_writing"),
                patch.object(runner.task_service, "fail") as fail,
                patch.object(runner.task_service, "finish") as finish,
            ):
                runner.run_task(db, task)

        fail.assert_called_once_with(db, task, "RuntimeError: synthetic platform failure")
        finish.assert_not_called()
        self.assertIn("task_id=93", captured.output[0])
        self.assertIn("Traceback", captured.output[0])


if __name__ == "__main__":
    unittest.main()
