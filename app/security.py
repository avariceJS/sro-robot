"""Small browser request protections for the cookie-authenticated HTML app."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from urllib.parse import urlsplit

from redis import Redis
from redis.exceptions import RedisError
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_DOCUMENT_PREVIEW_PATH = re.compile(r"/tasks/[0-9]+/document/preview\Z")
_TASK_REVIEW_PATH = re.compile(
    r"/tasks/[0-9]+(?:/(?:save|approve|review-recovery))?\Z"
)
REQUEST_ID_HEADER = b"x-request-id"
_SECURITY_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    # Native form POSTs can carry Origin: null under no-referrer. Preserve
    # same-origin form provenance without disclosing URLs to other origins.
    b"referrer-policy": b"same-origin",
    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
    b"content-security-policy": (
        b"default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        b"form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        b"img-src 'self'; font-src 'self'; connect-src 'self'"
    ),
}
_PRIVATE_CACHE_HEADER = (b"cache-control", b"no-store")
_DOCUMENT_PREVIEW_SECURITY_HEADERS = {
    **_SECURITY_HEADERS,
    b"x-frame-options": b"SAMEORIGIN",
    b"content-security-policy": (
        b"default-src 'none'; sandbox; frame-ancestors 'self'"
    ),
}
_TASK_REVIEW_SECURITY_HEADERS = {
    **_SECURITY_HEADERS,
    # TIFF page blobs are created only from same-origin, authenticated preview
    # responses by the first-party review controller.
    b"content-security-policy": (
        b"default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        b"form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        b"img-src 'self' blob:; font-src 'self'; connect-src 'self'"
    ),
}
LOGIN_REQUEST_BODY_LIMIT_BYTES = 16 * 1024
LOGIN_BODY_READ_TIMEOUT_SECONDS = 30
LOGIN_MAX_CONCURRENT_REQUESTS = 2
LOGIN_CONCURRENCY_WAIT_SECONDS = 0.25
LOGIN_ATTEMPT_WINDOW_SECONDS = 60.0
LOGIN_MAX_ATTEMPTS_PER_WINDOW = 10
LOGIN_ATTEMPT_BUCKETS = 4096


class LoginAttemptLimiter:
    """Bound repeated credential checks per connection peer and normalized login."""

    def __init__(
        self,
        *,
        max_attempts: int = LOGIN_MAX_ATTEMPTS_PER_WINDOW,
        window_seconds: float = LOGIN_ATTEMPT_WINDOW_SECONDS,
        max_buckets: int = LOGIN_ATTEMPT_BUCKETS,
        clock=time.monotonic,
    ) -> None:
        if max_attempts < 1 or window_seconds <= 0 or max_buckets < 1:
            raise ValueError("login attempt limits must be positive")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_buckets = max_buckets
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._attempts: OrderedDict[bytes, deque[tuple[float, bytes, bool]]] = OrderedDict()
        self._lock = threading.Lock()

    def reserve(self, peer: str, normalized_login: str) -> tuple[bytes | None, int | None]:
        """Reserve one concurrent check; return a retry delay when the window is full."""
        identity = hmac.new(
            self._key,
            f"{peer}\0{' '.join(normalized_login.split()).casefold()}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        now = self._clock()
        with self._lock:
            attempts = self._attempts.pop(identity, deque())
            while attempts and now - attempts[0][0] >= self.window_seconds:
                attempts.popleft()
            if len(attempts) >= self.max_attempts:
                retry_after = max(
                    1, int(attempts[0][0] + self.window_seconds - now + 0.999)
                )
                self._attempts[identity] = attempts
                return None, retry_after
            token = secrets.token_bytes(16)
            attempts.append((now, token, False))
            self._attempts[identity] = attempts
            if len(self._attempts) > self.max_buckets:
                self._attempts.popitem(last=False)
        return token, None

    def finish(
        self,
        peer: str,
        normalized_login: str,
        token: bytes,
        *,
        outcome: str,
    ) -> None:
        """Record bad credentials, clear on success, or release an aborted DB check."""
        if outcome not in {"failed", "success", "aborted"}:
            raise ValueError("unsupported login attempt outcome")
        identity = hmac.new(
            self._key,
            f"{peer}\0{' '.join(normalized_login.split()).casefold()}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        with self._lock:
            attempts = self._attempts.pop(identity, None)
            if attempts is None:
                return
            updated = deque()
            for timestamp, attempt_token, failed in attempts:
                if attempt_token == token:
                    if outcome == "failed":
                        updated.append((timestamp, attempt_token, True))
                    continue
                if outcome == "success" and failed:
                    continue
                updated.append((timestamp, attempt_token, failed))
            if updated:
                self._attempts[identity] = updated


login_attempt_limiter = LoginAttemptLimiter()


class LoginAttemptLimiterUnavailable(RuntimeError):
    """The shared login throttle could not safely admit or finish an attempt."""


class RedisLoginAttemptLimiter:
    """Share the existing per-peer/login rolling limit across web processes."""

    _RESERVE_SCRIPT = """
local failed_key = KEYS[1]
local active_key = KEYS[2]
local identities_key = KEYS[3]
local window_ms = tonumber(ARGV[1])
local maximum = tonumber(ARGV[2])
local token = ARGV[3]
local max_buckets = tonumber(ARGV[4])
local identity = ARGV[5]
local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local cutoff = now_ms - window_ms
redis.call('ZREMRANGEBYSCORE', identities_key, '-inf', cutoff)
local known_identity = redis.call('ZSCORE', identities_key, identity)
if not known_identity and redis.call('ZCARD', identities_key) >= max_buckets then
    local oldest = redis.call('ZRANGE', identities_key, 0, 0, 'WITHSCORES')
    local retry_after = math.max(1, math.floor((tonumber(oldest[2]) + window_ms - now_ms + 999) / 1000))
    return {-1, retry_after}
end
redis.call('ZREMRANGEBYSCORE', failed_key, '-inf', cutoff)
redis.call('ZREMRANGEBYSCORE', active_key, '-inf', cutoff)
local count = redis.call('ZCARD', failed_key) + redis.call('ZCARD', active_key)
if count >= maximum then
    local failed_oldest = redis.call('ZRANGE', failed_key, 0, 0, 'WITHSCORES')
    local active_oldest = redis.call('ZRANGE', active_key, 0, 0, 'WITHSCORES')
    local oldest = nil
    if #failed_oldest > 0 then oldest = tonumber(failed_oldest[2]) end
    if #active_oldest > 0 then
        local active_score = tonumber(active_oldest[2])
        if oldest == nil or active_score < oldest then oldest = active_score end
    end
    local retry_after = math.max(1, math.floor((oldest + window_ms - now_ms + 999) / 1000))
    return {0, retry_after}
end
redis.call('ZADD', identities_key, now_ms, identity)
redis.call('PEXPIRE', identities_key, window_ms + 1000)
redis.call('ZADD', active_key, now_ms, token)
redis.call('PEXPIRE', active_key, window_ms + 1000)
redis.call('PEXPIRE', failed_key, window_ms + 1000)
return {1, token}
"""

    _FINISH_SCRIPT = """
local failed_key = KEYS[1]
local active_key = KEYS[2]
local token = ARGV[1]
local outcome = ARGV[2]
local ttl_ms = tonumber(ARGV[3]) + 1000
local score = redis.call('ZSCORE', active_key, token)
redis.call('ZREM', active_key, token)
if outcome == 'failed' and score then
    redis.call('ZADD', failed_key, score, token)
elseif outcome == 'success' then
    redis.call('DEL', failed_key)
elseif outcome ~= 'aborted' then
    return redis.error_reply('unsupported login attempt outcome')
end
if redis.call('EXISTS', active_key) == 1 then redis.call('PEXPIRE', active_key, ttl_ms) end
if redis.call('EXISTS', failed_key) == 1 then redis.call('PEXPIRE', failed_key, ttl_ms) end
return 1
"""

    def __init__(
        self,
        client: Redis,
        *,
        secret_key: str,
        max_attempts: int = LOGIN_MAX_ATTEMPTS_PER_WINDOW,
        window_seconds: float = LOGIN_ATTEMPT_WINDOW_SECONDS,
        max_buckets: int = LOGIN_ATTEMPT_BUCKETS,
    ) -> None:
        if max_attempts < 1 or window_seconds <= 0 or max_buckets < 1:
            raise ValueError("login attempt limits must be positive")
        if len(secret_key.encode("utf-8")) < 32:
            raise ValueError("shared login limiter key must contain at least 32 bytes")
        self.client = client
        self.max_attempts = max_attempts
        self.window_ms = int(window_seconds * 1000)
        self.max_buckets = max_buckets
        self._key = secret_key.encode("utf-8")

    @staticmethod
    def _response_integer(value: object) -> int:
        """Accept only Redis integer replies used by the Lua protocol."""
        if type(value) is not int:
            raise LoginAttemptLimiterUnavailable("invalid shared limiter response")
        return value

    def _keys(self, peer: str, normalized_login: str) -> tuple[str, str, str, str]:
        normalized = " ".join(normalized_login.split()).casefold()
        scope = hmac.new(
            self._key, b"sro-robot/login-attempt-registry/v1\0", hashlib.sha256
        ).hexdigest()
        identity = hmac.new(
            self._key,
            b"sro-robot/login-attempt/v1\0"
            + peer.encode("utf-8")
            + b"\0"
            + normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        redis_scope = f"sro:login-attempt:v1:{{{scope}}}"
        identity_key = f"{redis_scope}:{identity}"
        return (
            f"{identity_key}:failed",
            f"{identity_key}:active",
            f"{redis_scope}:identities",
            identity,
        )

    def reserve(self, peer: str, normalized_login: str) -> tuple[bytes | None, int | None]:
        """Atomically reserve a shared rolling-window slot or return retry delay."""
        failed_key, active_key, identities_key, identity = self._keys(peer, normalized_login)
        token = secrets.token_bytes(16)
        try:
            result = self.client.eval(
                self._RESERVE_SCRIPT,
                3,
                failed_key,
                active_key,
                identities_key,
                self.window_ms,
                self.max_attempts,
                token.hex(),
                self.max_buckets,
                identity,
            )
        except RedisError as exc:
            raise LoginAttemptLimiterUnavailable from exc
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise LoginAttemptLimiterUnavailable("invalid shared limiter response")
        status = self._response_integer(result[0])
        if status in {-1, 0}:
            retry_after = self._response_integer(result[1])
            if retry_after < 1:
                raise LoginAttemptLimiterUnavailable("invalid shared limiter retry delay")
            return None, retry_after
        if status != 1 or result[1] not in {token.hex(), token.hex().encode()}:
            raise LoginAttemptLimiterUnavailable("invalid shared limiter reservation")
        return token, None

    def finish(
        self,
        peer: str,
        normalized_login: str,
        token: bytes,
        *,
        outcome: str,
    ) -> None:
        """Record failure, clear failures after success, or release an aborted check."""
        if outcome not in {"failed", "success", "aborted"}:
            raise ValueError("unsupported login attempt outcome")
        failed_key, active_key, _identities_key, _identity = self._keys(
            peer, normalized_login
        )
        try:
            result = self.client.eval(
                self._FINISH_SCRIPT,
                2,
                failed_key,
                active_key,
                token.hex(),
                outcome,
                self.window_ms,
            )
        except RedisError as exc:
            raise LoginAttemptLimiterUnavailable from exc
        if self._response_integer(result) != 1:
            raise LoginAttemptLimiterUnavailable("invalid shared limiter completion")


def create_login_attempt_limiter(*, debug: bool, redis_url: str, secret_key: str):
    """Keep local development simple and share production throttles through Redis."""
    if debug:
        return login_attempt_limiter
    client = Redis.from_url(
        redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
        max_connections=2,
    )
    return RedisLoginAttemptLimiter(client, secret_key=secret_key)


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
        body_read_timeout_seconds: float = 300,
    ) -> None:
        if (
            max_request_bytes < 1
            or max_concurrent_requests < 1
            or body_read_timeout_seconds <= 0
        ):
            raise ValueError("upload admission limits must be positive")
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)
        self.body_read_timeout_seconds = body_read_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "").upper() != "POST"
            or not (
                scope.get("path") in {"/upload", "/kad-helper/import"}
                or scope.get("path", "").startswith("/upload-batches/")
            )
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
            body_read_started: float | None = None

            async def counted_receive():
                nonlocal total_bytes, body_read_started
                loop = asyncio.get_running_loop()
                if body_read_started is None:
                    body_read_started = loop.time()
                remaining = self.body_read_timeout_seconds - (
                    loop.time() - body_read_started
                )
                if remaining <= 0:
                    raise _UploadBodyReadTimeout
                try:
                    async with asyncio.timeout(remaining):
                        message = await receive()
                except TimeoutError as exc:
                    raise _UploadBodyReadTimeout from exc
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
            except _UploadBodyReadTimeout:
                if response_started:
                    raise
                await PlainTextResponse("Время ожидания загрузки истекло", status_code=408)(
                    scope, receive, send
                )


class ActionRequestAdmissionMiddleware:
    """Bound small non-login POST bodies before application form parsing."""

    def __init__(
        self,
        app: ASGIApp,
        max_request_bytes: int = 64 * 1024,
        max_concurrent_requests: int = 32,
        body_read_timeout_seconds: float = 30,
        concurrency_wait_seconds: float = 0.25,
        manifest_max_request_bytes: int = 128 * 1024,
    ) -> None:
        if (
            max_request_bytes < 1
            or max_concurrent_requests < 1
            or body_read_timeout_seconds <= 0
            or concurrency_wait_seconds <= 0
            or manifest_max_request_bytes < 1
        ):
            raise ValueError("action request limits must be positive")
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.max_concurrent_requests = max_concurrent_requests
        self.body_read_timeout_seconds = body_read_timeout_seconds
        self.concurrency_wait_seconds = concurrency_wait_seconds
        self.manifest_max_request_bytes = manifest_max_request_bytes
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "").upper() in _SAFE_METHODS
            or (
                scope.get("method", "").upper() == "POST"
                and (
                    scope.get("path") in {"/upload", "/kad-helper/import", "/login"}
                    or scope.get("path", "").startswith("/upload-batches/")
                )
            )
        ):
            await self.app(scope, receive, send)
            return

        content_lengths = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        if len(content_lengths) > 1 or (
            content_lengths
            and (not content_lengths[0] or not content_lengths[0].isdigit())
        ):
            await PlainTextResponse("Некорректный запрос", status_code=400)(scope, receive, send)
            return
        request_limit = (
            self.manifest_max_request_bytes
            if scope.get("path") == "/upload-batches"
            else self.max_request_bytes
        )
        if content_lengths and _decimal_exceeds(content_lengths[0], request_limit):
            await PlainTextResponse("Слишком большой запрос", status_code=413)(scope, receive, send)
            return

        try:
            await asyncio.wait_for(
                self.semaphore.acquire(), timeout=self.concurrency_wait_seconds
            )
        except TimeoutError:
            response = PlainTextResponse(
                "Слишком много запросов на обработку",
                status_code=503,
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return

        try:
            total_bytes = 0
            response_started = False
            body_read_started: float | None = None

            async def counted_receive():
                nonlocal total_bytes, body_read_started
                loop = asyncio.get_running_loop()
                if body_read_started is None:
                    body_read_started = loop.time()
                remaining = self.body_read_timeout_seconds - (
                    loop.time() - body_read_started
                )
                if remaining <= 0:
                    raise _ActionBodyReadTimeout
                try:
                    async with asyncio.timeout(remaining):
                        message = await receive()
                except TimeoutError as exc:
                    raise _ActionBodyReadTimeout from exc
                if message["type"] == "http.request":
                    total_bytes += len(message.get("body", b""))
                    if total_bytes > request_limit:
                        raise _ActionBodyTooLarge
                return message

            async def tracked_send(message):
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                await send(message)

            try:
                await self.app(scope, counted_receive, tracked_send)
            except _ActionBodyTooLarge:
                if response_started:
                    raise
                await PlainTextResponse("Слишком большой запрос", status_code=413)(
                    scope, receive, send
                )
            except _ActionBodyReadTimeout:
                if response_started:
                    raise
                await PlainTextResponse("Время ожидания запроса истекло", status_code=408)(
                    scope, receive, send
                )
        finally:
            self.semaphore.release()


class _UploadBodyTooLarge(Exception):
    """Internal signal used to stop multipart parsing once the byte budget is exceeded."""


class _UploadBodyReadTimeout(Exception):
    """Internal signal used to stop slow or stalled upload-body collection."""


class _ActionBodyTooLarge(Exception):
    """Internal signal used to stop an oversized browser action form."""


class _ActionBodyReadTimeout(Exception):
    """Internal signal used to stop a stalled browser action form."""


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
                is_document_preview = bool(
                    _DOCUMENT_PREVIEW_PATH.fullmatch(scope.get("path", ""))
                )
                is_task_review = bool(_TASK_REVIEW_PATH.fullmatch(scope.get("path", "")))
                security_headers = (
                    _DOCUMENT_PREVIEW_SECURITY_HEADERS
                    if is_document_preview
                    else _TASK_REVIEW_SECURITY_HEADERS
                    if is_task_review
                    else _SECURITY_HEADERS
                )
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in security_headers
                    and (is_static_asset or key.lower() != _PRIVATE_CACHE_HEADER[0])
                ]
                headers.extend(security_headers.items())
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


def _same_host_origin(headers: list[tuple[bytes, bytes]], public_origin: str | None = None) -> bool:
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

    # Cloud mutations must originate at the configured HTTPS site. The legacy
    # loopback workflow retains its existing rules; forwarded headers are not
    # used as authority for this check.
    if public_origin:
        try:
            request_host = urlsplit("//" + host).hostname
        except ValueError:
            return False
    else:
        request_host = None
    if public_origin and request_host not in {"localhost", "127.0.0.1"}:
        if host not in {urlsplit(public_origin).netloc, urlsplit(public_origin).netloc + ":443"}:
            return False
        origin = values.get(b"origin")
        if origin is not None:
            return origin == public_origin
        referer = values.get(b"referer")
        if not referer or any(char.isspace() or ord(char) < 32 for char in referer) or "\\" in referer:
            return False
        try:
            parsed = urlsplit(referer)
            return (
                parsed.scheme == "https"
                and parsed.hostname == urlsplit(public_origin).hostname
                and parsed.port in {None, 443}
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
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

    def __init__(self, app, public_origin: str | None = None):
        self.app = app
        self.public_origin = public_origin

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        if not _same_host_origin(scope.get("headers", []), self.public_origin):
            response = PlainTextResponse("Недопустимый источник запроса", status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
