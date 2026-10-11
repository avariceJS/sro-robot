"""Offline tests for failures after an MKAD document has been acquired."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.capabilities import KAD_ACQUISITION_ENABLED
from app.db.models import TaskStatus
from app.workers import jobs


class KadWorkerTests(unittest.TestCase):
    def test_pdf_parse_failure_is_logged_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = SimpleNamespace(
                id=73,
                case_number="А99-12345/2099",
                assignee_id=8,
                profile="person",
                status=TaskStatus.recognizing,
                document=SimpleNamespace(stored_path=str(Path(temporary) / "source.pdf")),
            )
            db = MagicMock()
            db.get.return_value = task

            @contextmanager
            def session_scope():
                yield db

            profile = MagicMock()
            profile.parse.side_effect = ValueError("invalid PDF text layer")
            previous_umask = os.umask(0)
            try:
                with self.assertLogs(jobs.logger, level="ERROR") as captured:
                    with (
                        patch.object(jobs, "KAD_ACQUISITION_ENABLED", True),
                        patch.object(jobs, "session_scope", side_effect=lambda: session_scope()),
                        patch.object(jobs, "get_profile", return_value=profile),
                        patch.object(jobs, "fetch_latest_ruling", return_value=(b"%PDF-1.7\nfixture", "act.pdf")),
                        patch.object(jobs.task_service, "mark_recognizing") as mark_recognizing,
                        patch.object(jobs.task_service, "log") as log_event,
                        patch.object(jobs.task_service, "fail") as fail,
                    ):
                        jobs.recognize_from_kad(task.id)
            finally:
                os.umask(previous_umask)

            mark_recognizing.assert_called_once_with(db, task)
            self.assertEqual([call.args[2] for call in log_event.call_args_list], [
                "kad_lookup_started",
                "kad_document_received",
            ])
            event_messages = [call.args[3] for call in log_event.call_args_list]
            self.assertTrue(all(task.case_number not in message for message in event_messages))
            fail.assert_called_once_with(
                db,
                task,
                "MKAD acquisition or document parsing failed (ValueError)",
            )
            self.assertTrue(Path(task.document.stored_path).is_file())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(Path(task.document.stored_path).stat().st_mode), 0o600)
            self.assertIn("task_id=73", captured.output[0])
            self.assertIn("error_type=ValueError", captured.output[0])
            self.assertNotIn("invalid PDF text layer", captured.output[0])
            self.assertNotIn("Traceback", captured.output[0])

    def test_result_commit_ack_loss_does_not_mark_saved_kad_result_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = SimpleNamespace(
                id=74,
                case_number="А99-12345/2099",
                assignee_id=8,
                profile="person",
                status=TaskStatus.recognizing,
                document=SimpleNamespace(stored_path=str(Path(temporary) / "source.pdf")),
            )
            db = MagicMock()
            db.get.return_value = task
            calls = 0

            @contextmanager
            def session_scope():
                nonlocal calls
                calls += 1
                yield db
                if calls == 3:
                    raise RuntimeError("synthetic commit acknowledgement loss")

            profile = MagicMock()
            card = object()
            profile.parse.return_value = card

            def save_recognized(_db, current_task, _card):
                current_task.status = TaskStatus.needs_review

            with (
                patch.object(jobs, "KAD_ACQUISITION_ENABLED", True),
                patch.object(jobs, "session_scope", side_effect=lambda: session_scope()),
                patch.object(jobs, "get_profile", return_value=profile),
                patch.object(jobs, "fetch_latest_ruling", return_value=(b"%PDF-1.7\nfixture", "act.pdf")),
                patch.object(jobs.task_service, "mark_recognizing", return_value=True),
                patch.object(jobs.task_service, "log"),
                patch.object(jobs.task_service, "save_recognized", side_effect=save_recognized) as save,
                patch.object(jobs.task_service, "fail") as fail,
                self.assertLogs(jobs.logger, level="ERROR") as captured,
            ):
                jobs.recognize_from_kad(task.id)

            self.assertEqual(calls, 3)
            profile.parse.assert_called_once_with(Path(task.document.stored_path))
            save.assert_called_once_with(db, task, card)
            fail.assert_not_called()
            self.assertEqual(task.status, TaskStatus.needs_review)
            self.assertIn("MKAD parsed-result persistence failed task_id=74", captured.output[0])
            self.assertNotIn("synthetic commit acknowledgement loss", "\n".join(captured.output))

    def test_unqualified_worker_raises_before_database_or_network_access(self) -> None:
        self.assertFalse(KAD_ACQUISITION_ENABLED)
        with (
            patch.object(jobs, "session_scope", side_effect=AssertionError("database access")) as db,
            patch.object(jobs, "fetch_latest_ruling", side_effect=AssertionError("network access")) as fetch,
            self.assertLogs(jobs.logger, level="ERROR") as captured,
        ):
            with self.assertRaises(jobs.KADAcquisitionDisabled):
                jobs.recognize_from_kad(73)

        db.assert_not_called()
        fetch.assert_not_called()
        self.assertIn("candidate_selection_unqualified", captured.output[0])


if __name__ == "__main__":
    unittest.main()
