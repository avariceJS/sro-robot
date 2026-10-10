from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import cli
from app.db.models import TaskStatus


class OpsStatusCommandTests(unittest.TestCase):
    def _failed_registry_patch(self, ocr_count: int = 0, sro_count: int = 0):
        registries = [MagicMock(), MagicMock()]
        registries[0].get_job_count.return_value = ocr_count
        registries[1].get_job_count.return_value = sro_count
        return patch.object(cli, "FailedJobRegistry", side_effect=registries), registries

    def _database_engine(self):
        connection = MagicMock()
        connection.execute.side_effect = [
            MagicMock(all=MagicMock(return_value=[(TaskStatus.new, 3), (TaskStatus.recognizing, 2)])),
            MagicMock(scalar_one=MagicMock(return_value=datetime(2026, 1, 2, tzinfo=timezone.utc))),
        ]
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        return engine, connection

    def test_reports_aggregates_without_task_identifiers_or_mutations(self) -> None:
        engine, connection = self._database_engine()
        redis_client = MagicMock()
        queue = MagicMock()
        queue.count = 7
        sro_queue = MagicMock()
        sro_queue.count = 1
        registry_patch, registries = self._failed_registry_patch(4, 3)
        output = StringIO()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", side_effect=[queue, sro_queue]) as queue_type,
            patch.object(cli.Worker, "all", return_value=[]) as workers,
            registry_patch,
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 1)
        self.assertIn("- new: 3", output.getvalue())
        self.assertIn("- recognizing: 2", output.getvalue())
        self.assertIn("Queue depth: ocr=7 sro=1", output.getvalue())
        self.assertIn("Failed RQ registry entries: ocr=4 sro=3", output.getvalue())
        self.assertIn("Registered RQ workers: ocr=0 sro=0", output.getvalue())
        self.assertIn("Queue backlog has no registered worker: ocr, sro", output.getvalue())
        self.assertIn("no task was changed or re-enqueued", output.getvalue())
        self.assertNotIn("document", output.getvalue())
        self.assertEqual(connection.execute.call_count, 2)
        self.assertEqual(queue_type.call_count, 2)
        self.assertEqual(workers.call_count, 2)
        self.assertEqual(registries[0].get_job_count.call_args.kwargs, {"cleanup": False})
        self.assertEqual(registries[1].get_job_count.call_args.kwargs, {"cleanup": False})
        self.assertEqual(workers.call_args_list[0].kwargs["queue"], queue)
        self.assertEqual(workers.call_args_list[1].kwargs["queue"], sro_queue)
        redis_client.close.assert_called_once_with()

    def test_backlog_with_queue_worker_is_healthy(self) -> None:
        engine, _ = self._database_engine()
        redis_client = MagicMock()
        ocr_queue = MagicMock()
        ocr_queue.count = 7
        sro_queue = MagicMock()
        sro_queue.count = 0
        registry_patch, _registries = self._failed_registry_patch()
        output = StringIO()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", side_effect=[ocr_queue, sro_queue]),
            registry_patch,
            patch.object(
                cli.Worker,
                "all",
                side_effect=[
                    [SimpleNamespace(last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=42))],
                    [],
                ],
            ),
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 0)
        self.assertIn("Registered RQ workers: ocr=1 sro=0", output.getvalue())
        self.assertRegex(
            output.getvalue(),
            r"RQ worker heartbeat \(oldest age seconds / missing\): ocr=\d+s/missing=0 sro=none",
        )
        self.assertNotIn("Queue backlog has no registered worker", output.getvalue())

    def test_reports_worker_registration_without_heartbeat_as_unknown(self) -> None:
        engine, _ = self._database_engine()
        redis_client = MagicMock()
        ocr_queue = MagicMock()
        ocr_queue.count = 0
        sro_queue = MagicMock()
        sro_queue.count = 0
        registry_patch, _registries = self._failed_registry_patch()
        output = StringIO()
        private_worker = SimpleNamespace(name="host-and-pid-must-not-leak", last_heartbeat=None)

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", side_effect=[ocr_queue, sro_queue]),
            patch.object(cli.Worker, "all", side_effect=[[private_worker], []]),
            registry_patch,
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 0)
        self.assertIn("RQ worker heartbeat (oldest age seconds / missing): ocr=unknown/missing=1 sro=none", output.getvalue())
        self.assertNotIn(private_worker.name, output.getvalue())

    def test_idle_queues_need_no_registered_worker(self) -> None:
        engine, _ = self._database_engine()
        redis_client = MagicMock()
        ocr_queue = MagicMock()
        ocr_queue.count = 0
        sro_queue = MagicMock()
        sro_queue.count = 0
        registry_patch, _registries = self._failed_registry_patch()
        output = StringIO()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", side_effect=[ocr_queue, sro_queue]),
            patch.object(cli.Worker, "all", return_value=[]),
            registry_patch,
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 0)
        self.assertNotIn("Queue backlog has no registered worker", output.getvalue())

    def test_database_failure_does_not_contact_redis(self) -> None:
        engine = MagicMock()
        engine.connect.side_effect = OSError("synthetic database failure")
        output = StringIO()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url") as redis_from_url,
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 2)
        self.assertIn("Проверка задач недоступна: OSError", output.getvalue())
        self.assertNotIn("synthetic database failure", output.getvalue())
        redis_from_url.assert_not_called()

    def test_redis_failure_returns_nonzero_and_closes_client(self) -> None:
        engine, _ = self._database_engine()
        redis_client = MagicMock()
        redis_client.ping.side_effect = OSError("synthetic redis failure")
        output = StringIO()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 2)
        self.assertIn("Проверка очередей недоступна: OSError", output.getvalue())
        self.assertNotIn("synthetic redis failure", output.getvalue())
        redis_client.close.assert_called_once_with()

    def test_failed_registry_error_is_not_reported_as_zero_or_leaked(self) -> None:
        engine, _ = self._database_engine()
        redis_client = MagicMock()
        ocr_queue = MagicMock()
        sro_queue = MagicMock()
        failed_registry = MagicMock()
        failed_registry.get_job_count.side_effect = RuntimeError("synthetic registry detail")
        output = StringIO()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", side_effect=[ocr_queue, sro_queue]),
            patch.object(cli.Worker, "all", return_value=[]),
            patch.object(cli, "FailedJobRegistry", return_value=failed_registry),
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 2)
        self.assertIn("Проверка очередей недоступна: RuntimeError", output.getvalue())
        self.assertNotIn("synthetic registry detail", output.getvalue())
        self.assertNotIn("Failed RQ registry entries:", output.getvalue())
        failed_registry.get_job_count.assert_called_once_with(cleanup=False)
        redis_client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
