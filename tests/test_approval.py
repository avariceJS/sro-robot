"""Tests that operator approval stays bound to the reviewed field values."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.models import TaskStatus
from app.services import tasks
from app.services.runner import NotApproved, run_task


class ApprovalBindingTests(unittest.TestCase):
    def test_digest_is_stable_across_mapping_order(self) -> None:
        self.assertEqual(
            tasks.values_digest({"inn": "123", "name": "Тест"}),
            tasks.values_digest({"name": "Тест", "inn": "123"}),
        )

    def test_editing_approved_values_invalidates_approval(self) -> None:
        task = SimpleNamespace(
            id=9,
            status=TaskStatus.approved,
            values={"name": "Старое значение"},
            approved_by_id=4,
        )
        db = MagicMock()

        tasks.save_values(db, task, {"name": "Новое значение"}, user_id=5)

        self.assertEqual(task.status, TaskStatus.needs_review)
        self.assertIsNone(task.approved_by_id)
        self.assertEqual(task.values, {"name": "Новое значение"})
        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "approval_invalidated")

    def test_approval_event_contains_hash_of_exact_values(self) -> None:
        task = SimpleNamespace(
            id=10,
            status=TaskStatus.needs_review,
            values={"name": "Тест"},
            approved_by_id=None,
        )
        db = MagicMock()

        tasks.approve(db, task, user_id=6)

        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "approved")
        self.assertEqual(event.payload["values_sha256"], tasks.values_digest(task.values))

    def test_run_resets_stale_approval_before_opening_sro(self) -> None:
        task = SimpleNamespace(
            id=11,
            status=TaskStatus.approved,
            values={"name": "Изменено напрямую"},
            approved_by_id=7,
        )
        db = MagicMock()
        db.scalar.return_value = None

        with self.assertRaisesRegex(NotApproved, "подтвердите их снова"):
            run_task(db, task)

        self.assertEqual(task.status, TaskStatus.needs_review)
        self.assertIsNone(task.approved_by_id)
        db.flush.assert_called_once()

    def test_cannot_edit_task_after_write_started(self) -> None:
        task = SimpleNamespace(status=TaskStatus.writing, values={"name": "Тест"})

        with self.assertRaisesRegex(ValueError, "Нельзя редактировать"):
            tasks.save_values(MagicMock(), task, {"name": "Иное"})


if __name__ == "__main__":
    unittest.main()
