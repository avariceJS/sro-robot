from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.workers import jobs


class WorkerLeaseConfigurationTests(unittest.TestCase):
    def test_lease_claim_failure_does_not_fail_or_start_unleased_task(self) -> None:
        task = SimpleNamespace(id=202, profile="synthetic", document=SimpleNamespace(stored_path="/tmp/file.pdf"))
        db = MagicMock()
        db.get.return_value = task

        @contextmanager
        def session_scope():
            yield db

        with self.assertLogs(jobs.logger, level="ERROR") as captured:
            with (
                patch.object(jobs.settings, "ocr_task_leases_enabled", True),
                patch.object(jobs, "session_scope", side_effect=lambda: session_scope()),
                patch.object(jobs.leases, "claim_ocr", side_effect=RuntimeError("schema unavailable")),
                patch.object(jobs, "get_profile") as get_profile,
                patch.object(jobs.task_service, "fail") as fail,
            ):
                jobs.recognize(task.id)

        get_profile.assert_not_called()
        fail.assert_not_called()
        self.assertIn("task_id=202", captured.output[0])
        self.assertNotIn("schema unavailable", captured.output[0])


if __name__ == "__main__":
    unittest.main()
