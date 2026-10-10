"""Tests for worker failure handling without a database or external services."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.workers import jobs


class RecognizeJobTests(unittest.TestCase):
    def test_parse_error_is_saved_as_terminal_task_failure(self) -> None:
        task = SimpleNamespace(
            id=41,
            profile="court_ruling",
            document=SimpleNamespace(stored_path="/private/documents/opaque.pdf"),
        )
        db = MagicMock()
        db.get.return_value = task
        profile = MagicMock()
        profile.parse.side_effect = ValueError("invalid PDF")

        @contextmanager
        def session_scope():
            yield db

        with (
            patch.object(jobs, "session_scope", side_effect=lambda: session_scope()),
            patch.object(jobs, "get_profile", return_value=profile),
            patch.object(jobs.task_service, "claim_unleased_ocr", return_value=True) as claim,
            patch.object(jobs.task_service, "fail") as fail,
            patch.object(jobs.task_service, "save_recognized") as save_recognized,
        ):
            jobs.recognize(task.id)

        claim.assert_called_once_with(db, task)
        fail.assert_called_once_with(db, task, "Document recognition failed (ValueError)")
        save_recognized.assert_not_called()


if __name__ == "__main__":
    unittest.main()
