"""Status labels must distinguish local validation from unknown old runs."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from app.db.models import TaskStatus
from app.services.tasks import status_labels


class TaskStatusLabelTests(unittest.TestCase):
    def test_local_validation_and_legacy_dry_run_are_distinguished_in_one_query(self) -> None:
        tasks = [
            SimpleNamespace(id=1, status=TaskStatus.done, dry_run=True),
            SimpleNamespace(id=2, status=TaskStatus.done, dry_run=True),
            SimpleNamespace(id=3, status=TaskStatus.failed, dry_run=True),
        ]
        db = MagicMock()
        db.execute.return_value.all.return_value = [(1, "local_validation_completed")]

        result = status_labels(db, tasks)

        self.assertEqual(result[1], "проверено локально")
        self.assertEqual(result[2], "старый прогон: запись не подтверждена")
        self.assertEqual(result[3], "ошибка")
        db.execute.assert_called_once()

    def test_no_event_query_for_tasks_without_dry_run_completion(self) -> None:
        task = SimpleNamespace(id=4, status=TaskStatus.needs_review, dry_run=True)
        db = MagicMock()

        self.assertEqual(status_labels(db, [task]), {4: "на проверке"})
        db.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
