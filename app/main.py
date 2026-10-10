"""Веб-приложение платформы.

    .venv/bin/uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .api import routes_auth, routes_tasks
from .api.deps import NotAuthenticated
from .settings import settings

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
logger = logging.getLogger(__name__)

app = FastAPI(title="СРО-робот", docs_url="/api/docs" if settings.debug else None)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=not settings.debug)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(routes_auth.router)
app.include_router(routes_tasks.router)


@app.exception_handler(NotAuthenticated)
async def not_authenticated(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(request.url_for("login_page"), status_code=303)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception) -> PlainTextResponse:
    """Сохраняет стек необработанной ошибки, не показывая его пользователю."""
    logger.exception(
        "Unhandled request failure method=%s path=%s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return PlainTextResponse("Внутренняя ошибка сервера", status_code=500)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}
