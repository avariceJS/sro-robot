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

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from PIL import Image
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..db.base import get_session
from ..db.models import (
    AssignmentDraft,
    MAX_INTAKE_FILENAME_CHARACTERS,
    DebtorCardDraft,
    Document,
    IntakeItem,
    IntakeSource,
    OcrRetryRun,
    Task,
    TaskStatus,
    UploadTransportBatch,
    UploadTransportBatchItem,
    UploadTransportChunkReceipt,
    User,
)
from ..settings import settings
from ..services import bulk, case_packages, fields, mixed_intake, ocr_retries, outbox, tasks
from ..services.artifacts import (
    image_within_inline_preview_limit,
    read_document,
    read_screenshot,
    render_heic_preview,
)
from ..services.tiff_preview import (
    TIFF_PREVIEW_MAX_INPUT_BYTES,
    TIFF_PREVIEW_SLOTS,
    TiffPreviewError,
    TiffPreviewLimitError,
    TiffPreviewPageError,
    TiffPreviewTimeout,
    render_tiff_preview,
)
from ..services.fields import scenario_of
from ..services.assignments import PREPARATION_KEY
from ..observability import request_id_from_scope
from ..web.templates import templates
from sro_robot.core.ocr import (
    IMAGE_SUFFIXES,
    ImagePixelLimitExceeded,
    MAX_CONFIGURED_TIFF_FRAMES,
    UnsupportedMultiFrameImage,
)
from ..workers.queues import (
    QueueUnavailable,
    enqueue_sro_form_preview,
    enqueue_sro_preview_refresh,
)
from .deps import current_user
from .params import DatabaseId, MAX_DATABASE_ID
from .review_conflicts import review_conflict_response, review_recovery_response

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
    operation_ids = _parse_operation_ids(request.query_params.get("intake"))
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


def _parse_operation_ids(raw_intake: str | None) -> list[int] | None:
    operation_ids: list[int] | None = None
    if raw_intake is not None:
        parts = raw_intake.split(",")
        if not parts or len(parts) > MAX_INTAKE_OPERATION_IDS or any(
            _positive_dashboard_id(part) is None for part in parts
        ):
            raise HTTPException(status_code=422, detail="Некорректный список пакетов")
        operation_ids = [int(part) for part in parts]
        if len(set(operation_ids)) != len(operation_ids):
            raise HTTPException(status_code=422, detail="Некорректный список пакетов")
    return operation_ids


def _review_operation_scope(db: Session, task: Task, raw_intake: str | None) -> list[int] | None:
    operation_ids = _parse_operation_ids(raw_intake)
    if (
        not operation_ids
        or task.intake_operation_id not in operation_ids
        or task.intake_item_id is None
    ):
        return None
    item = db.get(IntakeItem, task.intake_item_id)
    if (
        item is None
        or item.operation_id != task.intake_operation_id
        or item.source not in bulk.UPLOAD_INTAKE_SOURCES
    ):
        return None
    return operation_ids


def _ocr_retry_review_scope(db: Session, task: Task, raw_operation_id: str | None) -> int | None:
    operation_id = _positive_dashboard_id(raw_operation_id)
    if (
        operation_id is None
        or task.intake_item_id is not None
        or task.intake_operation_id is not None
    ):
        return None
    if db.scalar(
        select(IntakeItem.id)
        .where(
            IntakeItem.operation_id == operation_id,
            ~IntakeItem.source.in_(bulk.UPLOAD_INTAKE_SOURCES),
        )
        .limit(1)
    ) is not None:
        return None
    item_id = db.scalar(
        select(IntakeItem.id)
        .join(OcrRetryRun, OcrRetryRun.source_intake_item_id == IntakeItem.id)
        .where(
            OcrRetryRun.child_task_id == task.id,
            OcrRetryRun.source_operation_id == operation_id,
            OcrRetryRun.document_id == task.document_id,
            IntakeItem.operation_id == OcrRetryRun.source_operation_id,
            IntakeItem.document_id == OcrRetryRun.document_id,
            IntakeItem.source.in_(bulk.UPLOAD_INTAKE_SOURCES),
        )
        .limit(1)
    )
    return operation_id if item_id is not None else None


def _scope_task_url(task_id: int, operation_ids: list[int] | None) -> str:
    url = f"/tasks/{task_id}"
    if operation_ids:
        url += "?" + urlencode({"intake": ",".join(str(value) for value in operation_ids)})
    return url


def _review_continuation_base_url(
    db: Session, task: Task, operation_ids: list[int] | None, continue_review: bool,
    ocr_review_operation_id: int | None = None,
) -> str:
    if ocr_review_operation_id is not None:
        if continue_review:
            next_task_id = bulk.next_ocr_retry_review_task_id(
                db, ocr_review_operation_id, task
            )
            if next_task_id is not None:
                return f"/tasks/{next_task_id}?" + urlencode({
                    "ocr_review_operation": ocr_review_operation_id,
                })
            return f"/intake-history/{ocr_review_operation_id}/ocr-attempts"
        return f"/tasks/{task.id}?" + urlencode({
            "ocr_review_operation": ocr_review_operation_id,
        })
    if not operation_ids:
        return f"/tasks/{task.id}"
    intake_query = ",".join(str(value) for value in operation_ids)
    if continue_review:
        next_task_id = bulk.next_upload_review_task_id(db, operation_ids, task)
        if next_task_id is not None:
            return _scope_task_url(next_task_id, operation_ids)
        return "/debtors/intake?" + urlencode({"intake": intake_query, "status": "needs_review"})
    return _scope_task_url(task.id, operation_ids)


def _review_continuation_url(db, task, operation_ids, continue_review,
                             ocr_review_operation_id=None, registry=None):
    url = _review_continuation_base_url(db, task, operation_ids, continue_review, ocr_review_operation_id)
    if registry in {"persons", "organizations", "assignments"}:
        url += ("&" if "?" in url else "?") + urlencode({"registry": registry})
    return url


def _positive_dashboard_id(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"[1-9][0-9]{0,9}", value):
        return None
    parsed = int(value)
    return parsed if parsed <= MAX_DATABASE_ID else None


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
    registry = request.query_params.get("registry")
    if registry in {"persons", "organizations", "assignments"}:
        filters["registry"] = registry
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


def _task_source_batch_work_url(db: Session, task: Task) -> str | None:
    """Link only the exact accepted manifest occurrence that created this task."""
    if task.intake_item_id is None or task.intake_operation_id is None:
        return None
    batch_id = db.scalar(
        select(UploadTransportBatch.id)
        .join(UploadTransportChunkReceipt,
              UploadTransportChunkReceipt.batch_id == UploadTransportBatch.id)
        .join(IntakeItem, and_(
            IntakeItem.operation_id == UploadTransportChunkReceipt.operation_id,
            IntakeItem.id == task.intake_item_id,
            IntakeItem.document_id == task.document_id,
        ))
        .join(UploadTransportBatchItem, and_(
            UploadTransportBatchItem.batch_id == UploadTransportBatch.id,
            UploadTransportBatchItem.chunk_index == UploadTransportChunkReceipt.chunk_index,
            UploadTransportBatchItem.file_index == (
                UploadTransportChunkReceipt.chunk_index * UploadTransportBatch.files_per_chunk
                + IntakeItem.item_index
            ),
            UploadTransportBatchItem.filename == IntakeItem.filename,
        ))
        .join(Document, and_(
            Document.id == task.document_id,
            Document.content_hash == UploadTransportBatchItem.content_sha256,
        ))
        .where(UploadTransportChunkReceipt.operation_id == task.intake_operation_id)
    )
    if batch_id is None:
        return None
    registry = mixed_intake.route_for_task(task).registry
    if registry not in mixed_intake.FOLDERS:
        return None
    return f"/upload-batches/{batch_id}/work?registry={registry}"


async def _values_from_form(request: Request, task: Task) -> dict[str, str]:
    form = await request.form()
    scenario = scenario_of(task)
    return {spec.attr: str(form.get(spec.attr, "") or "") for spec in scenario.fields}


@router.post("/tasks/{task_id}/review-recovery", name="task_restore_review_proposals")
async def task_restore_review_proposals(
    request: Request,
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Reopen current review values and show stale proposals without saving them."""
    task = _require_task(db, task_id)
    form = await request.form()
    submitted_values = await _values_from_form(request, task)
    operation_ids = _review_operation_scope(
        db, task, str(form.get("intake", "")) or None
    )
    ocr_review_operation_id = _ocr_retry_review_scope(
        db, task, str(form.get("ocr_review_operation", "")) or None
    )
    return review_recovery_response(
        request, task, user, db, submitted_values,
        operation_ids=operation_ids,
        ocr_review_operation_id=ocr_review_operation_id,
    )


@router.get("/debtors/intake", name="tasks_page")
def tasks_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    # The dedicated mixed entry is upload-only; its exact folders are created
    # from batch receipts after intake, so no unrelated task worklist is read.
    dashboard = {} if request.query_params.get("registry") == "mixed" else _dashboard_context(db, request)
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
            "recognition_only": settings.sro_recognition_only,
        },
    )


@router.api_route("/intake-history", methods=["GET", "POST"], name="intake_history_page")
def intake_history_page_route(
    request: Request,
    filename_form: str = Form(default="", alias="filename"),
    page_form: str = Form(default="1", alias="page"),
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    filename_query = ""
    if request.method == "POST":
        filename_query = filename_form.strip()
        if len(filename_query) > MAX_INTAKE_FILENAME_CHARACTERS:
            raise HTTPException(status_code=422, detail="Поисковая строка слишком длинная")
        raw_page = page_form
    else:
        raw_page = request.query_params.get("page", "1")
    page = _positive_dashboard_id(raw_page)
    if page is None or page > MAX_PAGE_NUMBER:
        raise HTTPException(status_code=422, detail="Некорректная страница журнала загрузок")
    history = bulk.intake_history_page(db, page, filename_query)
    recovery_state = request.query_params.get("recovery", "")
    if recovery_state not in {"", "none", "queued", "pending", "started", "mixed"}:
        recovery_state = ""
    try:
        recovery_queued = max(0, min(100, int(request.query_params.get("queued", "0"))))
        recovery_pending = max(0, min(100, int(request.query_params.get("pending", "0"))))
        recovery_started = max(0, min(100, int(request.query_params.get("started", "0"))))
    except ValueError:
        recovery_queued = recovery_pending = recovery_started = 0
    return templates.TemplateResponse(
        request,
        "intake_history.html",
        {
            "user": user,
            **history,
            "recovery_state": recovery_state,
            "recovery_queued": recovery_queued,
            "recovery_pending": recovery_pending,
            "recovery_started": recovery_started,
        },
    )


@router.get(
    "/intake-history/{operation_id}/ocr-attempts",
    name="intake_ocr_attempts_page",
)
def intake_ocr_attempts_page_route(
    operation_id: DatabaseId,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    try:
        context = bulk.intake_ocr_attempts(db, operation_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Загрузка не найдена") from None
    return templates.TemplateResponse(
        request,
        "intake_ocr_attempts.html",
        {"user": user, **context},
    )


@router.get(
    "/intake-history/{operation_id}/ocr-reviews",
    name="intake_ocr_retry_reviews_page",
)
def intake_ocr_retry_reviews_page_route(
    operation_id: DatabaseId,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    raw_page = request.query_params.get("page", "1")
    page = _positive_dashboard_id(raw_page)
    if page is None or page > MAX_PAGE_NUMBER:
        raise HTTPException(status_code=422, detail="Некорректная страница проверки OCR")
    try:
        context = bulk.intake_ocr_retry_review_queue(db, operation_id, page)
    except LookupError:
        raise HTTPException(status_code=404, detail="Загрузка OCR не найдена") from None
    return templates.TemplateResponse(
        request,
        "intake_ocr_retry_reviews.html",
        {"user": user, **context},
    )


@router.get(
    "/intake-history/{operation_id}/ocr-attempts/{item_id}",
    name="intake_ocr_attempt_chain_page",
)
def intake_ocr_attempt_chain_page_route(
    operation_id: DatabaseId,
    item_id: DatabaseId,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    raw_after = request.query_params.get("after")
    after_task_id = None
    if raw_after is not None:
        after_task_id = _positive_dashboard_id(raw_after)
        if after_task_id is None:
            raise HTTPException(status_code=422, detail="Некорректная страница истории OCR")
    try:
        context = bulk.intake_ocr_attempt_chain(
            db,
            operation_id=operation_id,
            item_id=item_id,
            after_task_id=after_task_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Поступление или попытка не найдены") from None
    return templates.TemplateResponse(
        request,
        "intake_ocr_attempt_chain.html",
        {"user": user, **context},
    )


@router.post("/intake-history/{operation_id}/recover", name="recover_intake_operation")
def recover_intake_operation_route(
    request: Request,
    operation_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    try:
        intent_ids = bulk.prepare_intake_recovery(
            db,
            operation_id=operation_id,
            user_id=user.id,
            request_id=request_id_from_scope(request.scope),
        )
        db.commit()
    except LookupError:
        db.rollback()
        raise HTTPException(status_code=404, detail="Загрузка не найдена") from None
    except Exception:
        db.rollback()
        raise

    if not intent_ids:
        return RedirectResponse("/intake-history?recovery=none", status_code=303)

    confirmed = 0
    pending = 0
    already_started = 0
    for intent_id in intent_ids:
        try:
            outcome = outbox.dispatch_intent(db, intent_id)
            if outcome == outbox.DispatchOutcome.CONFIRMED:
                confirmed += 1
            elif outcome == outbox.DispatchOutcome.ALREADY_STARTED:
                already_started += 1
            else:
                pending += 1
        except QueueUnavailable:
            # The durable outbox retains uncertain/unavailable work. Never
            # report it as confirmed until Redis accepted the delivery.
            pending += 1
    nonzero_outcomes = sum(bool(count) for count in (confirmed, pending, already_started))
    if nonzero_outcomes > 1:
        state = "mixed"
    elif confirmed:
        state = "queued"
    elif already_started:
        state = "started"
    else:
        state = "pending"
    return RedirectResponse(
        f"/intake-history?recovery={state}&queued={confirmed}&pending={pending}&started={already_started}",
        status_code=303,
    )


@router.get("/", name="home_page")
def home_page(user: User = Depends(current_user)):
    """Start staff on the debtor worklist rather than the technical queue."""
    return RedirectResponse("/debtors", status_code=303)


@router.get("/tasks/{task_id}/document", name="task_document")
def task_document(
    task_id: DatabaseId,
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
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
    page: int = Query(1, ge=1, le=MAX_CONFIGURED_TIFF_FRAMES),
    raster: bool = Query(False),
) -> Response:
    """Render supported source documents inline after private hash verification."""
    task = _require_task(db, task_id)
    document = task.document
    if document is None:
        raise HTTPException(status_code=404, detail="Исходный документ не найден")
    document_suffix = Path(str(document.stored_path)).suffix.casefold()
    read_limit = settings.upload_max_file_bytes
    if document_suffix in {".tif", ".tiff"}:
        read_limit = min(read_limit, TIFF_PREVIEW_MAX_INPUT_BYTES)
    try:
        content, media_type, suffix = read_document(
            str(document.stored_path),
            str(document.content_hash),
            settings.upload_dir,
            read_limit,
        )
    except (OSError, ValueError, TypeError):
        if document_suffix in {".tif", ".tiff"}:
            raise HTTPException(
                status_code=415,
                detail="Предпросмотр TIFF недоступен из-за формата или размера. Скачайте оригинал.",
            ) from None
        raise HTTPException(status_code=404, detail="Исходный документ недоступен") from None
    if suffix.casefold() == ".pdf" and raster is True:
        from ..services.pdf_preview import render_pdf_preview
        if not TIFF_PREVIEW_SLOTS.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="Предпросмотр занят", headers={"Retry-After": "2"})
        try:
            try:
                preview, page_count = render_pdf_preview(content, page_number=page, max_pages=settings.ocr_max_pdf_pages)
            except IndexError:
                raise HTTPException(status_code=416, detail="Страница документа не найдена") from None
            except Exception:
                raise HTTPException(status_code=422, detail="Предпросмотр PDF недоступен. Скачайте оригинал.") from None
        finally:
            TIFF_PREVIEW_SLOTS.release()
        return Response(preview, media_type="image/png", headers={
            "Content-Disposition": 'inline; filename="source-document-page.png"',
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
            "X-Document-Page": str(page), "X-Document-Page-Count": str(page_count),
        })
    if suffix.casefold() in {".tif", ".tiff"}:
        if not TIFF_PREVIEW_SLOTS.acquire(blocking=False):
            raise HTTPException(
                status_code=503,
                detail="Предпросмотр страницы временно занят. Повторите попытку через несколько секунд.",
                headers={"Retry-After": "2"},
            )
        try:
            try:
                preview, page_count = render_tiff_preview(
                    content,
                    page_number=page,
                    max_frames=settings.ocr_max_tiff_frames,
                    max_image_pixels=settings.ocr_max_image_pixels,
                )
            except TiffPreviewPageError:
                raise HTTPException(status_code=416, detail="Страница документа не найдена") from None
            except TiffPreviewLimitError:
                raise HTTPException(
                    status_code=415,
                    detail="Предпросмотр TIFF превышает ограничения безопасного декодирования. Скачайте оригинал.",
                ) from None
            except TiffPreviewTimeout:
                raise HTTPException(
                    status_code=504,
                    detail="Предпросмотр страницы TIFF занял слишком много времени. Скачайте оригинал.",
                ) from None
            except TiffPreviewError:
                raise HTTPException(
                    status_code=422,
                    detail="Предпросмотр этой страницы TIFF недоступен. Скачайте оригинал.",
                ) from None
        finally:
            TIFF_PREVIEW_SLOTS.release()
        return Response(
            preview,
            media_type="image/png",
            headers={
                "Content-Disposition": 'inline; filename="source-document-page.png"',
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Content-Type-Options": "nosniff",
                "X-Document-Page": str(page),
                "X-Document-Page-Count": str(page_count),
            },
        )
    if suffix.casefold() == ".heic":
        if page != 1:
            raise HTTPException(status_code=416, detail="Страница HEIC не найдена")
        try:
            preview, dimensions = render_heic_preview(
                content, max_image_pixels=settings.ocr_max_image_pixels,
            )
        except (ImagePixelLimitExceeded, UnsupportedMultiFrameImage):
            raise HTTPException(
                status_code=415,
                detail="Предпросмотр HEIC превышает ограничения безопасного декодирования. Скачайте оригинал.",
            ) from None
        except (OSError, ValueError, TypeError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise HTTPException(
                status_code=422,
                detail="Предпросмотр HEIC недоступен для этого файла. Скачайте оригинал.",
            ) from None
        return Response(
            preview,
            media_type="image/jpeg",
            headers={
                "Content-Disposition": 'inline; filename="source-document.jpg"',
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Content-Type-Options": "nosniff",
                "X-Document-Page": "1",
                "X-Document-Page-Count": "1",
                "X-Document-Image-Width": str(dimensions[0]),
                "X-Document-Image-Height": str(dimensions[1]),
            },
        )
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
                    detail="Поддерживаются PDF, JPEG, PNG, HEIC, TIFF, BMP и WebP",
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
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    task = _require_task(db, task_id)
    review_operation_ids = _review_operation_scope(
        db, task, request.query_params.get("intake")
    )
    ocr_review_operation_id = _ocr_retry_review_scope(
        db, task, request.query_params.get("ocr_review_operation")
    )
    internal_card = db.query(DebtorCardDraft).filter_by(source_task_id=task.id).one_or_none()
    internal_assignment = db.query(AssignmentDraft).filter_by(source_task_id=task.id).one_or_none()
    preparation = (task.recognized or {}).get(PREPARATION_KEY) or {}
    review_fields = fields.review_fields(task)
    alternate_inn = fields.alternate_person_inn(task, review_fields)
    refresh_eligibility = (
        tasks.mixed_refresh_eligibility(db, task, user.id)
        if task.preview_mode == "durable_closed" and not settings.sro_recognition_only
        else {"eligible": False, "reason": "preview_not_ready"}
    )
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "user": user,
            "task": task,
            "case_packages": case_packages.active_case_packages_for_task(db, task.id),
            "document_preview_kind": (
                Path(str(getattr(task.document, "stored_path", ""))).suffix.lower()
                if task.document is not None else ""
            ),
            "fields": review_fields,
            "alternate_inn": alternate_inn,
            "has_review_uncertainty": any(
                getattr(field, "warning", "") or getattr(field, "uncertain", False)
                for field in review_fields
            ),
            "review_suggestions": (task.recognized or {}).get(tasks.REVIEW_SUGGESTIONS_KEY, []),
            "TaskStatus": TaskStatus,
            "status_ru": tasks.status_label(db, task),
            "internal_card": internal_card,
            "internal_assignment": internal_assignment,
            "approval_current": bool(
                task.status == TaskStatus.approved and tasks.approval_matches(db, task)
            ),
            "preparation_reason": preparation.get("reason", ""),
            "review_token": tasks.review_token(task),
            "preview_token": tasks.preview_token(task) if task.status == TaskStatus.awaiting_preview else "",
            "preview_can_save": (
                task.preview_mode != "durable_closed"
                and not settings.sro_recognition_only
                and not settings.sro_existing_card_test_mode
                and task.status == TaskStatus.awaiting_preview
                and tasks.can_save_preview(db, task, user.id)
            ),
            "preview_can_refresh": bool(refresh_eligibility["eligible"]),
            "preview_refresh_reason": refresh_eligibility["reason"],
            "preview_refresh_key": secrets.token_hex(24),
            "preview_refresh_state": getattr(task, "preview_refresh_state", "idle") or "idle",
            "preview_can_retry_refresh": bool(
                not settings.sro_recognition_only
                and
                task.preview_mode == "durable_closed"
                and task.status == TaskStatus.awaiting_preview
                and task.preview_refresh_state == "requested"
                and task.preview_refresh_actor_id == user.id
                and task.preview_refresh_request_id
                and not task.browser_decision and not task.preview_save_enabled
            ),
            "preview_refresh_notice": request.query_params.get("refresh", ""),
            "recognition_only": settings.sro_recognition_only,
            "mixed_batch_work_url": _task_source_batch_work_url(db, task),
            "existing_card_test_mode": settings.sro_existing_card_test_mode,
            "internal_card_error": request.query_params.get("internal_card", ""),
            "ocr_retry_context": ocr_retries.context_for_task(db, task),
            "ocr_retry_notice": request.query_params.get("ocr_retry", ""),
            "review_operation_ids": review_operation_ids or [],
            "review_intake_query": ",".join(str(value) for value in review_operation_ids or []),
            "ocr_review_operation_id": ocr_review_operation_id,
        },
    )


@router.post("/tasks/{task_id}/refresh-owned-preview", name="task_refresh_owned_preview")
async def task_refresh_owned_preview(
    request: Request,
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    """Employee edits one bound draft; only same-GUID preview work is queued."""
    if settings.sro_recognition_only:
        raise HTTPException(status_code=409, detail="Локальный режим: только распознавание")
    task = _require_task(db, task_id)
    form = await request.form()
    refresh_key = str(form.get("refresh_key", ""))
    if re.fullmatch(r"[0-9a-f]{48}", refresh_key) is None:
        raise HTTPException(status_code=422, detail="Обновите задание перед изменением полей")
    expected_token = str(form.get("preview_token", ""))
    values = await _values_from_form(request, task)
    request_id = hashlib.sha256(
        f"sro-robot/owned-preview-refresh/v1:{task.id}:{user.id}:{refresh_key}".encode()
    ).hexdigest()[:32]
    try:
        requested = tasks.request_owned_preview_refresh(
            db, task, user.id, expected_token, values, request_id,
        )
        if not requested:
            db.rollback()
            raise HTTPException(status_code=409, detail="Снимки или поля изменились; обновите задание")
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise
    try:
        enqueue_sro_preview_refresh(task.id, user.id, request_id)
    except QueueUnavailable:
        return RedirectResponse(f"/tasks/{task.id}?refresh=queue_uncertain", status_code=303)
    return RedirectResponse(f"/tasks/{task.id}?refresh=requested", status_code=303)


@router.post("/tasks/{task_id}/retry-owned-preview-refresh", name="task_retry_owned_preview_refresh")
async def task_retry_owned_preview_refresh(
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    """Redeliver only an unclaimed refresh intent; never start a second Add."""
    if settings.sro_recognition_only:
        raise HTTPException(status_code=409, detail="Локальный режим: только распознавание")
    task = _require_task(db, task_id)
    if (task.preview_mode != "durable_closed" or task.status != TaskStatus.awaiting_preview
            or task.preview_refresh_state != "requested"
            or task.preview_refresh_actor_id != user.id
            or not task.preview_refresh_request_id
            or task.browser_decision or task.preview_save_enabled):
        raise HTTPException(status_code=409, detail="Повтор обновления снимков недоступен")
    try:
        enqueue_sro_preview_refresh(task.id, user.id, task.preview_refresh_request_id)
    except QueueUnavailable:
        return RedirectResponse(f"/tasks/{task.id}?refresh=queue_uncertain", status_code=303)
    return RedirectResponse(f"/tasks/{task.id}?refresh=requested", status_code=303)


@router.post("/tasks/{task_id}/retry-ocr", name="task_retry_ocr")
async def task_retry_ocr(
    request: Request,
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    raw_witness = str(form.get("failure_history_id", ""))
    if not re.fullmatch(r"[1-9][0-9]{0,9}", raw_witness):
        raise HTTPException(status_code=422, detail="Не подтверждена запись ошибки OCR")
    witness_id = int(raw_witness)
    if witness_id > MAX_DATABASE_ID:
        raise HTTPException(status_code=422, detail="Некорректная запись ошибки OCR")
    try:
        run, child, intent_id, created = ocr_retries.request_retry(
            db,
            parent_task_id=task_id,
            failure_history_id=witness_id,
            user_id=user.id,
            request_id=_server_request_id(request),
        )
        db.commit()
    except ocr_retries.OcrRetrySourceUnavailable:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Оригинал недоступен или не совпадает с сохранённой контрольной суммой; новая попытка не создана.",
        ) from None
    except ocr_retries.OcrRetryConflict:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Задание изменилось или указанная ошибка больше не является подтверждённым сбоем OCR.",
        ) from None
    except ocr_retries.OcrRetryNotEligible:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Для этого задания нельзя безопасно запустить повтор OCR.",
        ) from None
    except LookupError:
        db.rollback()
        raise HTTPException(status_code=404, detail="Задание не найдено") from None
    except Exception:
        db.rollback()
        raise

    if not created and intent_id is None:
        intent_id = outbox.restore_ocr_retry_intent(db, run)
        db.commit()
    outcome = outbox.DispatchOutcome.DEFERRED
    if intent_id is not None:
        try:
            outcome = outbox.dispatch_intent(db, intent_id)
        except QueueUnavailable:
            outcome = outbox.DispatchOutcome.DEFERRED
    state = (
        "queued"
        if outcome == outbox.DispatchOutcome.CONFIRMED
        else "started"
        if outcome == outbox.DispatchOutcome.ALREADY_STARTED
        else "pending"
    )
    if not created and child.status != TaskStatus.new:
        state = "existing"
    target = f"/tasks/{child.id}?ocr_retry={state}"
    registry = request.query_params.get("registry")
    if registry in {"persons", "organizations", "assignments"}:
        target += "&" + urlencode({"registry": registry})
    return RedirectResponse(target, status_code=303)


@router.post("/tasks/{task_id}/save", name="task_save")
async def task_save(
    request: Request,
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    task = _require_task(db, task_id)
    form = await request.form()
    values = await _values_from_form(request, task)
    review_operation_ids = _review_operation_scope(
        db, task, str(form.get("intake", "")) or None
    )
    ocr_review_operation_id = _ocr_retry_review_scope(
        db, task, str(form.get("ocr_review_operation", "")) or None
    )
    continue_review = str(form.get("continue_review", "")) == "1"
    request_id = _server_request_id(request)
    try:
        tasks.assert_review_token(db, task, str(form.get("review_token", "")))
    except ValueError:
        if tasks.saved_review_replay_matches(db, task, values, user.id):
            db.commit()
            return RedirectResponse(
                _review_continuation_url(
                    db, task, review_operation_ids, continue_review, ocr_review_operation_id,
                    registry=request.query_params.get("registry"),
                ),
                status_code=303,
            )
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="save_fields",
            user_id=user.id,
            request_id=request_id,
        )
        db.commit()
        return review_conflict_response(
            request, task, user, db, values, operation_ids=review_operation_ids,
            ocr_review_operation_id=ocr_review_operation_id,
        )
    try:
        tasks.save_values(
            db,
            task,
            values,
            user.id,
            request_id=request_id,
        )
    except ValueError:
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="save_fields",
            user_id=user.id,
            request_id=request_id,
        )
        db.commit()
        return review_conflict_response(
            request, task, user, db, values, operation_ids=review_operation_ids,
            ocr_review_operation_id=ocr_review_operation_id,
        )
    db.commit()
    return RedirectResponse(
        _review_continuation_url(
            db, task, review_operation_ids, continue_review, ocr_review_operation_id,
                    registry=request.query_params.get("registry"),
        ),
        status_code=303,
    )


@router.post("/tasks/{task_id}/approve", name="task_approve")
async def task_approve(
    request: Request,
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    task = _require_task(db, task_id)
    form = await request.form()
    values = await _values_from_form(request, task)
    review_operation_ids = _review_operation_scope(
        db, task, str(form.get("intake", "")) or None
    )
    ocr_review_operation_id = _ocr_retry_review_scope(
        db, task, str(form.get("ocr_review_operation", "")) or None
    )
    continue_review = str(form.get("continue_review", "")) == "1"
    request_id = _server_request_id(request)
    try:
        tasks.assert_review_token(db, task, str(form.get("review_token", "")))
    except ValueError:
        if tasks.approved_review_replay_matches(db, task, values, user.id):
            db.commit()
            return RedirectResponse(
                _review_continuation_url(
                    db, task, review_operation_ids, continue_review, ocr_review_operation_id,
                    registry=request.query_params.get("registry"),
                ),
                status_code=303,
            )
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="approve",
            user_id=user.id,
            request_id=request_id,
        )
        db.commit()
        return review_conflict_response(
            request, task, user, db, values, operation_ids=review_operation_ids,
            ocr_review_operation_id=ocr_review_operation_id,
        )
    try:
        tasks.save_values(db, task, values, user.id, request_id=request_id)
        tasks.approve(db, task, user.id, request_id=request_id)
    except ValueError:
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="approve",
            user_id=user.id,
            request_id=request_id,
        )
        db.commit()
        return review_conflict_response(
            request, task, user, db, values, operation_ids=review_operation_ids,
            ocr_review_operation_id=ocr_review_operation_id,
        )
    db.commit()
    return RedirectResponse(
        _review_continuation_url(
            db, task, review_operation_ids, continue_review, ocr_review_operation_id,
                    registry=request.query_params.get("registry"),
        ),
        status_code=303,
    )


@router.get("/tasks/{task_id}/confirm-reject", name="task_confirm_reject")
def task_confirm_reject(
    request: Request,
    task_id: DatabaseId,
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
    task_id: DatabaseId,
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
    task_id: DatabaseId,
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
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    """Queue form filling and screenshots; Save requires a separate preview decision."""
    if settings.sro_recognition_only:
        raise HTTPException(status_code=409, detail="Локальный режим: только распознавание")
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
    task_id: DatabaseId,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    """Ответ оператора роботу, который стоит на заполненной форме."""
    if settings.sro_recognition_only:
        raise HTTPException(status_code=409, detail="Локальный режим: только распознавание")
    task = _require_task(db, task_id)
    form = await request.form()
    decision = str(form.get("decision", "close"))
    if decision not in {"save", "close"}:
        tasks.log_operator_action_not_applied(
            db,
            task,
            action="preview_decision",
            user_id=user.id,
            request_id=_server_request_id(request),
        )
        db.commit()
        raise HTTPException(status_code=409, detail="Выберите сохранить карточку или закрыть форму")
    if decision == "save" and task.preview_mode == "durable_closed":
        raise HTTPException(
            status_code=409,
            detail="Сохраните подготовленную карточку из папки партии после проверки полей и снимков.",
        )
    if decision == "save" and settings.sro_existing_card_test_mode:
        raise HTTPException(
            status_code=409,
            detail="Тестовый режим существующих карточек включён: сохранение в СРОАрбитр отключено.",
        )
    request_id = _server_request_id(request)
    if tasks.decide_preview(db, task, decision, user.id, request_id,
                            expected_token=str(form.get("preview_token", ""))):
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
        if decision == "save":
            return HTMLResponse(
                '<p>Снимки устарели, форма закрыта или решение уже принято.</p>'
                f'<p><a href="/tasks/{int(task.id)}">Вернуться к заданию и проверить состояние</a></p>',
                status_code=409,
            )
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.get("/tasks/{task_id}/shot/{index}", name="task_shot")
def task_shot(
    task_id: DatabaseId,
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
    task_id: DatabaseId,
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
    if (request.headers.get("HX-Request", "").lower() == "true"
            and "decision" in request.query_params
            and request.query_params["decision"] != (task.browser_decision or "")):
        response.headers["HX-Refresh"] = "true"
    if (
        task.status not in ACTIVE_TASK_STATUSES
        and request.headers.get("HX-Request", "").lower() == "true"
    ):
        response.status_code = 286
    return response
