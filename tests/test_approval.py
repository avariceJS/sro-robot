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

        tasks.save_values(
            db,
            task,
            {"name": "Новое значение"},
            user_id=5,
            request_id="f" * 32,
        )

        self.assertEqual(task.status, TaskStatus.needs_review)
        self.assertIsNone(task.approved_by_id)
        self.assertEqual(task.values, {"name": "Новое значение"})
        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "approval_invalidated")

        saved_event = db.add.call_args_list[0].args[0]
        self.assertEqual(saved_event.kind, "fields_saved")
        self.assertEqual(saved_event.user_id, 5)
        self.assertEqual(saved_event.payload["request_id"], "f" * 32)
        self.assertEqual(
            saved_event.payload,
            {
                "changed": True,
                "field_count": 1,
                "values_sha256": tasks.values_digest({"name": "Новое значение"}),
                "request_id": "f" * 32,
            },
        )
        self.assertNotIn("Новое значение", repr(saved_event.payload))
        invalidated_event = db.add.call_args_list[1].args[0]
        self.assertEqual(invalidated_event.kind, "approval_invalidated")
        self.assertEqual(invalidated_event.user_id, 5)
        self.assertEqual(invalidated_event.payload, {"request_id": "f" * 32})

    def test_unchanged_field_submission_is_still_attributed_without_values(self) -> None:
        task = SimpleNamespace(
            id=12,
            status=TaskStatus.needs_review,
            values={"name": "Синтетическое значение"},
            approved_by_id=None,
        )
        db = MagicMock()

        tasks.save_values(
            db,
            task,
            {"name": " Синтетическое значение "},
            user_id=8,
        )

        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "fields_saved")
        self.assertEqual(event.user_id, 8)
        self.assertEqual(event.payload["changed"], False)
        self.assertEqual(event.payload["field_count"], 1)
        self.assertEqual(
            event.payload["values_sha256"],
            tasks.values_digest({"name": "Синтетическое значение"}),
        )
        self.assertNotIn("Синтетическое значение", repr(event.payload))

    def test_approval_event_contains_hash_of_exact_values(self) -> None:
        task = SimpleNamespace(
            id=10,
            status=TaskStatus.needs_review,
            values={"name": "Тест"},
            approved_by_id=None,
        )
        db = MagicMock()

        tasks.approve(db, task, user_id=6, request_id="9" * 32)

        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "approved")
        self.assertEqual(event.payload["values_sha256"], tasks.values_digest(task.values))
        self.assertEqual(event.user_id, 6)
        self.assertEqual(event.payload["request_id"], "9" * 32)

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
