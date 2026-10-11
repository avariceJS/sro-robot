from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.db.models import TaskStatus
from app.workers import jobs


class DuplicateWorkerDeliveryTests(unittest.TestCase):
    def test_unclaimed_delivery_does_not_parse_or_change_approved_task(self) -> None:
        task = SimpleNamespace(
            id=42,
            profile="court_ruling",
            status=TaskStatus.approved,
            document=SimpleNamespace(stored_path="/private/documents/opaque.pdf"),
        )
        db = MagicMock()
        db.get.return_value = task

        @contextmanager
        def session_scope():
            yield db

        with (
            patch.object(jobs.settings, "ocr_task_leases_enabled", False),
            patch.object(jobs, "_require_current_worker_schema"),
            patch.object(jobs, "session_scope", side_effect=lambda: session_scope()),
            patch.object(jobs.task_service, "claim_unleased_task", return_value=False) as claim,
            patch.object(jobs, "get_profile") as get_profile,
            patch.object(jobs.task_service, "save_recognized") as save_recognized,
        ):
            jobs.recognize(task.id)

        claim.assert_called_once_with(db, task)
        get_profile.assert_not_called()
        save_recognized.assert_not_called()
        self.assertEqual(task.status, TaskStatus.approved)


if __name__ == "__main__":
    unittest.main()
