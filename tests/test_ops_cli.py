from __future__ import annotations

import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import cli
from app.db.models import TaskStatus
from app.services.outbox import OutboxSummary
from app.worker_health import DEFAULT_WORKER_TTL


class OpsStatusCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inspector = SimpleNamespace(has_table=lambda _name: False)
        self.inspect_patcher = patch.object(cli, "inspect", return_value=self.inspector)
        self.inspect_patcher.start()
        self.outbox_summary_patcher = patch.object(
            cli.outbox,
            "status_summary",
            return_value=OutboxSummary(0, 0, 0, 0, None, ()),
        )
        self.outbox_summary_patcher.start()

    def tearDown(self) -> None:
        self.inspect_patcher.stop()
        self.outbox_summary_patcher.stop()

    def _failed_registry_patch(
        self,
        ocr_count: int = 0,
        sro_count: int = 0,
        *,
        ocr_started: int = 0,
        sro_started: int = 0,
    ):
        classes = {
            "started": "StartedJobRegistry",
            "deferred": "DeferredJobRegistry",
            "scheduled": "ScheduledJobRegistry",
            "failed": "FailedJobRegistry",
        }
        registries = {
            state: [MagicMock(), MagicMock()]
            for state in classes
        }
        values = {
            "started": [ocr_started, sro_started],
            "deferred": [0, 0],
            "scheduled": [0, 0],
            "failed": [ocr_count, sro_count],
        }
        stack = ExitStack()
        for state, attr in classes.items():
            for index, registry in enumerate(registries[state]):
                registry.get_job_count.return_value = values[state][index]
            stack.enter_context(
                patch.object(cli, attr, side_effect=registries[state])
            )
        return stack, registries

    def _database_engine(self):
        connection = MagicMock()
        connection.execute.side_effect = [
            MagicMock(all=MagicMock(return_value=[(TaskStatus.new, 3), (TaskStatus.recognizing, 2)])),
            MagicMock(all=MagicMock(return_value=[
                ("kad", TaskStatus.new, 1),
                ("upload", TaskStatus.new, 2),
                ("upload", TaskStatus.recognizing, 2),
            ])),
            MagicMock(scalar_one=MagicMock(return_value=datetime(2026, 1, 2, tzinfo=timezone.utc))),
            MagicMock(scalar_one=MagicMock(return_value=datetime(2026, 1, 1, tzinfo=timezone.utc))),
            MagicMock(scalar_one=MagicMock(return_value=0)),
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
        self.assertIn("kad/new: 1", output.getvalue())
        self.assertIn("upload/new: 2", output.getvalue())
        self.assertIn("upload/recognizing: 2", output.getvalue())
        self.assertIn("Oldest new task created_at: 2026-01-01 00:00:00+00:00", output.getvalue())
        self.assertIn("Recognizing tasks without a lease: unavailable (task_lease table absent)", output.getvalue())
        self.assertIn("Queue depth: ocr=7 sro=1", output.getvalue())
        self.assertIn("Queue-head RQ job enqueued_at: ocr=unknown sro=unknown", output.getvalue())
        self.assertIn("Queue-head age: ocr=unknown sro=unknown", output.getvalue())
        self.assertIn(
            "RQ registry entries (cleanup disabled): "
            "ocr=started=0,deferred=0,scheduled=0,failed=4 "
            "sro=started=0,deferred=0,scheduled=0,failed=3",
            output.getvalue(),
        )
        self.assertIn("Registered RQ workers: ocr=0 sro=0", output.getvalue())
        self.assertIn("Queue backlog has no registered worker: ocr, sro", output.getvalue())
        self.assertIn("no task was changed or re-enqueued", output.getvalue())
        self.assertNotIn("document", output.getvalue())
        self.assertNotIn("private-job-id", output.getvalue())
        self.assertEqual(connection.execute.call_count, 4)
        self.assertEqual(queue_type.call_count, 2)
        self.assertEqual(workers.call_count, 2)
        for state in registries.values():
            for registry in state:
                self.assertEqual(registry.get_job_count.call_args.kwargs, {"cleanup": False})
        self.assertEqual(workers.call_args_list[0].kwargs["queue"], queue)
        self.assertEqual(workers.call_args_list[1].kwargs["queue"], sro_queue)
        redis_client.close.assert_called_once_with()

    def test_postgresql_snapshot_uses_read_only_transaction_and_local_timeouts(self) -> None:
        engine, connection = self._database_engine()
        connection.dialect.name = "postgresql"
        connection.execute.side_effect = None
        connection.execute.return_value = MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one=MagicMock(return_value=None),
        )
        redis_client = MagicMock()
        registry_patch, _registries = self._failed_registry_patch()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", side_effect=[MagicMock(count=0), MagicMock(count=0)]),
            patch.object(cli.Worker, "all", return_value=[]),
            registry_patch,
            redirect_stdout(StringIO()),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 0)
        first_sql = str(connection.execute.call_args_list[0].args[0])
        second_call = connection.execute.call_args_list[1]
        self.assertEqual(first_sql, "SET TRANSACTION READ ONLY")
        self.assertIn("set_config('lock_timeout'", str(second_call.args[0]))
        self.assertIn("set_config('statement_timeout'", str(second_call.args[0]))
        self.assertEqual(
            second_call.args[1],
            {
                "lock_timeout": f"{cli.settings.database_lock_timeout_ms}ms",
                "statement_timeout": f"{cli.settings.database_statement_timeout_ms}ms",
            },
        )
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
        self.assertIn(f"RQ worker heartbeat freshness limit: {DEFAULT_WORKER_TTL} seconds", output.getvalue())
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

    def test_backlog_without_any_recorded_worker_heartbeat_is_unhealthy(self) -> None:
        engine, _ = self._database_engine()
        redis_client = MagicMock()
        ocr_queue = MagicMock()
        ocr_queue.count = 1
        sro_queue = MagicMock()
        sro_queue.count = 0
        registry_patch, _registries = self._failed_registry_patch()
        output = StringIO()
        private_worker = SimpleNamespace(name="synthetic-private-worker", last_heartbeat=None)

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", side_effect=[ocr_queue, sro_queue]),
            patch.object(cli.Worker, "all", side_effect=[[private_worker], []]),
            registry_patch,
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 1)
        self.assertIn(
            "Queue backlog has no worker with a fresh heartbeat: ocr",
            output.getvalue(),
        )
        self.assertNotIn(private_worker.name, output.getvalue())

    def test_backlog_with_only_stale_worker_heartbeats_is_unhealthy(self) -> None:
        engine, _ = self._database_engine()
        redis_client = MagicMock()
        ocr_queue = MagicMock()
        ocr_queue.count = 1
        sro_queue = MagicMock()
        sro_queue.count = 0
        registry_patch, _registries = self._failed_registry_patch()
        output = StringIO()
        stale_worker = SimpleNamespace(
            name="synthetic-stale-worker",
            last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=DEFAULT_WORKER_TTL + 1),
        )

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", side_effect=[ocr_queue, sro_queue]),
            patch.object(cli.Worker, "all", side_effect=[[stale_worker], []]),
            registry_patch,
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 1)
        self.assertIn("Queue backlog has no worker with a fresh heartbeat: ocr", output.getvalue())
        self.assertNotIn(stale_worker.name, output.getvalue())

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

    def test_started_job_without_fresh_worker_requires_reconciliation(self) -> None:
        engine, _ = self._database_engine()
        redis_client = MagicMock()
        ocr_queue = MagicMock()
        ocr_queue.count = 0
        sro_queue = MagicMock()
        sro_queue.count = 0
        registry_patch, _registries = self._failed_registry_patch(ocr_started=2)
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

        self.assertEqual(result, 1)
        self.assertIn("started=2", output.getvalue())
        self.assertIn(
            "Started RQ entries exist without a fresh worker heartbeat for the queue: ocr",
            output.getvalue(),
        )

    def test_started_registry_with_a_fresh_queue_worker_is_not_flagged(self) -> None:
        engine, _ = self._database_engine()
        redis_client = MagicMock()
        ocr_queue = MagicMock()
        ocr_queue.count = 0
        sro_queue = MagicMock()
        sro_queue.count = 0
        registry_patch, _registries = self._failed_registry_patch(ocr_started=1)
        worker = SimpleNamespace(last_heartbeat=datetime.now(timezone.utc))
        output = StringIO()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", side_effect=[ocr_queue, sro_queue]),
            patch.object(cli.Worker, "all", side_effect=[[worker], []]),
            registry_patch,
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 0)
        self.assertIn("started=1", output.getvalue())
        self.assertNotIn("Started RQ entries exist", output.getvalue())

    def test_reports_no_new_task_timestamp_when_queue_is_empty(self) -> None:
        engine, connection = self._database_engine()
        connection.execute.side_effect = [
            MagicMock(all=MagicMock(return_value=[(TaskStatus.recognizing, 2)])),
            MagicMock(all=MagicMock(return_value=[])),
            MagicMock(scalar_one=MagicMock(return_value=datetime(2026, 1, 2, tzinfo=timezone.utc))),
            MagicMock(scalar_one=MagicMock(return_value=None)),
            MagicMock(scalar_one=MagicMock(return_value=0)),
        ]
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
        self.assertIn("Oldest new task created_at: none", output.getvalue())

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
        self.assertNotIn("failed=0", output.getvalue())
        failed_registry.get_job_count.assert_called_once_with(cleanup=False)
        redis_client.close.assert_called_once_with()

    def test_reports_all_unleased_recognizing_tasks_for_reconciliation(self) -> None:
        self.inspector.has_table = lambda name: name == "task_lease"
        engine, connection = self._database_engine()
        connection.execute.side_effect = [
            MagicMock(all=MagicMock(return_value=[(TaskStatus.new, 3), (TaskStatus.recognizing, 2)])),
            MagicMock(all=MagicMock(return_value=[])),
            MagicMock(scalar_one=MagicMock(return_value=datetime(2026, 1, 2, tzinfo=timezone.utc))),
            MagicMock(scalar_one=MagicMock(return_value=datetime(2026, 1, 1, tzinfo=timezone.utc))),
            MagicMock(scalar_one=MagicMock(return_value=2)),
        ]
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

        self.assertEqual(result, 1)
        self.assertIn(
            "Recognizing tasks without a lease (require reconciliation): 2",
            output.getvalue(),
        )
        self.assertIn(
            "Unleased recognizing tasks require read-only reconciliation before processing",
            output.getvalue(),
        )
        statement = str(connection.execute.call_args_list[4].args[0])
        self.assertIn("LEFT OUTER JOIN task_lease", statement)
        self.assertIn("task.status =", statement)
        self.assertNotIn("document", statement)

    def test_pending_outbox_intents_make_ops_status_unhealthy(self) -> None:
        self.inspector.has_table = lambda name: name == "job_outbox"
        engine, connection = self._database_engine()
        connection.execute.side_effect = [
            MagicMock(all=MagicMock(return_value=[])),
            MagicMock(all=MagicMock(return_value=[])),
            MagicMock(scalar_one=MagicMock(return_value=None)),
            MagicMock(scalar_one=MagicMock(return_value=None)),
        ]
        redis_client = MagicMock()
        ocr_queue = MagicMock(count=0)
        sro_queue = MagicMock(count=0)
        registry_patch, _registries = self._failed_registry_patch()
        output = StringIO()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", side_effect=[ocr_queue, sro_queue]),
            patch.object(cli.Worker, "all", return_value=[]),
            patch.object(
                cli.outbox,
                "status_summary",
                return_value=OutboxSummary(2, 1, 0, 1, 19, (("ConnectionError", 2),)),
            ),
            registry_patch,
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 1)
        self.assertIn("pending=2 eligible=1 withheld=0 leased=0 delayed=1", output.getvalue())
        self.assertIn("oldest_age=19s last_errors=ConnectionError=2", output.getvalue())
        self.assertIn("run outbox-dispatch after review", output.getvalue())

    def test_withheld_outbox_intent_reports_release_gate_without_retry_advice(self) -> None:
        self.inspector.has_table = lambda name: name == "job_outbox"
        engine, connection = self._database_engine()
        connection.execute.side_effect = [
            MagicMock(all=MagicMock(return_value=[])),
            MagicMock(all=MagicMock(return_value=[])),
            MagicMock(scalar_one=MagicMock(return_value=None)),
            MagicMock(scalar_one=MagicMock(return_value=None)),
        ]
        redis_client = MagicMock()
        registry_patch, _registries = self._failed_registry_patch()
        output = StringIO()

        with (
            patch.object(cli, "engine", engine),
            patch.object(cli.Redis, "from_url", return_value=redis_client),
            patch.object(cli, "Queue", side_effect=[MagicMock(count=0), MagicMock(count=0)]),
            patch.object(cli.Worker, "all", return_value=[]),
            patch.object(
                cli.outbox,
                "status_summary",
                return_value=OutboxSummary(1, 0, 0, 0, 19, (), withheld=1),
            ),
            registry_patch,
            redirect_stdout(output),
        ):
            result = cli.cmd_ops_status(None)

        self.assertEqual(result, 1)
        self.assertIn("withheld=1", output.getvalue())
        self.assertIn("satisfy the capability release gate before dispatch", output.getvalue())
        self.assertNotIn("run outbox-dispatch", output.getvalue())


class OutboxDispatchCommandTests(unittest.TestCase):
    def test_watch_requires_explicit_apply(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = cli.cmd_outbox_dispatch(
                SimpleNamespace(apply=False, limit=10, watch=True, poll_seconds=5)
            )

        self.assertEqual(result, 2)
        self.assertIn("watch mode requires --apply", output.getvalue())

    def test_watch_rejects_out_of_range_batch_or_poll_limits_before_running(self) -> None:
        for args, expected in (
            (SimpleNamespace(apply=True, limit=1001, watch=True, poll_seconds=5), "limit"),
            (SimpleNamespace(apply=True, limit=10, watch=True, poll_seconds=0), "interval"),
        ):
            with self.subTest(expected=expected):
                output = StringIO()
                with redirect_stdout(output):
                    result = cli.cmd_outbox_dispatch(args)

                self.assertEqual(result, 2)
                self.assertIn(expected, output.getvalue())

    def test_watch_retries_transient_failures_with_backoff_and_stops_cleanly(self) -> None:
        class StopAfterSecondWait:
            def __init__(self) -> None:
                self.waits: list[float] = []
                self.stopped = False

            def is_set(self) -> bool:
                return self.stopped

            def wait(self, seconds: float) -> bool:
                self.waits.append(seconds)
                if len(self.waits) == 2:
                    self.stopped = True
                    return True
                return False

        stop_event = StopAfterSecondWait()
        calls = 0

        def dispatch_cycle():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("synthetic-private-connection-detail")
            return 2, OutboxSummary(1, 0, 0, 1, 8, ())

        output = StringIO()
        with redirect_stdout(output):
            result = cli.run_outbox_dispatch_watch(
                dispatch_cycle,
                stop_event=stop_event,
                poll_seconds=5,
            )

        self.assertEqual(result, 0)
        self.assertEqual(calls, 2)
        self.assertEqual(stop_event.waits, [1.0, 5])
        self.assertIn("Outbox dispatcher cycle failed: ConnectionError", output.getvalue())
        self.assertNotIn("synthetic-private-connection-detail", output.getvalue())
        self.assertIn("outbox_dispatcher_stopped", output.getvalue())

    def test_default_outbox_command_reports_without_dispatching(self) -> None:
        output = StringIO()
        db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = db

        with (
            patch.object(cli, "engine"),
            patch.object(
                cli.MigrationContext,
                "configure",
                return_value=SimpleNamespace(
                    get_current_revision=lambda: cli.EXPECTED_SCHEMA_REVISION
                ),
            ),
            patch.object(cli, "session_scope", return_value=context),
            patch.object(
                cli.outbox,
                "status_summary",
                return_value=OutboxSummary(3, 2, 1, 0, 45, (("TimeoutError", 1),)),
            ),
            patch.object(cli, "configure_read_only_diagnostic_transaction") as guard,
            patch.object(cli.outbox, "dispatch_ready") as dispatch,
            redirect_stdout(output),
        ):
            result = cli.cmd_outbox_dispatch(SimpleNamespace(apply=False, limit=10))

        self.assertEqual(result, 0)
        self.assertIn("pending=3 eligible=2 withheld=0 leased=1 delayed=0", output.getvalue())
        self.assertIn("oldest_age=45s last_errors=TimeoutError=1", output.getvalue())
        dispatch.assert_not_called()
        self.assertEqual(guard.call_count, 2)

    def test_apply_dispatches_only_through_the_bounded_service(self) -> None:
        output = StringIO()
        db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = db

        with (
            patch.object(cli, "engine"),
            patch.object(
                cli.MigrationContext,
                "configure",
                return_value=SimpleNamespace(
                    get_current_revision=lambda: cli.EXPECTED_SCHEMA_REVISION
                ),
            ),
            patch.object(cli, "session_scope", return_value=context),
            patch.object(cli, "configure_read_only_diagnostic_transaction") as guard,
            patch.object(cli.outbox, "dispatch_ready", return_value=(4, 0)) as dispatch,
            patch.object(
                cli.outbox,
                "status_summary",
                return_value=OutboxSummary(0, 0, 0, 0, None, ()),
            ),
            redirect_stdout(output),
        ):
            result = cli.cmd_outbox_dispatch(SimpleNamespace(apply=True, limit=25))

        self.assertEqual(result, 0)
        dispatch.assert_called_once_with(db, limit=25)
        self.assertEqual(guard.call_count, 1)

    def test_outbox_refuses_to_dispatch_on_a_stale_or_unversioned_schema(self) -> None:
        session_scope = MagicMock()
        with (
            patch.object(cli, "engine"),
            patch.object(
                cli.MigrationContext,
                "configure",
                return_value=SimpleNamespace(get_current_revision=lambda: None),
            ),
            patch.object(cli, "session_scope", session_scope),
        ):
            with self.assertRaises(cli.OutboxSchemaUnavailable):
                cli._outbox_dispatch_cycle(apply=True, limit=10)

        session_scope.assert_not_called()


class QueueHeadJobTimestampTests(unittest.TestCase):
    def test_reads_only_timestamp_without_deserializing_or_disclosing_job_id(self) -> None:
        queue = MagicMock()
        queue.get_job_ids.return_value = ["private-job-id"]
        redis_client = MagicMock()
        redis_client.hget.return_value = b"2026-01-02T03:04:05Z"

        with patch.object(cli.Job, "fetch", side_effect=AssertionError("payload must not be loaded")):
            timestamp = cli._queue_head_job_enqueued_at(queue, redis_client, count=1)

        self.assertEqual(timestamp, "2026-01-02T03:04:05+00:00")
        queue.get_job_ids.assert_called_once_with(offset=0, length=1)
        redis_client.hget.assert_called_once_with("rq:job:private-job-id", "enqueued_at")

    def test_reports_none_for_empty_queue_without_redis_job_lookup(self) -> None:
        queue = MagicMock()
        redis_client = MagicMock()

        self.assertEqual(cli._queue_head_job_enqueued_at(queue, redis_client, count=0), "none")

        queue.get_job_ids.assert_not_called()
        redis_client.hget.assert_not_called()

    def test_missing_or_malformed_metadata_is_unknown(self) -> None:
        queue = MagicMock()
        queue.get_job_ids.return_value = ["private-job-id"]
        redis_client = MagicMock()
        redis_client.hget.return_value = b"not-a-timestamp"

        self.assertEqual(cli._queue_head_job_enqueued_at(queue, redis_client, count=1), "unknown")

        redis_client.hget.return_value = None
        self.assertEqual(cli._queue_head_job_enqueued_at(queue, redis_client, count=1), "unknown")

    def test_timestamp_outside_utc_normalization_range_is_unknown(self) -> None:
        queue = MagicMock()
        queue.get_job_ids.return_value = ["private-job-id"]
        redis_client = MagicMock()
        redis_client.hget.return_value = b"0001-01-01T00:00:00+23:59"

        self.assertEqual(cli._queue_head_job_enqueued_at(queue, redis_client, count=1), "unknown")

    def test_queue_race_does_not_report_a_job_id(self) -> None:
        queue = MagicMock()
        queue.get_job_ids.return_value = []
        redis_client = MagicMock()

        self.assertEqual(cli._queue_head_job_enqueued_at(queue, redis_client, count=1), "unknown")

        redis_client.hget.assert_not_called()


class QueueHeadAgeTests(unittest.TestCase):
    def test_reports_age_using_aware_timestamps(self) -> None:
        checked_at = datetime(2026, 1, 2, 3, 6, 5, tzinfo=timezone.utc)
        self.assertEqual(
            cli._queue_head_job_age_seconds("2026-01-02T03:04:05+00:00", checked_at),
            "120s",
        )

    def test_distinguishes_future_clock_skew_and_missing_values(self) -> None:
        checked_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.assertEqual(
            cli._queue_head_job_age_seconds("2026-01-02T03:05:05+00:00", checked_at),
            "future",
        )
        self.assertEqual(cli._queue_head_job_age_seconds("unknown", checked_at), "unknown")
        self.assertEqual(cli._queue_head_job_age_seconds("none", checked_at), "none")
        self.assertEqual(
            cli._queue_head_job_age_seconds("2026-01-02T03:04:05", checked_at),
            "unknown",
        )


if __name__ == "__main__":
    unittest.main()
