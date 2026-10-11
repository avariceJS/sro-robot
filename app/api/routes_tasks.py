"""Задания: список, загрузка, проверка полей."""

from __future__ import annotations

import html
import logging
import re
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile
from sqlalchemy.orm import Session

from ..db.base import get_session
from ..db.models import IntakeSource, Task, TaskStatus, User
from ..capabilities import KAD_ACQUISITION_ENABLED
from ..settings import settings
from ..services import fields, outbox, tasks
from ..services.artifacts import read_screenshot
from ..services.fields import scenario_of
from ..observability import request_id_from_scope
from sro_robot.core.validators import normalize_case_number
from ..web.templates import templates
from ..workers.queues import (
    QueueUnavailable,
)
from .deps import current_user

router = APIRouter(tags=["tasks"])
logger = logging.getLogger(__name__)


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


@router.get("/", name="tasks_page")
def tasks_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    recent_tasks = tasks.recent(db)
    status_labels = tasks.status_labels(db, recent_tasks)
    task_rows = [
        {
            "task": task,
            "summary": fields.summary(task),
            "status_ru": status_labels[task.id],
        }
        for task in recent_tasks
    ]
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "user": user,
            "task_rows": task_rows,
            "upload_idempotency_key": secrets.token_hex(24),
            "kad_acquisition_enabled": KAD_ACQUISITION_ENABLED,
        },
    )


@router.post("/upload", name="upload_documents")
async def upload_documents(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
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
            accepted.append((upload.filename or "document", content))

    source = IntakeSource.upload if len(accepted) == 1 else IntakeSource.batch
    manifest = tasks.intake_manifest(
        source.value,
        [hashlib.sha256(content).hexdigest() for _filename, content in accepted],
    )
    try:
        operation, is_new = tasks.begin_intake_operation(
            db,
            user_id=user.id,
            idempotency_key=idempotency_key,
            manifest_sha256=manifest,
        )
    except tasks.IntakeOperationConflict as exc:
        raise HTTPException(status_code=409, detail="Ключ запроса уже использован для другого ввода") from exc

    if is_new:
        operation_tasks = []
        for filename, content in accepted:
            try:
                document = tasks.store_document(db, filename, content, source, user.id)
            except tasks.ExistingDocumentUnavailable as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Хранилище документа недоступно; повторите запрос после проверки",
                ) from exc
            task = tasks.create_task(
                db,
                document,
                user_id=user.id,
                request_id=request_id,
                intake_operation_id=operation.id,
            )
            operation_tasks.append(task)
    else:
        operation_tasks = tasks.operation_tasks(db, operation.id)
        if not operation_tasks:
            raise RuntimeError("Accepted intake operation has no linked tasks")
        tasks.log_intake_replay(db, operation_tasks, user.id, request_id)

    intent_ids = [
        outbox.create_intake_intent(
            db,
            task_id=task.id,
            operation_id=operation.id,
            job_kind="ocr",
        )
        for task in operation_tasks
    ]

    db.commit()
    try:
        for intent_id in intent_ids:
            outbox.dispatch_required(db, intent_id)
    except QueueUnavailable as exc:
        raise _queue_unavailable(exc, request_id) from exc
    return RedirectResponse("/", status_code=303)


def _server_request_id(request: Request) -> str | None:
    """Return only the opaque request ID generated by RequestIdMiddleware."""
    return request_id_from_scope(request.scope)


@router.post("/case-search", name="search_case_in_kad")
async def search_case_in_kad(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    if not KAD_ACQUISITION_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Получение актов из КАД отключено до проверки безопасного правила выбора акта",
        )
    async with request.form(max_files=0, max_fields=2) as form:
        case_number = normalize_case_number(str(form.get("case_number", "")))
        idempotency_key = str(form.get("idempotency_key", ""))
    if not re.fullmatch(r"[0-9a-f]{48}", idempotency_key):
        raise HTTPException(status_code=422, detail="Обновите страницу перед поиском")
    if not re.fullmatch(r"[А-ЯЁ]\d{1,3}-\d+/\d{2,4}", case_number):
        raise HTTPException(status_code=422, detail="Введите номер дела, например А99-12345/2099")
    request_id = _server_request_id(request)
    manifest = tasks.intake_manifest(
        "kad", [hashlib.sha256(case_number.encode("utf-8")).hexdigest()]
    )
    try:
        operation, is_new = tasks.begin_intake_operation(
            db,
            user_id=user.id,
            idempotency_key=idempotency_key,
            manifest_sha256=manifest,
        )
    except tasks.IntakeOperationConflict as exc:
        raise HTTPException(status_code=409, detail="Ключ запроса уже использован для другого ввода") from exc

    if is_new:
        operation_tasks = [
            tasks.create_kad_task(
                db,
                case_number,
                user.id,
                request_id=request_id,
                intake_operation_id=operation.id,
            )
        ]
    else:
        operation_tasks = tasks.operation_tasks(db, operation.id)
        if len(operation_tasks) != 1:
            raise RuntimeError("Accepted KAD operation must link exactly one task")
        tasks.log_intake_replay(db, operation_tasks, user.id, request_id)
    intent_id = outbox.create_intake_intent(
        db,
        task_id=operation_tasks[0].id,
        operation_id=operation.id,
        job_kind="kad",
    )
    db.commit()
    try:
        outbox.dispatch_required(db, intent_id)
    except QueueUnavailable as exc:
        raise _queue_unavailable(exc, request_id) from exc
    return RedirectResponse(f"/tasks/{operation_tasks[0].id}", status_code=303)


@router.get("/tasks/{task_id}", name="task_page")
def task_page(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    task = _require_task(db, task_id)
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "user": user,
            "task": task,
            "fields": fields.review_fields(task),
            "TaskStatus": TaskStatus,
            "status_ru": tasks.status_label(db, task),
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
    values = await _values_from_form(request, task)
    request_id = _server_request_id(request)
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
    values = await _values_from_form(request, task)
    request_id = _server_request_id(request)
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
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="local_validation",
            user_id=user.id,
            request_id=_server_request_id(request),
        )
        db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.get("/tasks/{task_id}/confirm-save", name="task_confirm_save")
def task_confirm_save(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Отдельный экран подтверждения: следующий шаг пишет в реальный реестр."""
    task = _require_task(db, task_id)
    return templates.TemplateResponse(
        request,
        "confirm_save.html",
        {"user": user, "task": task, "fields": fields.review_fields(task)},
    )


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
    if decision not in ("save", "close"):
        decision = "close"
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
    task_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    task = _require_task(db, task_id)
    parts = [f'<span class="status">{html.escape(tasks.status_label(db, task))}</span>']
    if task.error:
        parts.append(f'<div class="msg-error">{html.escape(task.error)}</div>')
    if task.record_url:
        url = html.escape(task.record_url, quote=True)
        parts.append(f'<div><a href="{url}" target="_blank" rel="noopener">Запись в СРО</a></div>')
    return HTMLResponse("".join(parts))
