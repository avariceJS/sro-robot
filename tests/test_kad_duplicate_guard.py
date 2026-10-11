from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.capabilities import KAD_ACQUISITION_ENABLED
from app.db.models import TaskStatus
from app.workers import jobs


class DuplicateKadDeliveryTests(unittest.TestCase):
    def test_unclaimed_delivery_does_not_repeat_external_acquisition(self) -> None:
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "opaque.pdf"
            task = SimpleNamespace(
                id=73,
                status=TaskStatus.needs_review,
                case_number="А00-1/2026",
                assignee_id=8,
                profile="person",
                document=SimpleNamespace(stored_path=str(destination)),
            )
            db = MagicMock()
            db.get.return_value = task

            @contextmanager
            def session_scope():
                yield db

            with (
                patch.object(jobs, "KAD_ACQUISITION_ENABLED", True),
                patch.object(jobs, "_require_current_worker_schema"),
                patch.object(jobs, "session_scope", side_effect=lambda: session_scope()),
                patch.object(jobs.task_service, "mark_recognizing", return_value=False) as claim,
                patch.object(jobs, "fetch_latest_ruling") as fetch_latest_ruling,
                patch.object(jobs, "get_profile") as get_profile,
            ):
                jobs.recognize_from_kad(task.id)

        claim.assert_called_once_with(db, task)
        fetch_latest_ruling.assert_not_called()
        get_profile.assert_not_called()
        self.assertEqual(task.status, TaskStatus.needs_review)
        self.assertFalse(destination.exists())

    def test_duplicate_delivery_remains_held_by_the_release_gate(self) -> None:
        self.assertFalse(KAD_ACQUISITION_ENABLED)
        with (
            patch.object(jobs, "session_scope") as database,
            patch.object(jobs, "fetch_latest_ruling") as fetch,
            self.assertLogs(jobs.logger, level="ERROR"),
            self.assertRaises(jobs.KADAcquisitionDisabled),
        ):
            jobs.recognize_from_kad(73)

        database.assert_not_called()
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
