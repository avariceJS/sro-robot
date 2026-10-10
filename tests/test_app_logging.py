"""Tests that uncaught HTTP failures produce diagnostics, not traceback pages."""

from __future__ import annotations

import unittest

from starlette.requests import Request

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
                raise RuntimeError("synthetic internal failure")
            except RuntimeError as error:
                response = await unexpected_error(request, error)

        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.body.decode(), "Внутренняя ошибка сервера")
        self.assertIn("method=POST path=/tasks/93/run", captured.output[0])
        self.assertIn("Traceback", captured.output[0])
        self.assertNotIn("synthetic internal failure", response.body.decode())


if __name__ == "__main__":
    unittest.main()
