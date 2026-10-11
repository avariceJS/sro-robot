from __future__ import annotations

import unittest
import traceback
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

    def test_sro_worker_logs_safe_failure_and_raises_generic_reconciliation_error(self) -> None:
        task = SimpleNamespace(id=202, status="approved")
        db = MagicMock()
        db.get.return_value = task
        sensitive_error = "postgresql://operator:synthetic-secret@db/private document text"

        @contextmanager
        def session_scope():
            yield db

        with (
            patch.object(jobs, "session_scope", side_effect=lambda: session_scope()),
            patch.object(jobs.runner, "run_task", side_effect=RuntimeError(sensitive_error)),
            patch.object(jobs.task_service, "log") as audit_log,
            self.assertLogs(jobs.logger, level="ERROR") as captured,
        ):
            with self.assertRaises(jobs.SroJobExecutionError) as raised:
                jobs.write_to_sro(task.id)

        self.assertEqual(
            str(raised.exception),
            "SRO job failed; outcome requires read-only reconciliation before retry",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertNotIn(sensitive_error, "".join(traceback.format_exception(raised.exception)))
        self.assertIn("task_id=202 error_type=RuntimeError", captured.output[0])
        self.assertNotIn(sensitive_error, captured.output[0])
        self.assertEqual(task.status, "approved")
        audit_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
