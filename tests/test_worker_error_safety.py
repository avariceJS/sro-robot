from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.workers import jobs


class WorkerErrorPrivacyTests(unittest.TestCase):
    def test_recognition_logs_and_persists_only_safe_failure_metadata(self) -> None:
        task = SimpleNamespace(
            id=101,
            profile="synthetic-profile",
            document=SimpleNamespace(stored_path="/private/documents/opaque.pdf"),
        )
        db = MagicMock()
        db.get.return_value = task
        profile = MagicMock()
        profile.parse.side_effect = ValueError("sensitive-document-content-sentinel")

        @contextmanager
        def session_scope():
            yield db

        with self.assertLogs(jobs.logger, level="ERROR") as captured:
            with (
                patch.object(jobs, "session_scope", side_effect=lambda: session_scope()),
                patch.object(jobs, "get_profile", return_value=profile),
                patch.object(jobs.task_service, "mark_recognizing"),
                patch.object(jobs.task_service, "fail") as fail,
            ):
                jobs.recognize(task.id)

        fail.assert_called_once_with(
            db,
            task,
            "Document recognition failed (ValueError)",
        )
        self.assertIn("task_id=101", captured.output[0])
        self.assertIn("error_type=ValueError", captured.output[0])
        self.assertNotIn("sensitive-document-content-sentinel", captured.output[0])
        self.assertNotIn("sensitive-document-content-sentinel", fail.call_args.args[2])
        self.assertNotIn("opaque.pdf", captured.output[0])


if __name__ == "__main__":
    unittest.main()
