from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.security import RequestIdMiddleware, SameOriginMutationMiddleware, _same_host_origin
from app.api import routes_auth
from starlette.requests import Request


class SameOriginMutationTests(unittest.IsolatedAsyncioTestCase):
    async def _request(self, method: str, headers: list[tuple[bytes, bytes]]):
        called = False
        messages = []

        async def downstream(scope, receive, send):
            nonlocal called
            called = True
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            raise AssertionError("middleware must not consume the request body")

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": method,
            "path": "/upload",
            "headers": headers,
        }
        await SameOriginMutationMiddleware(downstream)(scope, receive, send)
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = next(message for message in messages if message["type"] == "http.response.body")
        return called, start["status"], body["body"]

    async def test_rejects_cross_origin_mutation_before_application_or_body_read(self) -> None:
        called, status, body = await self._request(
            "POST",
            [(b"host", b"robot.example"), (b"origin", b"https://attacker.example")],
        )
        self.assertFalse(called)
        self.assertEqual(status, 403)
        self.assertIn("источник".encode(), body)

    async def test_allows_same_origin_and_referer_fallback_without_reading_body(self) -> None:
        for headers in (
            [(b"host", b"robot.example"), (b"origin", b"https://robot.example")],
            [(b"host", b"robot.example"), (b"referer", b"http://robot.example/login")],
        ):
            with self.subTest(headers=headers):
                called, status, _ = await self._request("POST", headers)
                self.assertTrue(called)
                self.assertEqual(status, 204)

    async def test_safe_methods_do_not_require_browser_origin_headers(self) -> None:
        called, status, _ = await self._request("GET", [])
        self.assertTrue(called)
        self.assertEqual(status, 204)

    def test_rejects_missing_malformed_and_ambiguous_origin_headers(self) -> None:
        for headers in (
            [(b"host", b"robot.example")],
            [(b"host", b"robot.example"), (b"origin", b"null")],
            [(b"host", b"robot.example"), (b"origin", b"https://user@robot.example")],
            [(b"host", b"robot.example"), (b"origin", b"https://[")],
            [(b"host", b"robot.example, attacker.example"), (b"origin", b"https://robot.example")],
            [(b"host", b"robot.example"), (b"referer", b"http://[")],
        ):
            with self.subTest(headers=headers):
                self.assertFalse(_same_host_origin(headers))

    def test_successful_login_replaces_pre_authentication_session(self) -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/login",
            "raw_path": b"/login",
            "query_string": b"",
            "headers": [(b"host", b"robot.example")],
            "client": ("127.0.0.1", 12345),
            "server": ("robot.example", 80),
            "session": {"pre_authentication_marker": "discard-me"},
        }
        request = Request(scope)
        user = SimpleNamespace(id=73)

        with patch.object(routes_auth.auth, "authenticate", return_value=user):
            response = routes_auth.login_submit(request, "synthetic-user", "synthetic-password", object())

        self.assertEqual(response.status_code, 303)
        self.assertEqual(request.session, {routes_auth.SESSION_USER_KEY: 73})

    async def test_request_id_is_generated_and_returned_without_reading_body(self) -> None:
        messages = []

        async def downstream(scope, receive, send):
            self.assertRegex(scope["state"]["request_id"], r"^[0-9a-f]{32}$")
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            raise AssertionError("request ID middleware must not consume the body")

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "POST",
            "path": "/upload",
            "headers": [(b"x-request-id", b"attacker-controlled")],
        }
        await RequestIdMiddleware(downstream)(scope, receive, send)

        start = next(message for message in messages if message["type"] == "http.response.start")
        returned_ids = [value for key, value in start["headers"] if key.lower() == b"x-request-id"]
        self.assertEqual(len(returned_ids), 1)
        self.assertEqual(returned_ids[0].decode(), scope["state"]["request_id"])
        self.assertNotEqual(returned_ids[0], b"attacker-controlled")


if __name__ == "__main__":
    unittest.main()
