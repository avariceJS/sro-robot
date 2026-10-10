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


if __name__ == "__main__":
    unittest.main()
