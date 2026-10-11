"""Tests that uncaught HTTP failures produce diagnostics, not traceback pages."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from starlette.requests import Request

from app import main
from app.db.schema import EXPECTED_SCHEMA_REVISION
from app.worker_health import DEFAULT_WORKER_TTL
from app.main import logger, unexpected_error


def _database_result(values: list[str]):
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


def _ready_database_engine():
    connection = MagicMock()
    connection.execute.side_effect = [
        MagicMock(),
        _database_result([EXPECTED_SCHEMA_REVISION]),
    ]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    return engine


class ApplicationFailureLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unhandled_error_is_logged_and_response_is_generic(self) -> None:
        request = Request({
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/tasks/93/run",
            "raw_path": b"/tasks/93/run",
            "query_string": b"",
            "headers": [],
            "state": {"request_id": "a" * 32},
            "route": SimpleNamespace(path="/tasks/{task_id}/run"),
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        })
        response = None
        with self.assertLogs(logger, level="ERROR") as captured:
            try:
                raise RuntimeError("synthetic-sensitive-value")
            except RuntimeError as error:
                response = await unexpected_error(request, error)

        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.body.decode(), "Внутренняя ошибка сервера")
        self.assertIn(f"request_id={'a' * 32}", captured.output[0])
        self.assertIn("method=POST route=/tasks/{task_id}/run", captured.output[0])
        self.assertIn("error_type=RuntimeError", captured.output[0])
        self.assertNotIn("/tasks/93/run", captured.output[0])
        self.assertNotIn("Traceback", captured.output[0])
        self.assertNotIn("synthetic-sensitive-value", captured.output[0])
        self.assertNotIn("synthetic internal failure", response.body.decode())

    async def test_unmatched_route_log_does_not_include_user_supplied_path(self) -> None:
        private_path = "/lookup/synthetic-person@example.invalid"
        request = Request({
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": private_path,
            "raw_path": private_path.encode(),
            "query_string": b"",
            "headers": [],
            "state": {"request_id": "c" * 32},
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        })

        with self.assertLogs(logger, level="ERROR") as captured:
            response = await unexpected_error(request, RuntimeError("synthetic"))

        self.assertEqual(response.status_code, 500)
        self.assertIn("route=<unmatched>", captured.output[0])
        self.assertNotIn(private_path, captured.output[0])


class ReadinessCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue_type = patch.object(
            main,
            "Queue",
            side_effect=lambda name, connection: SimpleNamespace(name=name, count=0),
        )
        self.worker_type = patch.object(main, "Worker")
        self.queue_mock = self.queue_type.start()
        self.worker_mock = self.worker_type.start()
        self.addCleanup(self.queue_type.stop)
        self.addCleanup(self.worker_type.stop)

    def test_ready_when_database_and_redis_respond(self) -> None:
        db_connection = MagicMock()
        db_connection.execute.side_effect = [
            MagicMock(),
            _database_result([EXPECTED_SCHEMA_REVISION]),
        ]
        db_engine = MagicMock()
        db_engine.connect.return_value.__enter__.return_value = db_connection
        redis_client = MagicMock()

        with patch.object(main, "engine", db_engine), patch.object(
            main, "Redis"
        ) as redis_type:
            redis_type.from_url.return_value = redis_client
            response = main.readyz(None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.body,
            b'{"ok":true,"checks":{"database":"ok","database_schema":"ok","redis":"ok","queue_workers":"ok"}}',
        )
        self.assertEqual(db_connection.execute.call_count, 2)
        redis_client.ping.assert_called_once_with()
        redis_client.close.assert_called_once_with()

    def test_lease_enabled_readiness_requires_the_lease_schema(self) -> None:
        db_connection = MagicMock()
        db_engine = MagicMock()
        db_engine.connect.return_value.__enter__.return_value = db_connection
        db_connection.execute.side_effect = [
            MagicMock(),
            _database_result([EXPECTED_SCHEMA_REVISION]),
            _database_result(
                ["task_id", "owner_token", "job_kind", "attempt", "generation", "lease_until", "updated_at"]
            ),
        ]
        redis_client = MagicMock()

        with patch.object(main, "settings") as settings, patch.object(
            main, "engine", db_engine
        ), patch.object(main, "Redis") as redis_type:
            settings.ocr_task_leases_enabled = True
            settings.redis_url = "redis://synthetic"
            redis_type.from_url.return_value = redis_client
            response = main.readyz(None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.body,
            b'{"ok":true,"checks":{"database":"ok","database_schema":"ok","ocr_lease_schema":"ok","redis":"ok","queue_workers":"ok"}}',
        )
        self.assertEqual(db_connection.execute.call_count, 3)

    def test_lease_enabled_readiness_fails_closed_when_schema_is_incomplete(self) -> None:
        db_connection = MagicMock()
        db_engine = MagicMock()
        db_engine.connect.return_value.__enter__.return_value = db_connection
        db_connection.execute.side_effect = [
            MagicMock(),
            _database_result([EXPECTED_SCHEMA_REVISION]),
            _database_result(["task_id", "owner_token"]),
        ]
        redis_client = MagicMock()

        with patch.object(main, "settings") as settings, patch.object(
            main, "engine", db_engine
        ), patch.object(main, "Redis") as redis_type:
            settings.ocr_task_leases_enabled = True
            settings.redis_url = "redis://synthetic"
            redis_type.from_url.return_value = redis_client
            response = main.readyz(None)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.body,
            b'{"ok":false,"checks":{"database":"ok","database_schema":"ok","ocr_lease_schema":"unavailable","redis":"ok","queue_workers":"ok"}}',
        )
        self.assertNotIn(b"owner_token", response.body)

    def test_unavailable_dependency_returns_503_without_exception_details(self) -> None:
        db_engine = MagicMock()
        db_engine.connect.side_effect = OSError("synthetic://secret-host")
        redis_client = MagicMock()

        with patch.object(main, "engine", db_engine), patch.object(
            main, "Redis"
        ) as redis_type:
            redis_type.from_url.return_value = redis_client
            response = main.readyz(None)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.body,
            b'{"ok":false,"checks":{"database":"unavailable","redis":"ok","queue_workers":"ok"}}',
        )
        self.assertNotIn(b"secret-host", response.body)

    def test_redis_ping_failure_returns_503_logs_only_exception_type_and_closes_client(self) -> None:
        request = Request({
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/readyz",
            "raw_path": b"/readyz",
            "query_string": b"",
            "headers": [],
            "state": {"request_id": "b" * 32},
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        })
        db_engine = MagicMock()
        connection = MagicMock()
        connection.execute.side_effect = [
            MagicMock(),
            _database_result([EXPECTED_SCHEMA_REVISION]),
        ]
        db_engine.connect.return_value.__enter__.return_value = connection
        redis_client = MagicMock()
        redis_client.ping.side_effect = OSError("redis://synthetic-user:synthetic-secret@host")

        with patch.object(main, "engine", db_engine), patch.object(
            main, "Redis"
        ) as redis_type, self.assertLogs(main.logger, level="WARNING") as captured:
            redis_type.from_url.return_value = redis_client
            response = main.readyz(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.body,
            b'{"ok":false,"checks":{"database":"ok","database_schema":"ok","redis":"unavailable"}}',
        )
        self.assertNotIn(b"synthetic-secret", response.body)
        self.assertNotIn("synthetic-secret", " ".join(captured.output))
        self.assertIn(
            f"request_id={'b' * 32} dependency=redis error_type=OSError",
            captured.output[0],
        )
        redis_client.close.assert_called_once_with()

    def test_both_dependencies_unavailable_are_reported_without_connection_details(self) -> None:
        db_engine = MagicMock()
        db_engine.connect.side_effect = OSError("postgresql://synthetic-secret@host")
        redis_client = MagicMock()
        redis_client.ping.side_effect = OSError("redis://synthetic-secret@host")

        with patch.object(main, "engine", db_engine), patch.object(
            main, "Redis"
        ) as redis_type:
            redis_type.from_url.return_value = redis_client
            response = main.readyz(None)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.body,
            b'{"ok":false,"checks":{"database":"unavailable","redis":"unavailable"}}',
        )
        self.assertNotIn(b"synthetic-secret", response.body)
        redis_client.close.assert_called_once_with()

    def test_queue_backlog_without_worker_heartbeat_fails_readiness(self) -> None:
        db_engine = _ready_database_engine()
        redis_client = MagicMock()
        ocr_queue = SimpleNamespace(name="ocr", count=1)
        self.queue_mock.side_effect = [
            ocr_queue,
            SimpleNamespace(name="sro", count=0),
        ]
        self.worker_mock.all.return_value = []

        with patch.object(main, "engine", db_engine), patch.object(
            main, "Redis"
        ) as redis_type, self.assertLogs(main.logger, level="WARNING") as captured:
            redis_type.from_url.return_value = redis_client
            response = main.readyz(None)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["checks"]["queue_workers"], "unavailable")
        self.assertIn("dependency=queue_workers error_type=BacklogWithoutFreshHeartbeat", captured.output[0])
        self.assertNotIn("ocr", response.body.decode())
        redis_client.close.assert_called_once_with()
        self.worker_mock.all.assert_called_once_with(
            connection=redis_client,
            queue=ocr_queue,
        )

    def test_queue_backlog_with_worker_heartbeat_remains_ready(self) -> None:
        db_engine = _ready_database_engine()
        redis_client = MagicMock()
        ocr_queue = SimpleNamespace(name="ocr", count=1)
        self.queue_mock.side_effect = [ocr_queue, SimpleNamespace(name="sro", count=0)]
        self.worker_mock.all.return_value = [
            SimpleNamespace(last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=1))
        ]

        with patch.object(main, "engine", db_engine), patch.object(main, "Redis") as redis_type:
            redis_type.from_url.return_value = redis_client
            response = main.readyz(None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["checks"]["queue_workers"], "ok")

    def test_queue_backlog_with_stale_worker_heartbeat_fails_readiness(self) -> None:
        db_engine = _ready_database_engine()
        redis_client = MagicMock()
        self.queue_mock.side_effect = [
            SimpleNamespace(name="ocr", count=1),
            SimpleNamespace(name="sro", count=0),
        ]
        self.worker_mock.all.return_value = [
            SimpleNamespace(
                last_heartbeat=datetime.now(timezone.utc)
                - timedelta(seconds=DEFAULT_WORKER_TTL + 1)
            )
        ]

        with patch.object(main, "engine", db_engine), patch.object(main, "Redis") as redis_type:
            redis_type.from_url.return_value = redis_client
            response = main.readyz(None)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["checks"]["queue_workers"], "unavailable")

    def test_rq_worker_heartbeat_ttl_boundary_is_inclusive(self) -> None:
        checked_at = datetime(2026, 10, 11, tzinfo=timezone.utc)
        worker = SimpleNamespace(
            last_heartbeat=checked_at - timedelta(seconds=DEFAULT_WORKER_TTL)
        )

        self.assertTrue(main.has_fresh_worker_heartbeat(worker, checked_at))
        worker.last_heartbeat -= timedelta(microseconds=1)
        self.assertFalse(main.has_fresh_worker_heartbeat(worker, checked_at))
        worker.last_heartbeat = checked_at + timedelta(seconds=1)
        self.assertFalse(main.has_fresh_worker_heartbeat(worker, checked_at))

    def test_registered_worker_without_heartbeat_fails_readiness(self) -> None:
        db_engine = _ready_database_engine()
        redis_client = MagicMock()
        self.queue_mock.side_effect = [
            SimpleNamespace(name="ocr", count=1),
            SimpleNamespace(name="sro", count=0),
        ]
        self.worker_mock.all.return_value = [SimpleNamespace(last_heartbeat=None)]

        with patch.object(main, "engine", db_engine), patch.object(main, "Redis") as redis_type:
            redis_type.from_url.return_value = redis_client
            response = main.readyz(None)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["checks"]["queue_workers"], "unavailable")

    def test_queue_diagnostic_failure_is_bounded_and_does_not_masquerade_as_redis_failure(self) -> None:
        db_engine = _ready_database_engine()
        redis_client = MagicMock()
        self.queue_mock.side_effect = [
            SimpleNamespace(name="ocr", count=1),
            SimpleNamespace(name="sro", count=0),
        ]
        self.worker_mock.all.side_effect = RuntimeError("synthetic private RQ payload")

        with patch.object(main, "engine", db_engine), patch.object(
            main, "Redis"
        ) as redis_type, self.assertLogs(main.logger, level="WARNING") as captured:
            redis_type.from_url.return_value = redis_client
            response = main.readyz(None)

        checks = json.loads(response.body)["checks"]
        self.assertEqual(response.status_code, 503)
        self.assertEqual(checks["redis"], "ok")
        self.assertEqual(checks["queue_workers"], "unavailable")
        self.assertNotIn("synthetic private RQ payload", " ".join(captured.output))
        redis_client.close.assert_called_once_with()

    def test_redis_connection_timeout_configuration_is_bounded(self) -> None:
        db_engine = _ready_database_engine()
        redis_client = MagicMock()

        with patch.object(main, "engine", db_engine), patch.object(
            main, "Redis"
        ) as redis_type:
            redis_type.from_url.return_value = redis_client
            main.readyz(None)

        redis_type.from_url.assert_called_once_with(
            main.settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )


if __name__ == "__main__":
    unittest.main()
