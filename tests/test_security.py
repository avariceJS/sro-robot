from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.security import (
    LoginRequestLimitMiddleware,
    LOGIN_REQUEST_BODY_LIMIT_BYTES,
    RequestIdMiddleware,
    SameOriginMutationMiddleware,
    SecurityHeadersMiddleware,
    UploadAdmissionMiddleware,
    _same_host_origin,
)
from app.api import routes_auth
from starlette.requests import Request
from starlette.middleware.sessions import SessionMiddleware


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
            [(b"host", b"robot.example"), (b"host", b"attacker.example"), (b"origin", b"https://robot.example")],
            [(b"host", b"robot.example"), (b"origin", b"https://attacker.example"), (b"origin", b"https://robot.example")],
            [(b"host", b"robot.example"), (b"referer", b"https://attacker.example/"), (b"referer", b"https://robot.example/")],
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
            "session": {
                "pre_authentication_marker": "discard-me",
                routes_auth.SESSION_USER_KEY: 72,
                routes_auth.SESSION_TOKEN_KEY: "p" * 43,
            },
        }
        request = Request(scope)
        user = SimpleNamespace(id=73)
        previous_user = SimpleNamespace(id=72, is_active=True)
        db = MagicMock()

        with (
            patch.object(routes_auth.auth, "authenticate", return_value=user),
            patch.object(routes_auth.auth, "revoke_login_session", return_value=True) as revoke,
            patch.object(routes_auth.auth, "get_user", return_value=previous_user),
        ):
            response = routes_auth.login_submit(
                request,
                "synthetic-user",
                "synthetic-password",
                db,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(request.session[routes_auth.SESSION_USER_KEY], 73)
        token = request.session[routes_auth.SESSION_TOKEN_KEY]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(db.add.call_count, 3)
        added_rows = [call.args[0] for call in db.add.call_args_list]
        session_row = next(row for row in added_rows if hasattr(row, "token_sha256"))
        ended_event, started_event = [row for row in added_rows if hasattr(row, "kind")]
        self.assertEqual(session_row.user_id, 73)
        self.assertNotEqual(session_row.token_sha256, token)
        self.assertEqual(ended_event.kind, "session_ended")
        self.assertEqual(ended_event.user_id, 72)
        self.assertEqual(started_event.kind, "session_started")
        self.assertEqual(started_event.user_id, 73)
        revoke.assert_called_once_with(db, 72, "p" * 43)
        db.commit.assert_called_once_with()

    def test_login_commit_failure_does_not_issue_a_new_cookie_session(self) -> None:
        prior_session = {
            routes_auth.SESSION_USER_KEY: 72,
            routes_auth.SESSION_TOKEN_KEY: "p" * 43,
        }
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
            "session": dict(prior_session),
        }
        request = Request(scope)
        db = MagicMock()
        db.commit.side_effect = RuntimeError("synthetic commit failure")

        with (
            patch.object(routes_auth.auth, "authenticate", return_value=SimpleNamespace(id=73)),
            patch.object(routes_auth.auth, "revoke_login_session", return_value=False),
            self.assertRaisesRegex(RuntimeError, "synthetic commit failure"),
        ):
            routes_auth.login_submit(request, "synthetic-user", "synthetic-password", db)

        self.assertEqual(request.session, prior_session)

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

    async def test_security_headers_are_unique_and_override_downstream_values(self) -> None:
        messages = []

        async def downstream(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"x-frame-options", b"SAMEORIGIN"),
                    (b"content-type", b"text/html"),
                    (b"x-frame-options", b"ALLOWALL"),
                ],
            })
            await send({"type": "http.response.body", "body": b"ok"})

        async def send(message):
            messages.append(message)

        await SecurityHeadersMiddleware(downstream)(
            {"type": "http", "method": "GET", "path": "/"},
            lambda: None,
            send,
        )
        start = next(message for message in messages if message["type"] == "http.response.start")
        response_headers = start["headers"]
        for header, expected in (
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
            (
                b"content-security-policy",
                b"default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
                b"form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                b"img-src 'self'; font-src 'self'; connect-src 'self'",
            ),
            (b"cache-control", b"no-store"),
        ):
            with self.subTest(header=header):
                values = [value for key, value in response_headers if key.lower() == header]
                self.assertEqual(values, [expected])
        self.assertIn((b"content-type", b"text/html"), response_headers)

    async def test_static_assets_keep_cache_policy_while_receiving_security_headers(self) -> None:
        messages = []

        async def downstream(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"cache-control", b"public, max-age=3600")],
            })
            await send({"type": "http.response.body", "body": b"asset"})

        async def send(message):
            messages.append(message)

        await SecurityHeadersMiddleware(downstream)(
            {"type": "http", "method": "GET", "path": "/static/htmx.min.js"},
            lambda: None,
            send,
        )
        start = next(message for message in messages if message["type"] == "http.response.start")
        cache_values = [
            value for key, value in start["headers"] if key.lower() == b"cache-control"
        ]
        self.assertEqual(cache_values, [b"public, max-age=3600"])
        self.assertTrue(
            any(key.lower() == b"content-security-policy" for key, _value in start["headers"])
        )

    async def test_security_headers_cover_origin_rejection_response(self) -> None:
        messages = []

        async def downstream(scope, receive, send):
            raise AssertionError("cross-origin mutation must be rejected")

        async def send(message):
            messages.append(message)

        app = SecurityHeadersMiddleware(SameOriginMutationMiddleware(downstream))
        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/mutate",
                "headers": [(b"host", b"robot.example")],
            },
            lambda: None,
            send,
        )
        start = next(message for message in messages if message["type"] == "http.response.start")
        self.assertEqual(start["status"], 403)
        self.assertIn((b"x-content-type-options", b"nosniff"), start["headers"])
        self.assertIn((b"x-frame-options", b"DENY"), start["headers"])
        self.assertTrue(
            any(key.lower() == b"content-security-policy" for key, _value in start["headers"])
        )

    async def test_rotating_session_secret_invalidates_cookies_signed_by_previous_key(self) -> None:
        messages = []

        async def create_session(scope, receive, send):
            scope["session"]["user_id"] = 17
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def collect(message):
            messages.append(message)

        request_scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("robot.example", 443),
        }
        await SessionMiddleware(create_session, secret_key="old synthetic key")(request_scope, None, collect)
        response = next(message for message in messages if message["type"] == "http.response.start")
        cookie_header = next(value for key, value in response["headers"] if key.lower() == b"set-cookie")
        cookie = cookie_header.split(b";", 1)[0]
        observed_session = []

        async def inspect_session(scope, receive, send):
            observed_session.append(scope["session"].get("user_id"))
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        changed_key_scope = {**request_scope, "headers": [(b"cookie", cookie)]}
        await SessionMiddleware(inspect_session, secret_key="new synthetic key")(
            changed_key_scope, None, collect
        )

        self.assertEqual(observed_session, [None])


class LoginRequestLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_application_checks_origin_before_login_limit_middleware(self) -> None:
        from app.main import app

        middleware_order = [middleware.cls for middleware in app.user_middleware]
        self.assertLess(
            middleware_order.index(SameOriginMutationMiddleware),
            middleware_order.index(LoginRequestLimitMiddleware),
        )
        self.assertLess(
            middleware_order.index(RequestIdMiddleware),
            middleware_order.index(LoginRequestLimitMiddleware),
        )
        self.assertLess(
            middleware_order.index(SecurityHeadersMiddleware),
            middleware_order.index(LoginRequestLimitMiddleware),
        )

    async def test_application_413_keeps_request_id_and_security_headers(self) -> None:
        from app.main import app

        messages = []

        async def receive():
            raise AssertionError("declared oversized body must be rejected before reading")

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/login",
            "raw_path": b"/login",
            "query_string": b"",
            "headers": [
                (b"host", b"robot.example"),
                (b"origin", b"http://robot.example"),
                (b"content-length", b"16385"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("robot.example", 80),
        }
        await app.build_middleware_stack()(scope, receive, send)

        start = next(message for message in messages if message["type"] == "http.response.start")
        headers = {key.lower(): value for key, value in start["headers"]}
        self.assertEqual(start["status"], 413)
        self.assertRegex(headers[b"x-request-id"].decode(), r"^[0-9a-f]{32}$")
        self.assertEqual(headers[b"x-frame-options"], b"DENY")
        self.assertEqual(headers[b"x-content-type-options"], b"nosniff")
        self.assertEqual(headers[b"cache-control"], b"no-store")

    async def _request(
        self,
        *,
        path: str = "/login",
        method: str = "POST",
        headers: list[tuple[bytes, bytes]] | None = None,
        chunks: list[tuple[bytes, bool]] | None = None,
        limit: int = LOGIN_REQUEST_BODY_LIMIT_BYTES,
        body_timeout_seconds: float = 30,
    ):
        called = False
        messages = []
        incoming = iter(chunks or [(b"", False)])

        async def downstream(scope, receive, send):
            nonlocal called
            called = True
            bodies = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    break
                bodies.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": bytes(bodies)})

        async def receive():
            body, more_body = next(incoming)
            return {"type": "http.request", "body": body, "more_body": more_body}

        async def send(message):
            messages.append(message)

        await LoginRequestLimitMiddleware(
            downstream,
            max_body_bytes=limit,
            body_read_timeout_seconds=body_timeout_seconds,
        )(
            {"type": "http", "method": method, "path": path, "headers": headers or []},
            receive,
            send,
        )
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = next(message for message in messages if message["type"] == "http.response.body")
        return called, start["status"], body["body"]

    async def test_login_body_at_limit_is_replayed_without_corruption(self) -> None:
        body = b"x" * LOGIN_REQUEST_BODY_LIMIT_BYTES
        called, status, returned = await self._request(
            headers=[(b"content-length", str(len(body)).encode())],
            chunks=[(body[:7000], True), (body[7000:], False)],
        )
        self.assertTrue(called)
        self.assertEqual(status, 204)
        self.assertEqual(returned, body)

    async def test_login_concurrency_is_bounded_and_overload_can_retry(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        first_messages = []
        second_messages = []
        called_count = 0

        async def downstream(_scope, receive, send):
            nonlocal called_count
            called_count += 1
            await receive()
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b"login", "more_body": False}

        async def first_send(message):
            first_messages.append(message)

        async def second_send(message):
            second_messages.append(message)

        limiter = LoginRequestLimitMiddleware(
            downstream,
            max_concurrent_requests=1,
            concurrency_wait_seconds=0.01,
        )
        scope = {"type": "http", "method": "POST", "path": "/login", "headers": []}
        first = asyncio.create_task(limiter(scope, receive, first_send))
        await entered.wait()

        async def unreadable_receive():
            raise AssertionError("overloaded login request must be rejected before reading body")

        await limiter(scope, unreadable_receive, second_send)
        second_start = next(
            message for message in second_messages if message["type"] == "http.response.start"
        )
        self.assertEqual(second_start["status"], 503)
        self.assertIn((b"retry-after", b"1"), second_start["headers"])
        self.assertEqual(called_count, 1)

        release.set()
        await first
        first_start = next(
            message for message in first_messages if message["type"] == "http.response.start"
        )
        self.assertEqual(first_start["status"], 204)

    async def test_login_concurrency_setting_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "login request limits"):
            LoginRequestLimitMiddleware(lambda *_args: None, max_concurrent_requests=0)

    async def test_login_rejects_over_limit_chunked_body_before_route(self) -> None:
        called, status, body = await self._request(
            limit=10,
            chunks=[(b"123456", True), (b"78901", False)],
        )
        self.assertFalse(called)
        self.assertEqual(status, 413)
        self.assertIn("большой".encode(), body)

    async def test_login_body_read_timeout_returns_408_and_releases_capacity(self) -> None:
        messages = []
        downstream_calls = 0

        async def downstream(_scope, _receive, send):
            nonlocal downstream_calls
            downstream_calls += 1
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def hanging_receive():
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        limiter = LoginRequestLimitMiddleware(
            downstream,
            max_concurrent_requests=1,
            body_read_timeout_seconds=0.01,
        )
        scope = {"type": "http", "method": "POST", "path": "/login", "headers": []}
        await limiter(scope, hanging_receive, send)

        start = next(message for message in messages if message["type"] == "http.response.start")
        self.assertEqual(start["status"], 408)
        self.assertEqual(downstream_calls, 0)

        async def normal_receive():
            return {"type": "http.request", "body": b"login", "more_body": False}

        await limiter(scope, normal_receive, send)
        self.assertEqual(downstream_calls, 1)
        retry_start = [
            message for message in messages if message["type"] == "http.response.start"
        ][-1]
        self.assertEqual(retry_start["status"], 204)

    async def test_login_body_timeout_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "login request limits"):
            LoginRequestLimitMiddleware(lambda *_args: None, body_read_timeout_seconds=0)

    async def test_login_rejects_oversized_content_length_without_reading_body(self) -> None:
        called, status, _body = await self._request(
            headers=[(b"content-length", b"00016385")],
            chunks=[],
        )
        self.assertFalse(called)
        self.assertEqual(status, 413)

    async def test_login_rejects_extremely_long_content_length_without_integer_parsing(self) -> None:
        called, status, _body = await self._request(
            headers=[(b"content-length", b"9" * 5000)],
            chunks=[],
        )
        self.assertFalse(called)
        self.assertEqual(status, 413)

    async def test_login_rejects_invalid_or_duplicate_content_length(self) -> None:
        for headers in (
            [(b"content-length", b"many")],
            [(b"content-length", b"2"), (b"content-length", b"2")],
            [(b"content-length", b"-1")],
            [(b"content-length", b"+2")],
            [(b"content-length", b" 2")],
        ):
            with self.subTest(headers=headers):
                called, status, _body = await self._request(headers=headers)
                self.assertFalse(called)
                self.assertEqual(status, 400)

    async def test_other_paths_and_methods_are_not_buffered(self) -> None:
        called, status, _body = await self._request(
            path="/upload",
            headers=[(b"content-length", b"16385")],
            chunks=[],
        )
        self.assertTrue(called)
        self.assertEqual(status, 204)

    async def test_same_origin_rejection_happens_before_login_body_is_read(self) -> None:
        called = False
        messages = []

        async def downstream(scope, receive, send):
            nonlocal called
            called = True
            raise AssertionError("invalid origin must be rejected before the route")

        async def receive():
            raise AssertionError("invalid origin must be rejected before reading the body")

        async def send(message):
            messages.append(message)

        app = SameOriginMutationMiddleware(LoginRequestLimitMiddleware(downstream))
        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/login",
                "headers": [
                    (b"host", b"robot.example"),
                    (b"origin", b"https://attacker.example"),
                    (b"content-length", b"16385"),
                ],
            },
            receive,
            send,
        )

        start = next(message for message in messages if message["type"] == "http.response.start")
        self.assertFalse(called)
        self.assertEqual(start["status"], 403)


class UploadAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def _request(
        self,
        headers=None,
        chunks=None,
        path="/upload",
        method="POST",
        limit=10,
        body_timeout_seconds=300,
    ):
        called = False
        messages = []
        incoming = iter(chunks or [(b"", False)])

        async def downstream(scope, receive, send):
            nonlocal called
            called = True
            while True:
                message = await receive()
                if message["type"] != "http.request" or not message.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            body, more_body = next(incoming)
            return {"type": "http.request", "body": body, "more_body": more_body}

        async def send(message):
            messages.append(message)

        await UploadAdmissionMiddleware(
            downstream, limit, 1, body_read_timeout_seconds=body_timeout_seconds
        )(
            {"type": "http", "method": method, "path": path, "headers": headers or []},
            receive,
            send,
        )
        start = next(message for message in messages if message["type"] == "http.response.start")
        return called, start["status"]

    async def test_rejects_oversized_declared_body_without_reading(self) -> None:
        called, status = await self._request(
            headers=[(b"content-length", b"11")],
        )
        self.assertFalse(called)
        self.assertEqual(status, 413)

    async def test_rejects_chunked_body_after_crossing_budget(self) -> None:
        called, status = await self._request(chunks=[(b"123456", True), (b"78901", False)])
        self.assertTrue(called)
        self.assertEqual(status, 413)

    async def test_stalled_upload_body_times_out_and_releases_admission_capacity(self) -> None:
        messages = []
        downstream_calls = 0

        async def downstream(_scope, receive, send):
            nonlocal downstream_calls
            downstream_calls += 1
            await receive()
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def send(message):
            messages.append(message)

        limiter = UploadAdmissionMiddleware(
            downstream,
            max_request_bytes=10,
            max_concurrent_requests=1,
            body_read_timeout_seconds=0.01,
        )
        scope = {"type": "http", "method": "POST", "path": "/upload", "headers": []}

        async def stalled_receive():
            await asyncio.Event().wait()

        await limiter(scope, stalled_receive, send)
        first_start = next(message for message in messages if message["type"] == "http.response.start")
        self.assertEqual(first_start["status"], 408)
        self.assertEqual(downstream_calls, 1)

        async def complete_receive():
            return {"type": "http.request", "body": b"ok", "more_body": False}

        await limiter(scope, complete_receive, send)
        retry_start = [
            message for message in messages if message["type"] == "http.response.start"
        ][-1]
        self.assertEqual(retry_start["status"], 204)
        self.assertEqual(downstream_calls, 2)

    async def test_upload_timeout_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "upload admission limits"):
            UploadAdmissionMiddleware(lambda *_args: None, 10, 1, body_read_timeout_seconds=0)

    async def test_rejects_invalid_or_duplicate_content_length(self) -> None:
        for headers in (
            [(b"content-length", b"many")],
            [(b"content-length", b"1"), (b"content-length", b"1")],
        ):
            with self.subTest(headers=headers):
                called, status = await self._request(headers=headers)
                self.assertFalse(called)
                self.assertEqual(status, 400)

    async def test_does_not_apply_to_other_routes_or_methods(self) -> None:
        for path, method in (("/login", "POST"), ("/upload", "GET")):
            with self.subTest(path=path, method=method):
                called, status = await self._request(
                    path=path,
                    method=method,
                    headers=[(b"content-length", b"11")],
                )
                self.assertTrue(called)
                self.assertEqual(status, 204)


if __name__ == "__main__":
    unittest.main()
