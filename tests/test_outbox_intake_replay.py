from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.db.models import TaskStatus
from app.services import outbox


class IntakeReplayIntentTests(unittest.TestCase):
    def _dedupe_key(self, operation_id: int, task_id: int) -> str:
        material = f"sro-robot/intake-dispatch/v1:{operation_id}:{task_id}:ocr"
        return hashlib.sha256(material.encode("ascii")).hexdigest()

    def test_pending_outbox_row_is_retried_without_queue_scan(self) -> None:
        operation_id, task_id, intent_id = 55, 101, 991
        db = MagicMock()
        db.execute.return_value = [
            SimpleNamespace(id=intent_id, dedupe_key=self._dedupe_key(operation_id, task_id))
        ]
        db.scalars.return_value = iter([SimpleNamespace(id=task_id, status=TaskStatus.new)])

        with patch.object(outbox, "active_ocr_task_ids") as active:
            self.assertEqual(
                outbox.pending_intake_intents(db, operation_id=operation_id, task_ids=[task_id]),
                [intent_id],
            )

        active.assert_not_called()

    def test_active_rq_job_prevents_duplicate_outbox_admission(self) -> None:
        db = MagicMock()
        db.execute.return_value = []
        db.scalars.return_value = iter([SimpleNamespace(id=101, status=TaskStatus.new)])

        with (
            patch.object(outbox, "active_ocr_task_ids", return_value={101}) as active,
            patch.object(outbox, "create_intake_intent") as create,
        ):
            self.assertEqual(
                outbox.pending_intake_intents(db, operation_id=55, task_ids=[101]),
                [],
            )

        active.assert_called_once_with({101})
        create.assert_not_called()

    def test_missing_rq_job_recreates_intent_for_unclaimed_task(self) -> None:
        db = MagicMock()
        db.execute.return_value = []
        db.scalars.return_value = iter([SimpleNamespace(id=101, status=TaskStatus.new)])

        with (
            patch.object(outbox, "active_ocr_task_ids", return_value=set()) as active,
            patch.object(outbox, "create_intake_intent", return_value=992) as create,
        ):
            self.assertEqual(
                outbox.pending_intake_intents(db, operation_id=55, task_ids=[101]),
                [992],
            )

        active.assert_called_once_with({101})
        create.assert_called_once_with(db, task_id=101, operation_id=55, job_kind="ocr")

    def test_terminal_task_is_never_requeued(self) -> None:
        db = MagicMock()
        db.execute.return_value = []
        db.scalars.return_value = iter([SimpleNamespace(id=101, status=TaskStatus.needs_review)])

        with (
            patch.object(outbox, "active_ocr_task_ids") as active,
            patch.object(outbox, "create_intake_intent") as create,
        ):
            self.assertEqual(
                outbox.pending_intake_intents(db, operation_id=55, task_ids=[101]),
                [],
            )

        active.assert_not_called()
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
