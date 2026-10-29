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

    def test_sro_preview_states_are_labeled_as_unsaved_and_do_not_query_events(self) -> None:
        tasks = [
            SimpleNamespace(id=5, status=TaskStatus.writing, dry_run=True),
            SimpleNamespace(id=6, status=TaskStatus.awaiting_preview, browser_decision=""),
            SimpleNamespace(id=7, status=TaskStatus.awaiting_preview, browser_decision="close"),
        ]
        db = MagicMock()

        self.assertEqual(
            status_labels(db, tasks),
            {
                5: "подготовка формы СРО без записи",
                6: "форма открыта для проверки",
                7: "закрытие формы без записи",
            },
        )
        db.execute.assert_not_called()


    def test_save_choice_is_not_labeled_saved_until_verified_receipt(self) -> None:
        states = {
            "save_requested": "сохранение запрошено",
            "save_started": "карточка сохраняется",
            "saved": "карточка СРО сохранена и проверена",
            "save_unknown": "результат сохранения неизвестен; требуется сверка",
            "closed": "форма закрыта без записи",
            "preview_failed": "ошибка формы СРО; требуется проверка",
            "expired": "время ожидания решения истекло",
        }
        db = MagicMock()
        for decision, expected in states.items():
            with self.subTest(decision=decision):
                task = SimpleNamespace(id=8, status=TaskStatus.awaiting_preview,
                    browser_decision=decision)
                self.assertEqual(status_labels(db, [task]), {8: expected})
        failed_preparation = SimpleNamespace(id=9, status=TaskStatus.writing,
            dry_run=True, browser_decision="preview_failed")
        self.assertEqual(status_labels(db, [failed_preparation]),
            {9: "ошибка формы СРО; требуется проверка"})
        db.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
