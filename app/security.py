"""Small browser request protections for the cookie-authenticated HTML app."""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _same_host_origin(headers: list[tuple[bytes, bytes]]) -> bool:
    values = {key.lower(): value.decode("latin-1") for key, value in headers}
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
