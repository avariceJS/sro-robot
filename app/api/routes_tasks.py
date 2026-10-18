"""Задания: список, загрузка, проверка полей."""

from __future__ import annotations

import html
import logging
import re
import hashlib
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile
from sqlalchemy.orm import Session

from ..db.base import get_session
from ..db.models import (
    AssignmentDraft,
    MAX_INTAKE_FILENAME_CHARACTERS,
    DebtorCardDraft,
    IntakeSource,
    Task,
    TaskStatus,
    User,
)
from ..settings import settings
from ..services import bulk, fields, outbox, tasks
from ..services.artifacts import image_within_inline_preview_limit, read_document, read_screenshot
from ..services.fields import scenario_of
from ..services.assignments import PREPARATION_KEY
from ..observability import request_id_from_scope
from ..web.templates import templates
from sro_robot.core.ocr import IMAGE_SUFFIXES
from ..workers.queues import (
    QueueUnavailable,
    enqueue_sro_form_preview,
)
from .deps import current_user

router = APIRouter(tags=["tasks"])
logger = logging.getLogger(__name__)
BULK_STATUS_FILTERS = frozenset({
    "queued", "processing", "prepared", "needs_review", "failed", "other",
})
ACTIVE_TASK_STATUSES = frozenset({
    TaskStatus.new,
    TaskStatus.recognizing,
    TaskStatus.writing,
    TaskStatus.awaiting_preview,
})
MAX_INTAKE_OPERATION_IDS = 100
MAX_PAGE_NUMBER = 1_000_000
SUPPORTED_UPLOAD_SUFFIXES = frozenset((*IMAGE_SUFFIXES, ".pdf"))


def _supported_upload_filename(filename: str) -> bool:
    return Path(filename).suffix.casefold() in SUPPORTED_UPLOAD_SUFFIXES


def _dashboard_filters(
    request: Request,
) -> tuple[list[int] | None, str | None, int, tuple[str, datetime, int] | None]:
    raw_intake = request.query_params.get("intake")
    operation_ids: list[int] | None = None
    if raw_intake is not None:
        parts = raw_intake.split(",")
        if (
            not parts
            or len(parts) > MAX_INTAKE_OPERATION_IDS
            or any(not re.fullmatch(r"[1-9][0-9]{0,18}", part) for part in parts)
        ):
            raise HTTPException(status_code=422, detail="Некорректный список пакетов")
        operation_ids = [int(part) for part in parts]
        if len(set(operation_ids)) != len(operation_ids):
            raise HTTPException(status_code=422, detail="Некорректный список пакетов")

    status = request.query_params.get("status") or None
    if status is not None and status not in BULK_STATUS_FILTERS:
        raise HTTPException(status_code=422, detail="Некорректный фильтр статуса")
    raw_page = request.query_params.get("page", "1")
    if not re.fullmatch(r"[1-9][0-9]{0,5}", raw_page):
        raise HTTPException(status_code=422, detail="Некорректная страница")
    page = int(raw_page)
    if page > MAX_PAGE_NUMBER:
        raise HTTPException(status_code=422, detail="Некорректная страница")
    cursor = None
    cursor_values = []
    for direction in ("before", "after"):
        raw_created = request.query_params.get(f"{direction}_created_at")
        raw_id = request.query_params.get(f"{direction}_id")
        if (raw_created is None) != (raw_id is None):
            raise HTTPException(status_code=422, detail="Некорректный курсор страницы")
        if raw_created is not None:
            if len(raw_created) > 64:
                raise HTTPException(status_code=422, detail="Некорректный курсор страницы")
            try:
                created_at = datetime.fromisoformat(raw_created)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="Некорректный курсор страницы") from exc
            task_id = _positive_dashboard_id(raw_id)
            if created_at.tzinfo is None or task_id is None:
                raise HTTPException(status_code=422, detail="Некорректный курсор страницы")
            cursor_values.append((direction, created_at, task_id))
    if len(cursor_values) > 1:
        raise HTTPException(status_code=422, detail="Некорректный курсор страницы")
    if cursor_values:
        if page != 1:
            raise HTTPException(status_code=422, detail="Некорректный курсор страницы")
        cursor = cursor_values[0]
    return operation_ids, status, page, cursor


def _positive_dashboard_id(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"[1-9][0-9]{0,9}", value):
        return None
    parsed = int(value)
    return parsed if parsed <= 2_147_483_647 else None


def _dashboard_context(db: Session, request: Request) -> dict[str, object]:
    operation_ids, status, page, cursor = _dashboard_filters(request)
    context = bulk.bulk_task_context(
        db,
        operation_ids=operation_ids,
        status=status,
        page=page,
        cursor=cursor,
    )
    pages = max(1, int(context["pages"]))
    page = min(int(context["page"]), pages)
    filters = {}
    if operation_ids:
        filters["intake"] = ",".join(str(value) for value in operation_ids)
    if status:
        filters["status"] = status
    poll_filters = dict(filters)
    if cursor:
        poll_filters[f"{cursor[0]}_created_at"] = cursor[1].isoformat()
        poll_filters[f"{cursor[0]}_id"] = str(cursor[2])
    elif page > 1:
        poll_filters["page"] = str(page)
    def cursor_url(name: str) -> str:
        cursor_value = context.get(f"{name}_cursor")
        if not cursor_value:
            return ""
        direction = "after" if name == "previous" else "before"
        return str(request.url_for("tasks_page")) + "?" + urlencode({
            **filters,
            f"{direction}_created_at": cursor_value[0].isoformat(),
            f"{direction}_id": cursor_value[1],
        })
    context.update({
        "operation_ids": operation_ids or [],
        "intake_query": ",".join(str(value) for value in operation_ids or []),
        "status_filter": status or "",
        "page": page,
        "pages": pages,
        "poll_url": str(request.url_for("refresh_task_rows")) +
                    ("?" + urlencode(poll_filters) if poll_filters else ""),
        "previous_url": cursor_url("previous"),
        "next_url": cursor_url("next"),
    })
    return context


def _queue_unavailable(exc: QueueUnavailable, request_id: str | None) -> HTTPException:
    logger.warning(
        "Background queue admission unavailable request_id=%s error_type=%s",
        request_id or "unavailable",
        exc.error_type,
    )
    return HTTPException(
        status_code=503,
        detail="Не удалось подтвердить постановку в очередь; задача могла быть поставлена. Повторите действие позже.",
        headers={"Retry-After": "5"},
    )


def _require_task(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    return task


async def _values_from_form(request: Request, task: Task) -> dict[str, str]:
    form = await request.form()
    scenario = scenario_of(task)
    return {spec.attr: str(form.get(spec.attr, "") or "") for spec in scenario.fields}


@router.get("/debtors/intake", name="tasks_page")
def tasks_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    dashboard = _dashboard_context(db, request)
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "user": user,
            **dashboard,
            "upload_idempotency_key": secrets.token_hex(24),
            "upload_max_files": settings.upload_max_files,
            "upload_max_batch_files": settings.upload_max_batch_files,
            "upload_max_file_bytes": settings.upload_max_file_bytes,
            "upload_max_batch_bytes": settings.upload_max_batch_bytes,
        },
    )


@router.get("/", name="home_page")
def home_page(user: User = Depends(current_user)):
    """Start staff on the debtor worklist rather than the technical queue."""
    return RedirectResponse("/debtors", status_code=303)


@router.get("/tasks/{task_id}/document", name="task_document")
def task_document(
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> Response:
    """Return the original source as a bounded, hash-verified attachment."""
    task = _require_task(db, task_id)
    document = task.document
    if document is None:
        raise HTTPException(status_code=404, detail="Исходный документ не найден")
    try:
        content, media_type, suffix = read_document(
            str(document.stored_path),
            str(document.content_hash),
            settings.upload_dir,
            settings.upload_max_file_bytes,
        )
    except (OSError, ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Исходный документ недоступен") from None
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="source-document{suffix}"',
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/tasks/{task_id}/document/preview", name="task_document_preview")
def task_document_preview(
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> Response:
    """Render supported source documents inline after private hash verification."""
    task = _require_task(db, task_id)
    document = task.document
    if document is None:
        raise HTTPException(status_code=404, detail="Исходный документ не найден")
    try:
        content, media_type, suffix = read_document(
            str(document.stored_path),
            str(document.content_hash),
            settings.upload_dir,
            settings.upload_max_file_bytes,
        )
    except (OSError, ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Исходный документ недоступен") from None
    supported = {
        ("application/pdf", ".pdf"),
        ("image/jpeg", ".jpg"),
        ("image/jpeg", ".jpeg"),
        ("image/png", ".png"),
    }
    if (media_type, suffix.casefold()) not in supported:
        raise HTTPException(status_code=415, detail="Предпросмотр поддерживает только PDF, JPEG и PNG")
    if media_type.startswith("image/") and not image_within_inline_preview_limit(content, media_type):
        raise HTTPException(
            status_code=415,
            detail="Предпросмотр фото ограничен 12 Мп. Скачайте оригинал для полного просмотра.",
        )
    filename = "source-document" + suffix.casefold()
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/tasks/status-list", name="refresh_task_rows")
def refresh_task_rows(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> Response:
    dashboard = _dashboard_context(db, request)
    response = templates.TemplateResponse(
        request,
        "task_rows.html",
        {"user": user, **dashboard},
    )
    if not dashboard["should_poll"] and request.headers.get("HX-Request", "").lower() == "true":
        # htmx 2.x swaps this final response and stops the polling trigger.
        response.status_code = 286
    return response


@router.post("/upload", name="upload_documents")
async def upload_documents(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> Response:
    request_id = _server_request_id(request)
    async with request.form(max_files=settings.upload_max_files + 1, max_fields=1) as form:
        idempotency_key = str(form.get("idempotency_key", ""))
        if not re.fullmatch(r"[0-9a-f]{48}", idempotency_key):
            raise HTTPException(status_code=422, detail="Обновите страницу перед загрузкой")
        uploads = form.getlist("files")
        if not uploads or any(not isinstance(upload, StarletteUploadFile) for upload in uploads):
            raise HTTPException(status_code=422, detail="Нужен хотя бы один файл")
        if len(uploads) > settings.upload_max_files:
            raise HTTPException(status_code=413, detail="Слишком много файлов")

        accepted: list[tuple[str, bytes]] = []
        batch_bytes = 0
        for upload in uploads:
            filename = upload.filename or "document"
            if len(filename) > MAX_INTAKE_FILENAME_CHARACTERS:
                raise HTTPException(
                    status_code=422,
                    detail="Имя файла превышает допустимую длину",
                )
            if "\x00" in filename:
                raise HTTPException(
                    status_code=422,
                    detail="Имя файла содержит недопустимые символы",
                )
            if not _supported_upload_filename(filename):
                raise HTTPException(
                    status_code=422,
                    detail="Поддерживаются PDF, JPEG, PNG, TIFF, BMP и WebP",
                )
            remaining_batch_bytes = settings.upload_max_batch_bytes - batch_bytes
            read_limit = min(settings.upload_max_file_bytes, remaining_batch_bytes)
            content = await upload.read(read_limit + 1)
            if not content:
                raise HTTPException(status_code=422, detail="Пустые файлы не принимаются")
            if len(content) > settings.upload_max_file_bytes:
                raise HTTPException(status_code=413, detail="Файл превышает допустимый размер")
            if len(content) > remaining_batch_bytes:
                raise HTTPException(status_code=413, detail="Пакет превышает допустимый размер")
            batch_bytes += len(content)
            accepted.append((filename, content))

    source = IntakeSource.upload if len(accepted) == 1 else IntakeSource.batch
    manifest = tasks.intake_manifest(
        source.value,
        [hashlib.sha256(content).hexdigest() for _filename, content in accepted],
    )
    source_metadata_manifest = tasks.intake_source_metadata_manifest(
        [filename for filename, _content in accepted]
    )
    try:
        operation, is_new = tasks.begin_intake_operation(
            db,
            user_id=user.id,
            idempotency_key=idempotency_key,
            manifest_sha256=manifest,
            source_metadata_sha256=source_metadata_manifest,
        )
    except tasks.IntakeOperationConflict as exc:
        raise HTTPException(status_code=409, detail="Ключ запроса уже использован для другого ввода") from exc

    if is_new:
        operation_tasks = []
        for item_index, (filename, content) in enumerate(accepted):
            try:
                document = tasks.store_document(db, filename, content, source, user.id)
            except tasks.ExistingDocumentUnavailable as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Хранилище документа недоступно; повторите запрос после проверки",
                ) from exc
            except OSError as exc:
                logger.warning(
                    "Document intake storage unavailable request_id=%s error_type=%s",
                    request_id or "unavailable",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=503,
                    detail="Не удалось сохранить документ; проверьте хранилище перед повтором",
                ) from None
            intake_item = tasks.create_intake_item(
                db,
                operation_id=operation.id,
                item_index=item_index,
                document=document,
                filename=filename,
                source=source,
                received_by_id=user.id,
            )
            task = tasks.create_task(
                db,
                document,
                user_id=user.id,
                request_id=request_id,
                intake_operation_id=operation.id,
                intake_item_id=intake_item.id,
            )
            operation_tasks.append(task)
        intent_ids = [
            outbox.create_intake_intent(
                db,
                task_id=task.id,
                operation_id=operation.id,
                job_kind="ocr",
            )
            for task in operation_tasks
        ]
    else:
        operation_tasks = tasks.operation_tasks(db, operation.id)
        if not operation_tasks:
            raise RuntimeError("Accepted intake operation has no linked tasks")
        tasks.log_intake_replay(db, operation_tasks, user.id, request_id)
        # An accepted operation with no pending outbox row has already had its
        # enqueue acknowledged. Replays must not enqueue a duplicate while a
        # worker is still showing the task as new. Queue-admission failures keep
        # their durable outbox row and can safely retry that exact intent.
        intent_ids = outbox.pending_intake_intents(
            db,
            operation_id=operation.id,
            task_ids=[task.id for task in operation_tasks],
        )

    db.commit()
    try:
        for intent_id in intent_ids:
            outbox.dispatch_required(db, intent_id)
    except QueueUnavailable as exc:
        raise _queue_unavailable(exc, request_id) from exc
    task_ids = [task.id for task in operation_tasks]
    if "application/json" in request.headers.get("accept", "").lower():
        return JSONResponse(
            {"operation_id": operation.id, "task_ids": task_ids},
            status_code=200,
        )
    return RedirectResponse("/", status_code=303)


def _server_request_id(request: Request) -> str | None:
    """Return only the opaque request ID generated by RequestIdMiddleware."""
    return request_id_from_scope(request.scope)


@router.get("/tasks/{task_id}", name="task_page")
def task_page(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    task = _require_task(db, task_id)
    internal_card = db.query(DebtorCardDraft).filter_by(source_task_id=task.id).one_or_none()
    internal_assignment = db.query(AssignmentDraft).filter_by(source_task_id=task.id).one_or_none()
    preparation = (task.recognized or {}).get(PREPARATION_KEY) or {}
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "user": user,
            "task": task,
            "document_preview_kind": (
                Path(str(getattr(task.document, "stored_path", ""))).suffix.lower()
                if task.document is not None else ""
            ),
            "fields": fields.review_fields(task),
            "TaskStatus": TaskStatus,
            "status_ru": tasks.status_label(db, task),
            "internal_card": internal_card,
            "internal_assignment": internal_assignment,
            "approval_current": bool(
                task.status == TaskStatus.approved and tasks.approval_matches(db, task)
            ),
            "preparation_reason": preparation.get("reason", ""),
            "review_token": tasks.review_token(task),
            "internal_card_error": request.query_params.get("internal_card", ""),
        },
    )


@router.post("/tasks/{task_id}/save", name="task_save")
async def task_save(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    task = _require_task(db, task_id)
    form = await request.form()
    values = await _values_from_form(request, task)
    request_id = _server_request_id(request)
    try:
        tasks.assert_review_token(db, task, str(form.get("review_token", "")))
    except ValueError as exc:
        if tasks.saved_review_replay_matches(db, task, values, user.id):
            db.commit()
            return RedirectResponse(f"/tasks/{task_id}", status_code=303)
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="save_fields",
            user_id=user.id,
            request_id=request_id,
        )
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        tasks.save_values(
            db,
            task,
            values,
            user.id,
            request_id=request_id,
        )
    except ValueError as exc:
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="save_fields",
            user_id=user.id,
            request_id=request_id,
        )
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/approve", name="task_approve")
async def task_approve(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    task = _require_task(db, task_id)
    form = await request.form()
    values = await _values_from_form(request, task)
    request_id = _server_request_id(request)
    try:
        tasks.assert_review_token(db, task, str(form.get("review_token", "")))
    except ValueError as exc:
        if tasks.approved_review_replay_matches(db, task, values, user.id):
            db.commit()
            return RedirectResponse(f"/tasks/{task_id}", status_code=303)
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="approve",
            user_id=user.id,
            request_id=request_id,
        )
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        tasks.save_values(db, task, values, user.id, request_id=request_id)
        tasks.approve(db, task, user.id, request_id=request_id)
    except ValueError as exc:
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="approve",
            user_id=user.id,
            request_id=request_id,
        )
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.get("/tasks/{task_id}/confirm-reject", name="task_confirm_reject")
def task_confirm_reject(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    task = _require_task(db, task_id)
    if task.status not in (TaskStatus.new, TaskStatus.needs_review, TaskStatus.approved):
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)
    return templates.TemplateResponse(
        request,
        "confirm_reject.html",
        {"user": user, "task": task},
    )


@router.post("/tasks/{task_id}/reject", name="task_reject")
def task_reject(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    task = _require_task(db, task_id)
    try:
        tasks.reject(db, task, user.id, request_id=_server_request_id(request))
    except tasks.InvalidTaskTransition as exc:
        if tasks.rejected_action_replay_matches(db, task, user.id):
            db.commit()
            return RedirectResponse(f"/tasks/{task_id}", status_code=303)
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="reject",
            user_id=user.id,
            request_id=_server_request_id(request),
        )
        db.commit()
        raise HTTPException(
            status_code=409,
            detail="Задание уже обрабатывается или завершено",
        ) from exc
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/run", name="task_run")
def task_run(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    task = _require_task(db, task_id)
    if getattr(task, "scenario", "person_card") != "person_card":
        raise HTTPException(status_code=409, detail="Локальная проверка для этого типа должника пока не настроена")
    intent_id = tasks.request_local_validation(
        db, task, user.id, _server_request_id(request)
    )
    if intent_id is not None:
        # Persist the attributable request before handing it to Redis. If queue
        # admission fails, the audit trail still records what the operator asked.
        db.commit()
        try:
            outbox.dispatch_required(db, intent_id)
        except QueueUnavailable as exc:
            raise _queue_unavailable(exc, _server_request_id(request)) from exc
    else:
        if tasks.local_validation_request_replay_matches(db, task, user.id):
            db.commit()
            return RedirectResponse(f"/tasks/{task_id}", status_code=303)
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="local_validation",
            user_id=user.id,
            request_id=_server_request_id(request),
        )
        db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/prepare-sro-form", name="task_prepare_sro_form")
def task_prepare_sro_form(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    """Queue the narrowly guarded Add -> fill -> Close SRO form preview."""
    task = _require_task(db, task_id)
    if getattr(task, "scenario", "person_card") != "person_card":
        raise HTTPException(status_code=409, detail="Подготовка формы СРО доступна только для поддержанного сценария физического лица")
    if task.status == TaskStatus.approved and tasks.approval_matches(db, task):
        try:
            tasks.mark_writing(db, task, dry_run=True)
        except tasks.InvalidTaskTransition as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Задание уже обрабатывается или завершено") from exc
    elif not tasks.retry_sro_preview_after_reconciliation(db, task):
        raise HTTPException(
            status_code=409,
            detail="Подтверждение устарело или предыдущую попытку нельзя безопасно повторить",
        )
    db.commit()
    try:
        enqueue_sro_form_preview(task_id, user.id)
    except QueueUnavailable as exc:
        # Do not reset an ambiguous queue submission: inspect task/queue state
        # before any retry so that Add cannot be invoked twice unknowingly.
        logger.warning(
            "SRO form preview enqueue uncertain request_id=%s error_type=%s",
            _server_request_id(request) or "unavailable",
            exc.error_type,
        )
        raise _queue_unavailable(exc, _server_request_id(request)) from exc
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/decision", name="task_decision")
async def task_decision(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    """Ответ оператора роботу, который стоит на заполненной форме."""
    task = _require_task(db, task_id)
    form = await request.form()
    decision = str(form.get("decision", "close"))
    if decision != "close":
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="preview_decision",
            user_id=user.id,
            request_id=_server_request_id(request),
        )
        db.commit()
        raise HTTPException(status_code=409, detail="Разрешено только закрыть форму без сохранения")
    request_id = _server_request_id(request)
    if tasks.decide_preview(db, task, decision, user.id, request_id):
        db.commit()
    else:
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="preview_decision",
            user_id=user.id,
            request_id=request_id,
        )
        db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.get("/tasks/{task_id}/shot/{index}", name="task_shot")
def task_shot(
    task_id: int,
    index: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> Response:
    """Снимок заполненной формы. Отдаются только пути, записанные в задание."""
    task = _require_task(db, task_id)
    shots = task.screenshots or []
    if index < 0 or index >= len(shots):
        raise HTTPException(status_code=404, detail="Снимок не найден")
    try:
        content = read_screenshot(str(shots[index]))
    except (OSError, ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Снимок не найден") from None
    return Response(content, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})


@router.get("/tasks/{task_id}/status", name="task_status")
def task_status(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    task = _require_task(db, task_id)
    parts = [f'<span class="status" data-task-state="{task.status.value}">{html.escape(tasks.status_label(db, task))}</span>']
    if task.error:
        parts.append(f'<div class="msg-error">{html.escape(task.error)}</div>')
    if task.record_url:
        url = html.escape(task.record_url, quote=True)
        parts.append(f'<div><a href="{url}" target="_blank" rel="noopener">Запись в СРО</a></div>')
    response = HTMLResponse("".join(parts))
    try:
        previous_status = TaskStatus(request.query_params.get("since", ""))
    except ValueError:
        previous_status = None
    if (
        request.headers.get("HX-Request", "").lower() == "true"
        and previous_status is not None
        and previous_status != task.status
    ):
        # The status fragment alone cannot reveal controls/panels that are
        # rendered for the new state (notably the unsaved SRO preview gate).
        response.headers["HX-Refresh"] = "true"
    if (
        task.status not in ACTIVE_TASK_STATUSES
        and request.headers.get("HX-Request", "").lower() == "true"
    ):
        response.status_code = 286
    return response
