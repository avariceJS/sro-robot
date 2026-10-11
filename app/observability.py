"""Privacy-safe HTTP access diagnostics for the browser application."""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send


# Uvicorn configures this logger to stderr by default; its raw access logger is disabled.
access_logger = logging.getLogger("uvicorn.error")


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path.startswith("/"):
        return route_path
    return "<unmatched>"


class SafeAccessLogMiddleware:
    """Log request outcome without recording the concrete URL or peer address."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_at = time.monotonic()
        response_status: int | None = None
        downstream_send_failed = False

        async def send_with_status(message: Message) -> None:
            nonlocal response_status, downstream_send_failed
            if message["type"] == "http.response.start":
                response_status = message["status"]
            try:
                await send(message)
            except Exception:
                downstream_send_failed = True
                response_status = None
                raise

        try:
            await self.app(scope, receive, send_with_status)
        except Exception:
            if response_status is None and not downstream_send_failed:
                response_status = 500
            raise
        finally:
            state = scope.get("state", {})
            request_id = state.get("request_id", "unavailable") if isinstance(state, dict) else "unavailable"
            method = scope.get("method", "UNKNOWN")
            status = response_status if response_status is not None else "unknown"
            elapsed_ms = max(0.0, (time.monotonic() - started_at) * 1000)
            access_logger.info(
                "HTTP request request_id=%s method=%s route=%s status=%s duration_ms=%.1f",
                request_id,
                method,
                _route_template(scope),
                status,
                elapsed_ms,
            )
