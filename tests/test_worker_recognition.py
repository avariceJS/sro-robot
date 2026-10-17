"""Tests for worker failure handling without a database or external services."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.workers import jobs
from sro_robot.core.ocr import PdfPageLimitExceeded


class RecognizeJobTests(unittest.TestCase):
    def test_parse_error_is_saved_as_terminal_task_failure(self) -> None:
        task = SimpleNamespace(
            id=41,
            profile="court_ruling",
            recognized={},
            document=SimpleNamespace(
                stored_path="/private/documents/opaque.pdf",
                content_hash="a" * 64,
            ),
        )
        db = MagicMock()
        db.get.return_value = task
        profile = MagicMock()
        profile.parse.side_effect = ValueError("invalid PDF")

        @contextmanager
        def session_scope():
            yield db

        with self.assertLogs(jobs.logger, level="ERROR") as captured:
            with (
                patch.object(jobs, "session_scope", side_effect=lambda: session_scope()),
                patch.object(jobs, "_require_current_worker_schema"),
                patch.object(jobs, "get_profile", return_value=profile),
                patch.object(jobs.task_service, "validate_stored_document_file"),
                patch.object(jobs.task_service, "claim_unleased_task", return_value=True) as claim,
                patch.object(jobs.task_service, "fail") as fail,
                patch.object(jobs.task_service, "save_recognized") as save_recognized,
            ):
                jobs.recognize(task.id)

        claim.assert_called_once_with(db, task)
        fail.assert_called_once_with(db, task, "Document recognition failed (ValueError)")
        save_recognized.assert_not_called()
        self.assertIn("task_id=41", captured.output[0])
        self.assertIn("error_type=ValueError", captured.output[0])
        self.assertNotIn("invalid PDF", captured.output[0])

    def test_page_limit_failure_shows_safe_employee_guidance(self) -> None:
        task = SimpleNamespace(
            id=42,
            profile="court_ruling",
            recognized={},
            document=SimpleNamespace(
                stored_path="/private/documents/opaque.pdf",
                content_hash="b" * 64,
            ),
        )
        db = MagicMock()
        db.get.return_value = task
        profile = MagicMock()
        profile.parse.side_effect = PdfPageLimitExceeded(81, 80)

        @contextmanager
        def session_scope():
            yield db

        with self.assertLogs(jobs.logger, level="ERROR") as captured:
            with (
                patch.object(jobs, "session_scope", side_effect=lambda: session_scope()),
                patch.object(jobs, "_require_current_worker_schema"),
                patch.object(jobs, "get_profile", return_value=profile),
                patch.object(jobs.task_service, "validate_stored_document_file"),
                patch.object(jobs.task_service, "claim_unleased_task", return_value=True),
                patch.object(jobs.task_service, "fail") as fail,
            ):
                jobs.recognize(task.id)

        fail.assert_called_once()
        message = fail.call_args.args[2]
        self.assertIn("81 стр.", message)
        self.assertIn("OCR_MAX_PDF_PAGES", message)
        self.assertNotIn("opaque.pdf", message)
        self.assertIn("error_type=PdfPageLimitExceeded", captured.output[0])

if __name__ == "__main__":
    unittest.main()
