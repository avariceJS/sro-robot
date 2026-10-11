from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from app.observability import SafeAccessLogMiddleware, access_logger


class SafeAccessLogMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, app, scope):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        return messages

    @staticmethod
    def _scope(path: str, query: bytes = b""):
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "headers": [],
            "state": {"request_id": "e" * 32},
            "client": ("192.0.2.18", 53124),
            "server": ("127.0.0.1", 8000),
        }

    async def test_logs_route_template_and_outcome_without_path_query_or_client(self):
        async def endpoint(scope, receive, send):
            scope["route"] = SimpleNamespace(path="/tasks/{task_id}")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        scope = self._scope("/tasks/741923", b"token=synthetic-secret")
        with self.assertLogs(access_logger, level="INFO") as captured:
            messages = await self._run(SafeAccessLogMiddleware(endpoint), scope)

        self.assertEqual(messages[0]["status"], 200)
        self.assertIn("request_id=" + "e" * 32, captured.output[0])
        self.assertIn("route=/tasks/{task_id} status=200", captured.output[0])
        self.assertRegex(captured.output[0], r"duration_ms=\d+\.\d")
        for private_value in ("741923", "synthetic-secret", "192.0.2.18", "/tasks/741923"):
            self.assertNotIn(private_value, captured.output[0])

    async def test_unmatched_and_application_failure_log_safe_500(self):
        async def failing_endpoint(scope, receive, send):
            raise RuntimeError("synthetic-private-failure")

        scope = self._scope("/lookup/synthetic-person@example.invalid")
        with self.assertLogs(access_logger, level="INFO") as captured:
            with self.assertRaisesRegex(RuntimeError, "synthetic-private-failure"):
                await self._run(SafeAccessLogMiddleware(failing_endpoint), scope)

        self.assertIn("route=<unmatched> status=500", captured.output[0])
        self.assertNotIn("synthetic-person@example.invalid", captured.output[0])
        self.assertNotIn("synthetic-private-failure", captured.output[0])

    async def test_downstream_disconnect_does_not_masquerade_as_http_500(self):
        async def endpoint(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def failing_send(message):
            raise OSError("client disconnected")

        scope = self._scope("/healthz")
        with self.assertLogs(access_logger, level="INFO") as captured:
            with self.assertRaises(OSError):
                await SafeAccessLogMiddleware(endpoint)(scope, receive, failing_send)

        self.assertIn("status=unknown", captured.output[0])
        self.assertNotIn("client disconnected", captured.output[0])


class SafeAccessLogLaunchContractTests(unittest.TestCase):
    def test_development_launcher_disables_uvicorn_raw_access_paths(self):
        root = Path(__file__).resolve().parents[1]
        launcher = (root / "scripts/dev/run-web.sh").read_text()
        readme = (root / "README.md").read_text()

        self.assertIn("--no-access-log", launcher)
        self.assertIn("bash scripts/dev/run-web.sh", readme)
