"""Веб-приложение платформы.

    .venv/bin/uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from redis import Redis
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from .api import routes_auth, routes_tasks
from .api.deps import NotAuthenticated
from .db.base import engine
from .security import (
    RequestIdMiddleware,
    SameOriginMutationMiddleware,
    SecurityHeadersMiddleware,
)
from .settings import settings

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
logger = logging.getLogger(__name__)

app = FastAPI(title="СРО-робот", docs_url="/api/docs" if settings.debug else None)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=not settings.debug)
app.add_middleware(SameOriginMutationMiddleware)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(routes_auth.router)
app.include_router(routes_tasks.router)


@app.exception_handler(NotAuthenticated)
async def not_authenticated(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(request.url_for("login_page"), status_code=303)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception) -> PlainTextResponse:
    """Record a correlatable failure without persisting exception contents."""
    request_id = request.scope.get("state", {}).get("request_id", "unavailable")
    logger.error(
        "Unhandled request failure request_id=%s method=%s path=%s error_type=%s",
        request_id,
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    return PlainTextResponse("Внутренняя ошибка сервера", status_code=500)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}


@app.get("/readyz", include_in_schema=False)
def readyz() -> JSONResponse:
    """Report whether required persistence and queue services are reachable."""
    checks: dict[str, str] = {}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = "unavailable"
        logger.warning("Readiness dependency unavailable dependency=database error_type=%s", type(exc).__name__)

    client = None
    try:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = "unavailable"
        logger.warning("Readiness dependency unavailable dependency=redis error_type=%s", type(exc).__name__)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                logger.warning(
                    "Readiness client cleanup failed dependency=redis error_type=%s",
                    type(exc).__name__,
                )

    is_ready = all(result == "ok" for result in checks.values())
    return JSONResponse(
        {"ok": is_ready, "checks": checks},
        status_code=200 if is_ready else 503,
    )
