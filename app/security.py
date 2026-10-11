"""Small browser request protections for the cookie-authenticated HTML app."""

from __future__ import annotations

import asyncio
import secrets
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
REQUEST_ID_HEADER = b"x-request-id"
_SECURITY_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"no-referrer",
    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
    b"content-security-policy": (
        b"default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        b"form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        b"img-src 'self'; font-src 'self'; connect-src 'self'"
    ),
}
_PRIVATE_CACHE_HEADER = (b"cache-control", b"no-store")
LOGIN_REQUEST_BODY_LIMIT_BYTES = 16 * 1024
LOGIN_BODY_READ_TIMEOUT_SECONDS = 30
LOGIN_MAX_CONCURRENT_REQUESTS = 2
LOGIN_CONCURRENCY_WAIT_SECONDS = 0.25


class LoginRequestLimitMiddleware:
    """Bound buffered form input on the credential endpoint only."""

    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int = LOGIN_REQUEST_BODY_LIMIT_BYTES,
        body_read_timeout_seconds: float = LOGIN_BODY_READ_TIMEOUT_SECONDS,
        max_concurrent_requests: int = LOGIN_MAX_CONCURRENT_REQUESTS,
        concurrency_wait_seconds: float = LOGIN_CONCURRENCY_WAIT_SECONDS,
    ) -> None:
        if (
            max_body_bytes < 1
            or body_read_timeout_seconds <= 0
            or max_concurrent_requests < 1
            or concurrency_wait_seconds <= 0
        ):
            raise ValueError("login request limits must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.body_read_timeout_seconds = body_read_timeout_seconds
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)
        self.concurrency_wait_seconds = concurrency_wait_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "").upper() != "POST"
            or scope.get("path") != "/login"
        ):
            await self.app(scope, receive, send)
            return

        try:
            await asyncio.wait_for(
                self.semaphore.acquire(), timeout=self.concurrency_wait_seconds
            )
        except TimeoutError:
            response = PlainTextResponse(
                "Слишком много запросов на вход",
                status_code=503,
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return

        try:
            await self._forward_limited(scope, receive, send)
        finally:
            self.semaphore.release()

    async def _forward_limited(self, scope: Scope, receive: Receive, send: Send) -> None:
        content_lengths = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            response = PlainTextResponse("Некорректный запрос", status_code=400)
            await response(scope, receive, send)
            return
        if content_lengths:
            raw_length = content_lengths[0]
            if not raw_length or not raw_length.isdigit():
                response = PlainTextResponse("Некорректный запрос", status_code=400)
                await response(scope, receive, send)
                return
            normalized_length = raw_length.lstrip(b"0") or b"0"
            limit_text = str(self.max_body_bytes).encode("ascii")
            exceeds_limit = len(normalized_length) > len(limit_text) or (
                len(normalized_length) == len(limit_text) and normalized_length > limit_text
            )
            if exceeds_limit:
                response = PlainTextResponse("Слишком большой запрос", status_code=413)
                await response(scope, receive, send)
                return

        buffered_messages = []
        total_bytes = 0
        try:
            async with asyncio.timeout(self.body_read_timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        buffered_messages.append(message)
                        break
                    if message["type"] != "http.request":
                        buffered_messages.append(message)
                        continue
                    body_length = len(message.get("body", b""))
                    if total_bytes + body_length > self.max_body_bytes:
                        del message
                        response = PlainTextResponse("Слишком большой запрос", status_code=413)
                        await response(scope, receive, send)
                        return
                    total_bytes += body_length
                    buffered_messages.append(message)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            response = PlainTextResponse("Время ожидания запроса истекло", status_code=408)
            await response(scope, receive, send)
            return

        position = 0

        async def replay_receive():
            nonlocal position
            if position < len(buffered_messages):
                message = buffered_messages[position]
                position += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)


class UploadAdmissionMiddleware:
    """Bound request bytes and concurrent multipart parsing for document intake."""

    def __init__(
        self,
        app: ASGIApp,
        max_request_bytes: int,
        max_concurrent_requests: int,
    ) -> None:
        if max_request_bytes < 1 or max_concurrent_requests < 1:
            raise ValueError("upload admission limits must be positive")
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "").upper() != "POST"
            or scope.get("path") != "/upload"
        ):
            await self.app(scope, receive, send)
            return

        content_lengths = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        if len(content_lengths) > 1 or (
            content_lengths
            and (
                not content_lengths[0]
                or not content_lengths[0].isdigit()
            )
        ):
            await PlainTextResponse("Некорректный запрос", status_code=400)(scope, receive, send)
            return
        if content_lengths and _decimal_exceeds(content_lengths[0], self.max_request_bytes):
            await PlainTextResponse("Слишком большой запрос", status_code=413)(scope, receive, send)
            return

        async with self.semaphore:
            total_bytes = 0
            response_started = False

            async def counted_receive():
                nonlocal total_bytes
                message = await receive()
                if message["type"] == "http.request":
                    total_bytes += len(message.get("body", b""))
                    if total_bytes > self.max_request_bytes:
                        raise _UploadBodyTooLarge
                return message

            async def tracked_send(message):
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                await send(message)

            try:
                await self.app(scope, counted_receive, tracked_send)
            except _UploadBodyTooLarge:
                if response_started:
                    raise
                await PlainTextResponse("Слишком большой запрос", status_code=413)(
                    scope, receive, send
                )


class _UploadBodyTooLarge(Exception):
    """Internal signal used to stop multipart parsing once the byte budget is exceeded."""


def _decimal_exceeds(value: bytes, limit: int) -> bool:
    normalized = value.lstrip(b"0") or b"0"
    limit_text = str(limit).encode("ascii")
    return len(normalized) > len(limit_text) or (
        len(normalized) == len(limit_text) and normalized > limit_text
    )


class SecurityHeadersMiddleware:
    """Apply browser protections consistently to success and error responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                is_static_asset = scope.get("path", "").startswith("/static/")
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in _SECURITY_HEADERS
                    and (is_static_asset or key.lower() != _PRIVATE_CACHE_HEADER[0])
                ]
                headers.extend(_SECURITY_HEADERS.items())
                if not is_static_asset:
                    headers.append(_PRIVATE_CACHE_HEADER)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


class RequestIdMiddleware:
    """Attach an untrusted-input-independent correlation ID to each HTTP response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = secrets.token_hex(16)
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != REQUEST_ID_HEADER
                ]
                headers.append((REQUEST_ID_HEADER, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_request_id)


def _same_host_origin(headers: list[tuple[bytes, bytes]]) -> bool:
    values: dict[bytes, str] = {}
    for key, value in headers:
        key = key.lower()
        if key not in {b"host", b"origin", b"referer"}:
            continue
        # These fields are security inputs; do not let proxy/parser differences
        # choose which duplicate value defines the request's origin.
        if key in values:
            return False
        values[key] = value.decode("latin-1")
    host = values.get(b"host", "").strip().casefold()
    if not host or "," in host:
        return False

    origin = values.get(b"origin")
    if origin is not None:
        if origin != origin.strip():
            return False
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
            and parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and not parsed.query
            and not parsed.fragment
            and parsed.netloc.casefold() == host
        )

    referer = values.get(b"referer")
    if referer is None:
        return False
    try:
        parsed = urlsplit(referer)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and parsed.netloc.casefold() == host
    )


class SameOriginMutationMiddleware:
    """Reject browser mutations without a same-host Origin or Referer header.

    The request body is never read here, so multipart document uploads remain
    streamed to the normal form parser without buffering or size amplification.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        if not _same_host_origin(scope.get("headers", [])):
            response = PlainTextResponse("Недопустимый источник запроса", status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
