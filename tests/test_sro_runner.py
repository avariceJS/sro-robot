"""Safety checks around local validation and the real SRO boundary."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.db.models import TaskStatus
from app.services import runner
from app.services.tasks import finish_local_validation


class SroRunnerTests(unittest.TestCase):
    def _approved_task(self):
        return SimpleNamespace(id=93, status=TaskStatus.approved, values={})

    def test_non_live_run_is_local_and_never_opens_sro(self) -> None:
        task = self._approved_task()
        db = MagicMock()
        scenario = MagicMock()
        scenario.validate.return_value = []

        with (
            patch.object(runner, "scenario_of", return_value=scenario),
            patch.object(runner, "card_of", return_value=object()),
            patch.object(runner.task_service, "approval_matches", return_value=True),
            patch.object(runner.task_service, "finish_local_validation") as finish,
        ):
            runner.run_task(db, task, live=False, preview=True)

        finish.assert_called_once_with(db, task, None)
        scenario.preflight.assert_not_called()
        scenario.run.assert_not_called()

    def test_live_run_is_rejected_before_any_external_work(self) -> None:
        task = self._approved_task()
        db = MagicMock()

        with (
            patch.object(runner, "scenario_of") as scenario_of,
            patch.object(runner.task_service, "approval_matches") as approval_matches,
        ):
            with self.assertRaises(runner.LiveWritesDisabled):
                runner.run_task(db, task, live=True)

        scenario_of.assert_not_called()
        approval_matches.assert_not_called()
        db.commit.assert_not_called()

    def test_missing_required_fields_fail_before_local_completion(self) -> None:
        task = self._approved_task()
        db = MagicMock()
        scenario = MagicMock()
        scenario.validate.return_value = ["Фамилия"]

        with (
            patch.object(runner, "scenario_of", return_value=scenario),
            patch.object(runner, "card_of", return_value=object()),
            patch.object(runner.task_service, "approval_matches", return_value=True),
            patch.object(runner.task_service, "fail") as fail,
            patch.object(runner.task_service, "finish_local_validation") as finish,
        ):
            runner.run_task(db, task)

        fail.assert_called_once_with(db, task, "Не заполнены обязательные поля: Фамилия")
        finish.assert_not_called()

    def test_local_completion_is_explicitly_not_a_saved_record(self) -> None:
        task = SimpleNamespace(
            id=4,
            status=TaskStatus.approved,
            dry_run=False,
            record_url="https://not-used.invalid/",
            error="old failure",
            values={},
        )
        db = MagicMock()

        with patch("app.services.tasks.approval_matches", return_value=True):
            finish_local_validation(db, task, user_id=8)

        self.assertEqual(task.status, TaskStatus.done)
        self.assertTrue(task.dry_run)
        self.assertEqual(task.record_url, "")
        self.assertEqual(task.error, "")
        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "local_validation_completed")
        self.assertEqual(event.payload, {"execution_mode": "local_validation", "saved": False})
        self.assertEqual(event.user_id, 8)


if __name__ == "__main__":
    unittest.main()
