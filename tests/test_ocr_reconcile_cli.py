from __future__ import annotations

import unittest
from argparse import Namespace
from contextlib import contextmanager
from io import StringIO
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from app import cli


class OcrReconcileCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lease_table = patch.object(cli, "_task_lease_table_exists", return_value=True)
        self.lease_table_mock = self.lease_table.start()

    def tearDown(self) -> None:
        self.lease_table.stop()

    def _session_scope(self):
        @contextmanager
        def scope():
            yield MagicMock()

        return scope

    def test_default_mode_only_reports_candidates(self) -> None:
        output = StringIO()
        with (
            patch.object(cli, "session_scope", side_effect=lambda: self._session_scope()()),
            patch.object(cli, "reconcilable_ocr_task_ids", return_value=[2, 3, 5]),
            patch.object(cli, "configure_read_only_diagnostic_transaction") as guard,
            patch.object(cli.Redis, "from_url") as redis_from_url,
            redirect_stdout(output),
        ):
            result = cli.cmd_ocr_reconcile(Namespace(apply=False))

        self.assertEqual(result, 0)
        self.assertIn("Eligible local OCR tasks: 3", output.getvalue())
        self.assertNotIn("2", output.getvalue())
        redis_from_url.assert_not_called()
        guard.assert_called_once()
        self.assertEqual(guard.call_count, 1)

    def test_limit_validation_fails_before_database_or_redis_access(self) -> None:
        output = StringIO()
        with (
            patch.object(cli, "_task_lease_table_exists") as lease_table,
            patch.object(cli, "reconcilable_ocr_task_ids") as candidates,
            patch.object(cli.Redis, "from_url") as redis_from_url,
            redirect_stdout(output),
        ):
            result = cli.cmd_ocr_reconcile(Namespace(apply=False, limit=1001))

        self.assertEqual(result, 2)
        self.assertIn("limit must be between 1 and 1000", output.getvalue())
        lease_table.assert_not_called()
        candidates.assert_not_called()
        redis_from_url.assert_not_called()

    def test_report_uses_bounded_oldest_first_batch_and_reports_more_candidates(self) -> None:
        output = StringIO()
        with (
            patch.object(cli, "session_scope", side_effect=lambda: self._session_scope()()),
            patch.object(cli, "reconcilable_ocr_task_ids", return_value=[11, 19, 25, 31]) as candidates,
            patch.object(cli.Redis, "from_url") as redis_from_url,
            redirect_stdout(output),
        ):
            result = cli.cmd_ocr_reconcile(Namespace(apply=False, limit=3))

        self.assertEqual(result, 0)
        candidates.assert_called_once_with(candidates.call_args.args[0], limit=4)
        self.assertIn("Eligible local OCR tasks: 3", output.getvalue())
        self.assertIn("at least one more candidate remains beyond limit 3", output.getvalue())
        self.assertIn("review queue state before another batch", output.getvalue())
        self.assertNotIn("11", output.getvalue())
        redis_from_url.assert_not_called()

    def test_missing_lease_schema_fails_before_candidate_or_queue_access(self) -> None:
        self.lease_table_mock.return_value = False
        output = StringIO()

        with (
            patch.object(cli, "reconcilable_ocr_task_ids") as candidates,
            patch.object(cli.Redis, "from_url") as redis_from_url,
            redirect_stdout(output),
        ):
            result = cli.cmd_ocr_reconcile(Namespace(apply=False))

        self.assertEqual(result, 2)
        self.assertIn("отсутствует таблица task_lease", output.getvalue())
        self.assertIn("задания и очередь не проверялись", output.getvalue())
        candidates.assert_not_called()
        redis_from_url.assert_not_called()

    def test_apply_requires_lease_feature_flag(self) -> None:
        output = StringIO()
        with (
            patch.object(cli, "session_scope", side_effect=lambda: self._session_scope()()),
            patch.object(cli, "reconcilable_ocr_task_ids", return_value=[7]),
            patch.object(cli.settings, "ocr_task_leases_enabled", False),
            patch.object(cli.Redis, "from_url") as redis_from_url,
            redirect_stdout(output),
        ):
            result = cli.cmd_ocr_reconcile(Namespace(apply=True))

        self.assertEqual(result, 2)
        self.assertIn("OCR_TASK_LEASES_ENABLED", output.getvalue())
        redis_from_url.assert_not_called()

    def test_apply_enqueues_only_database_reconciled_candidates(self) -> None:
        redis_client = MagicMock()
        queue = MagicMock()
        output = StringIO()
        with (
            patch.object(cli, "session_scope", side_effect=lambda: self._session_scope()()),
            patch.object(cli, "reconcilable_ocr_task_ids", return_value=[11, 19]),
            patch.object(cli.settings, "ocr_task_leases_enabled", True),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", return_value=queue),
            patch("app.workers.jobs.recognize") as recognize,
            redirect_stdout(output),
        ):
            result = cli.cmd_ocr_reconcile(Namespace(apply=True))

        self.assertEqual(result, 0)
        self.assertEqual(queue.enqueue.call_count, 2)
        self.assertEqual(
            [call.args for call in queue.enqueue.call_args_list],
            [(recognize, 11), (recognize, 19)],
        )
        self.assertIn("database-fenced", output.getvalue())
        redis_client.close.assert_called_once_with()

    def test_apply_enqueues_at_most_the_requested_batch(self) -> None:
        redis_client = MagicMock()
        queue = MagicMock()
        output = StringIO()
        with (
            patch.object(cli, "session_scope", side_effect=lambda: self._session_scope()()),
            patch.object(cli, "reconcilable_ocr_task_ids", return_value=[11, 19, 25]),
            patch.object(cli.settings, "ocr_task_leases_enabled", True),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", return_value=queue),
            patch("app.workers.jobs.recognize") as recognize,
            redirect_stdout(output),
        ):
            result = cli.cmd_ocr_reconcile(Namespace(apply=True, limit=2))

        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args for call in queue.enqueue.call_args_list],
            [(recognize, 11), (recognize, 19)],
        )
        self.assertIn("Enqueued 2 eligible local OCR task(s)", output.getvalue())
        self.assertIn("more eligible tasks remain beyond limit 2", output.getvalue())
        self.assertIn("review queue state before another batch", output.getvalue())
        redis_client.close.assert_called_once_with()

    def test_close_failure_does_not_hide_successful_enqueue(self) -> None:
        redis_client = MagicMock()
        redis_client.close.side_effect = OSError("synthetic close failure")
        output = StringIO()
        with (
            patch.object(cli, "session_scope", side_effect=lambda: self._session_scope()()),
            patch.object(cli, "reconcilable_ocr_task_ids", return_value=[11]),
            patch.object(cli.settings, "ocr_task_leases_enabled", True),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue") as queue_class,
            patch("app.workers.jobs.recognize"),
            redirect_stdout(output),
        ):
            result = cli.cmd_ocr_reconcile(Namespace(apply=True))

        self.assertEqual(result, 0)
        queue_class.return_value.enqueue.assert_called_once()
        self.assertIn("Enqueued 1", output.getvalue())
        self.assertIn("OSError", output.getvalue())

    def test_lost_enqueue_acknowledgement_is_reported_as_unknown(self) -> None:
        redis_client = MagicMock()
        queue = MagicMock()
        queue.enqueue.side_effect = OSError("redis://synthetic-secret@private.invalid")
        output = StringIO()
        with (
            patch.object(cli, "session_scope", side_effect=lambda: self._session_scope()()),
            patch.object(cli, "reconcilable_ocr_task_ids", return_value=[11]),
            patch.object(cli.settings, "ocr_task_leases_enabled", True),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", return_value=queue),
            patch("app.workers.jobs.recognize"),
            redirect_stdout(output),
        ):
            result = cli.cmd_ocr_reconcile(Namespace(apply=True, limit=10))

        self.assertEqual(result, 2)
        self.assertIn("Redis подтвердил 0 из 1 заданий", output.getvalue())
        self.assertIn("исход последней отправки неизвестен", output.getvalue())
        self.assertIn("проверьте очередь и состояние задач перед повтором", output.getvalue())
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertNotIn("private.invalid", output.getvalue())
        self.assertNotIn("очередь приняла", output.getvalue())
        redis_client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
