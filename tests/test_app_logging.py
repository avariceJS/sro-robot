"""Tests that uncaught HTTP failures produce diagnostics, not traceback pages."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from starlette.requests import Request

from app import main
from app.main import logger, unexpected_error


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
        self.assertIn("method=POST path=/tasks/93/run", captured.output[0])
        self.assertIn("error_type=RuntimeError", captured.output[0])
        self.assertNotIn("Traceback", captured.output[0])
        self.assertNotIn("synthetic-sensitive-value", captured.output[0])
        self.assertNotIn("synthetic internal failure", response.body.decode())


class ReadinessCheckTests(unittest.TestCase):
    def test_ready_when_database_and_redis_respond(self) -> None:
        db_connection = MagicMock()
        db_engine = MagicMock()
        db_engine.connect.return_value.__enter__.return_value = db_connection
        redis_client = MagicMock()

        with patch.object(main, "engine", db_engine), patch.object(
            main, "Redis"
        ) as redis_type:
            redis_type.from_url.return_value = redis_client
            response = main.readyz()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.body,
            b'{"ok":true,"checks":{"database":"ok","redis":"ok"}}',
        )
        db_connection.execute.assert_called_once()
        redis_client.ping.assert_called_once_with()
        redis_client.close.assert_called_once_with()

    def test_lease_enabled_readiness_requires_the_lease_schema(self) -> None:
        db_connection = MagicMock()
        db_engine = MagicMock()
        db_engine.connect.return_value.__enter__.return_value = db_connection
        db_connection.execute.return_value.scalars.return_value.all.return_value = [
            "task_id",
            "owner_token",
            "job_kind",
            "attempt",
            "generation",
            "lease_until",
            "updated_at",
        ]
        redis_client = MagicMock()

        with patch.object(main, "settings") as settings, patch.object(
            main, "engine", db_engine
        ), patch.object(main, "Redis") as redis_type:
            settings.ocr_task_leases_enabled = True
            settings.redis_url = "redis://synthetic"
            redis_type.from_url.return_value = redis_client
            response = main.readyz()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.body,
            b'{"ok":true,"checks":{"database":"ok","ocr_lease_schema":"ok","redis":"ok"}}',
        )
        self.assertEqual(db_connection.execute.call_count, 2)

    def test_lease_enabled_readiness_fails_closed_when_schema_is_incomplete(self) -> None:
        db_connection = MagicMock()
        db_engine = MagicMock()
        db_engine.connect.return_value.__enter__.return_value = db_connection
        db_connection.execute.return_value.scalars.return_value.all.return_value = [
            "task_id",
            "owner_token",
        ]
        redis_client = MagicMock()

        with patch.object(main, "settings") as settings, patch.object(
            main, "engine", db_engine
        ), patch.object(main, "Redis") as redis_type:
            settings.ocr_task_leases_enabled = True
            settings.redis_url = "redis://synthetic"
            redis_type.from_url.return_value = redis_client
            response = main.readyz()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.body,
            b'{"ok":false,"checks":{"database":"ok","ocr_lease_schema":"unavailable","redis":"ok"}}',
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
            response = main.readyz()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.body,
            b'{"ok":false,"checks":{"database":"unavailable","redis":"ok"}}',
        )
        self.assertNotIn(b"secret-host", response.body)

    def test_redis_ping_failure_returns_503_logs_only_exception_type_and_closes_client(self) -> None:
        db_engine = MagicMock()
        db_engine.connect.return_value.__enter__.return_value = MagicMock()
        redis_client = MagicMock()
        redis_client.ping.side_effect = OSError("redis://synthetic-user:synthetic-secret@host")

        with patch.object(main, "engine", db_engine), patch.object(
            main, "Redis"
        ) as redis_type, self.assertLogs(main.logger, level="WARNING") as captured:
            redis_type.from_url.return_value = redis_client
            response = main.readyz()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.body,
            b'{"ok":false,"checks":{"database":"ok","redis":"unavailable"}}',
        )
        self.assertNotIn(b"synthetic-secret", response.body)
        self.assertNotIn("synthetic-secret", " ".join(captured.output))
        self.assertIn("dependency=redis error_type=OSError", captured.output[0])
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
            response = main.readyz()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.body,
            b'{"ok":false,"checks":{"database":"unavailable","redis":"unavailable"}}',
        )
        self.assertNotIn(b"synthetic-secret", response.body)
        redis_client.close.assert_called_once_with()

    def test_redis_connection_timeout_configuration_is_bounded(self) -> None:
        db_engine = MagicMock()
        db_engine.connect.return_value.__enter__.return_value = MagicMock()
        redis_client = MagicMock()

        with patch.object(main, "engine", db_engine), patch.object(
            main, "Redis"
        ) as redis_type:
            redis_type.from_url.return_value = redis_client
            main.readyz()

        redis_type.from_url.assert_called_once_with(
            main.settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )


if __name__ == "__main__":
    unittest.main()
