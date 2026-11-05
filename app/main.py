"""Веб-приложение платформы.

    bash scripts/dev/run-web.sh
"""

from __future__ import annotations

import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from redis import Redis
from rq import Queue, Worker
from rq.registry import StartedJobRegistry
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api import (
    routes_assignments,
    routes_auth,
    routes_case_packages,
    routes_kad_helper,
    routes_kad_import,
    routes_mixed_batches,
    routes_tasks,
    routes_upload_batches,
)
from .api.deps import NotAuthenticated
from .db.base import engine
from .db.schema import (
    EXPECTED_SCHEMA_REVISION,
    configure_read_only_diagnostic_transaction,
    runtime_guard_differences,
)
from .observability import SafeAccessLogMiddleware
from .security import (
    ActionRequestAdmissionMiddleware,
    LoginRequestLimitMiddleware,
    RequestIdMiddleware,
    SameOriginMutationMiddleware,
    SecurityHeadersMiddleware,
    UploadAdmissionMiddleware,
)
from .settings import settings
from .services.auth import SESSION_MAX_AGE_SECONDS
from .worker_health import has_fresh_worker_heartbeat

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
logger = logging.getLogger(__name__)
app = FastAPI(title="СРО-робот", docs_url="/api/docs" if settings.debug else None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=SESSION_MAX_AGE_SECONDS,
    same_site="lax",
    https_only=not settings.debug,
)
app.add_middleware(
    LoginRequestLimitMiddleware,
    max_concurrent_requests=settings.login_max_concurrent_requests,
    body_read_timeout_seconds=settings.login_body_read_timeout_seconds,
)
app.add_middleware(
    UploadAdmissionMiddleware,
    max_request_bytes=settings.upload_max_request_bytes,
    max_concurrent_requests=settings.upload_max_concurrent_requests,
    body_read_timeout_seconds=settings.upload_body_read_timeout_seconds,
)
app.add_middleware(
    ActionRequestAdmissionMiddleware,
    max_request_bytes=settings.action_max_request_bytes,
    max_concurrent_requests=settings.action_max_concurrent_requests,
    body_read_timeout_seconds=settings.action_body_read_timeout_seconds,
)
app.add_middleware(SameOriginMutationMiddleware, public_origin=settings.public_app_origin)
if settings.public_app_origin:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[urlsplit(settings.public_app_origin).hostname, "localhost", "127.0.0.1"],
        www_redirect=False,
    )
app.add_middleware(RequestIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SafeAccessLogMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(routes_auth.router)
app.include_router(routes_tasks.router)
app.include_router(routes_upload_batches.router)
app.include_router(routes_mixed_batches.router)
app.include_router(routes_kad_helper.router)
app.include_router(routes_kad_import.router)
app.include_router(routes_assignments.router)
app.include_router(routes_case_packages.router)


def _document_storage_is_ready() -> bool:
    """Read-only check that the configured private storage root remains usable."""
    if os.name != "posix":
        return settings.upload_dir.is_dir() and os.access(
            settings.upload_dir,
            os.R_OK | os.W_OK | os.X_OK,
        )
    directory_fd = -1
    try:
        path_stat = settings.upload_dir.lstat()
        if not stat.S_ISDIR(path_stat.st_mode):
            return False
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        directory_fd = os.open(settings.upload_dir, flags)
        opened_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(opened_stat.st_mode)
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
            or opened_stat.st_uid != os.geteuid()
            or stat.S_IMODE(opened_stat.st_mode) != 0o700
        ):
            return False
        if hasattr(os, "fstatvfs") and hasattr(os, "ST_RDONLY"):
            return not (os.fstatvfs(directory_fd).f_flag & os.ST_RDONLY)
        return True
    except OSError:
        return False
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


@app.exception_handler(NotAuthenticated)
async def not_authenticated(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(request.url_for("login_page"), status_code=303)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception) -> PlainTextResponse:
    """Record a correlatable failure without persisting exception contents."""
    request_id = request.scope.get("state", {}).get("request_id", "unavailable")
    route = request.scope.get("route")
    route_template = getattr(route, "path", None)
    if not isinstance(route_template, str) or not route_template.startswith("/"):
        route_template = "<unmatched>"
    logger.error(
        "Unhandled request failure request_id=%s method=%s route=%s error_type=%s",
        request_id,
        request.method,
        route_template,
        type(exc).__name__,
    )
    return PlainTextResponse("Внутренняя ошибка сервера", status_code=500)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}


@app.get("/version", include_in_schema=False)
def release_version() -> dict:
    """Expose only the immutable release commit for deployment read-back."""
    marker = Path(__file__).resolve().parents[1] / ".release-commit"
    try:
        commit = marker.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        commit = "development"
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        commit = "development"
    return {"commit": commit}


@app.get("/readyz", include_in_schema=False)
def readyz(request: Request) -> JSONResponse:
    """Report whether required persistence and queue services are ready."""
    request_id = (
        request.scope.get("state", {}).get("request_id", "unavailable")
        if request is not None
        else "unavailable"
    )
    checks: dict[str, str] = {}
    if _document_storage_is_ready():
        checks["document_storage"] = "ok"
    else:
        checks["document_storage"] = "unavailable"
        logger.warning(
            "Readiness dependency unavailable request_id=%s "
            "dependency=document_storage error_type=DocumentStorageUnavailable",
            request_id,
        )
    try:
        with engine.connect() as connection:
            configure_read_only_diagnostic_transaction(connection)
            connection.execute(text("SELECT 1"))
            checks["database"] = "ok"
            try:
                revisions = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars().all()
                if revisions == [EXPECTED_SCHEMA_REVISION]:
                    checks["database_schema"] = "ok"
                    try:
                        guard_differences = runtime_guard_differences(connection)
                        if guard_differences:
                            checks["database_guards"] = "unavailable"
                            logger.warning(
                                "Readiness dependency unavailable request_id=%s "
                                "dependency=database_guards error_type=ManagedSchemaDrift",
                                request_id,
                            )
                        else:
                            checks["database_guards"] = "ok"
                    except Exception as exc:
                        checks["database_guards"] = "unavailable"
                        logger.warning(
                            "Readiness dependency unavailable request_id=%s "
                            "dependency=database_guards error_type=%s",
                            request_id,
                            type(exc).__name__,
                        )
                else:
                    checks["database_schema"] = "unavailable"
                    logger.warning(
                        "Readiness dependency unavailable request_id=%s "
                        "dependency=database_schema error_type=SchemaRevisionMismatch",
                        request_id,
                    )
            except Exception as exc:
                checks["database_schema"] = "unavailable"
                logger.warning(
                    "Readiness dependency unavailable request_id=%s "
                    "dependency=database_schema error_type=%s",
                    request_id,
                    type(exc).__name__,
                )
            if settings.ocr_task_leases_enabled:
                try:
                    columns = set(
                        connection.execute(
                            text(
                                "SELECT column_name "
                                "FROM information_schema.columns "
                                "WHERE table_schema = current_schema() "
                                "AND table_name = 'task_lease'"
                            )
                        ).scalars().all()
                    )
                    required_columns = {
                        "task_id",
                        "owner_token",
                        "job_kind",
                        "attempt",
                        "generation",
                        "lease_until",
                        "updated_at",
                    }
                    if required_columns <= columns:
                        checks["ocr_lease_schema"] = "ok"
                    else:
                        checks["ocr_lease_schema"] = "unavailable"
                        logger.warning(
                            "Readiness dependency unavailable "
                            "request_id=%s dependency=ocr_lease_schema "
                            "error_type=MissingRequiredColumns",
                            request_id,
                        )
                except Exception as exc:
                    checks["ocr_lease_schema"] = "unavailable"
                    logger.warning(
                        "Readiness dependency unavailable request_id=%s "
                        "dependency=ocr_lease_schema error_type=%s",
                        request_id,
                        type(exc).__name__,
                    )
    except Exception as exc:
        checks["database"] = "unavailable"
        logger.warning(
            "Readiness dependency unavailable request_id=%s dependency=database error_type=%s",
            request_id,
            type(exc).__name__,
        )
        if settings.ocr_task_leases_enabled:
            checks["ocr_lease_schema"] = "unavailable"

    client = None
    try:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        checks["redis"] = "ok"
        try:
            queues = [Queue(name, connection=client) for name in ("ocr", "sro")]
            uncovered_backlog = []
            checked_at = datetime.now(timezone.utc)
            for queue in queues:
                started_count = StartedJobRegistry(
                    queue.name,
                    connection=client,
                ).get_job_count(cleanup=False)
                if queue.count == 0 and started_count == 0:
                    continue
                workers = Worker.all(connection=client, queue=queue)
                if not any(has_fresh_worker_heartbeat(worker, checked_at) for worker in workers):
                    uncovered_backlog.append(queue.name)
            if uncovered_backlog:
                checks["queue_workers"] = "unavailable"
                logger.warning(
                    "Readiness dependency unavailable "
                    "request_id=%s dependency=queue_workers "
                    "error_type=BacklogWithoutFreshHeartbeat",
                    request_id,
                )
            else:
                checks["queue_workers"] = "ok"
        except Exception as exc:
            checks["queue_workers"] = "unavailable"
            logger.warning(
                "Readiness dependency unavailable request_id=%s "
                "dependency=queue_workers error_type=%s",
                request_id,
                type(exc).__name__,
            )
    except Exception as exc:
        checks["redis"] = "unavailable"
        logger.warning(
            "Readiness dependency unavailable request_id=%s dependency=redis error_type=%s",
            request_id,
            type(exc).__name__,
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                logger.warning(
                    "Readiness client cleanup failed request_id=%s "
                    "dependency=redis error_type=%s",
                    request_id,
                    type(exc).__name__,
                )

    is_ready = all(result == "ok" for result in checks.values())
    return JSONResponse(
        {"ok": is_ready, "checks": checks},
        status_code=200 if is_ready else 503,
    )
