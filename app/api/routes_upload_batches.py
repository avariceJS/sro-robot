"""Shared, resumable multipart transport manifests."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..db.base import get_session
from ..db.models import (
    Document,
    IntakeSource,
    IntakeOperation,
    UploadTransportBatch,
    UploadTransportChunkReceipt,
    User,
)
from ..services import outbox, tasks, upload_transport
from ..settings import settings
from ..workers.queues import QueueUnavailable
from .deps import current_user
from .routes_tasks import SUPPORTED_UPLOAD_SUFFIXES, _queue_unavailable, _server_request_id


router = APIRouter(tags=["upload batches"])
logger = logging.getLogger(__name__)
MAX_MANIFEST_BODY_BYTES = 128 * 1024


def _validate_manifest(data: object) -> tuple[str, int, list[dict]]:
    if not isinstance(data, dict) or set(data) != {"request_key", "files_per_chunk", "files"}:
        raise HTTPException(status_code=422, detail="Некорректный план загрузки")
    request_key = data["request_key"]
    files_per_chunk = data["files_per_chunk"]
    files = data["files"]
    if not isinstance(request_key, str) or re.fullmatch(r"[0-9a-f]{48}", request_key) is None:
        raise HTTPException(status_code=422, detail="Обновите страницу перед загрузкой")
    if type(files_per_chunk) is not int or not 1 <= files_per_chunk <= min(100, settings.upload_max_files):
        raise HTTPException(status_code=422, detail="Некорректный размер части загрузки")
    if not isinstance(files, list) or not 1 <= len(files) <= settings.upload_max_batch_files:
        raise HTTPException(status_code=422, detail="Некорректное число файлов в партии")
    normalized = []
    total_bytes = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"filename", "size", "sha256"}:
            raise HTTPException(status_code=422, detail="Некорректное описание файла")
        filename, size, digest = item["filename"], item["size"], item["sha256"]
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > 255
            or "\x00" in filename
            or not Path(filename).suffix.casefold() in SUPPORTED_UPLOAD_SUFFIXES
        ):
            raise HTTPException(status_code=422, detail="Недопустимое имя или тип файла")
        if type(size) is not int or not 1 <= size <= settings.upload_max_file_bytes:
            raise HTTPException(status_code=413, detail="Размер файла превышает допустимый предел")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise HTTPException(status_code=422, detail="Некорректная контрольная сумма файла")
        total_bytes += size
        if total_bytes > settings.upload_max_batch_bytes:
            raise HTTPException(status_code=413, detail="Пакет превышает допустимый размер")
        normalized.append({"filename": filename, "size": size, "sha256": digest})
    return request_key, files_per_chunk, normalized


@router.post("/upload-batches", name="register_upload_batch")
async def register_upload_batch(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Ожидается JSON манифест партии")
    body = await request.body()
    if len(body) > MAX_MANIFEST_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Манифест партии слишком велик")
    try:
        request_key, files_per_chunk, files = _validate_manifest(await request.json())
        batch, created = upload_transport.register_batch(
            db,
            creator_id=user.id,
            request_key=request_key,
            files_per_chunk=files_per_chunk,
            files=files,
        )
    except upload_transport.UploadTransportConflict as exc:
        raise HTTPException(status_code=409, detail="Ключ регистрации уже связан с другим планом") from exc
    db.commit()
    return JSONResponse(
        upload_transport.public_batch_state(db, batch, include_files=True),
        status_code=201 if created else 200,
    )


@router.get("/upload-batches", name="list_upload_batches")
def list_upload_batches(
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
    page: int = Query(default=0, ge=0, le=1000),
):
    del user
    batches = list(
        db.scalars(
            select(UploadTransportBatch)
            .order_by(UploadTransportBatch.created_at.desc(), UploadTransportBatch.id.desc())
            .offset(page * 50)
            .limit(51)
        )
    )
    has_more = len(batches) > 50
    states = [upload_transport.public_batch_state(db, batch) for batch in batches[:50]]
    return JSONResponse({"batches": states, "page": page, "has_more": has_more})


@router.get("/upload-batches/{batch_id}", name="get_upload_batch")
def get_upload_batch(
    batch_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    del user
    if re.fullmatch(r"[0-9a-f]{32}", batch_id) is None:
        raise HTTPException(status_code=404, detail="Партия загрузки не найдена")
    batch = db.get(UploadTransportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Партия загрузки не найдена")
    return JSONResponse(upload_transport.public_batch_state(db, batch, include_files=True))


@router.post("/upload-batches/{batch_id}/chunks/{chunk_index}", name="upload_batch_chunk")
async def upload_batch_chunk(
    batch_id: str,
    chunk_index: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
):
    if re.fullmatch(r"[0-9a-f]{32}", batch_id) is None or not 0 <= chunk_index < 100:
        raise HTTPException(status_code=404, detail="Часть партии не найдена")
    batch = db.get(UploadTransportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Партия загрузки не найдена")
    planned = upload_transport.chunk_items(db, batch.id, chunk_index)
    if not planned:
        raise HTTPException(status_code=404, detail="Часть партии не найдена")
    if batch.files_per_chunk > settings.upload_max_files or any(
        item.file_size > settings.upload_max_file_bytes for item in planned
    ) or sum(item.file_size for item in planned) > min(
        settings.upload_max_batch_bytes, settings.upload_max_request_bytes
    ):
        raise HTTPException(status_code=409, detail="Текущие лимиты сервера не позволяют принять эту часть; ранее принятые части сохранены")

    async with request.form(max_files=settings.upload_max_files + 1, max_fields=0) as form:
        uploads = form.getlist("files")
        if not uploads or len(uploads) != len(planned) or any(
            not isinstance(upload, StarletteUploadFile) for upload in uploads
        ):
            raise HTTPException(status_code=422, detail="Выберите все файлы этой части в исходном порядке")
        accepted: list[tuple[str, bytes, str]] = []
        for upload, item in zip(uploads, planned, strict=True):
            filename = upload.filename or ""
            content = await upload.read(settings.upload_max_file_bytes + 1)
            digest = hashlib.sha256(content).hexdigest()
            if filename != item.filename or len(content) != item.file_size or digest != item.content_sha256:
                raise HTTPException(status_code=409, detail="Файлы не совпадают с исходным планом партии")
            accepted.append((filename, content, digest))

    # The request bytes are fully read and checked before the batch row lock.
    batch = db.scalar(
        select(UploadTransportBatch)
        .where(UploadTransportBatch.id == batch_id)
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Партия загрузки не найдена")
    receipt = db.get(UploadTransportChunkReceipt, (batch.id, chunk_index))
    if receipt is not None:
        operation = db.get(IntakeOperation, receipt.operation_id)
        if operation is None:
            raise RuntimeError("Upload transport receipt lost its intake operation")
        operation_tasks = tasks.operation_tasks(db, operation.id)
        if len(operation_tasks) != len(planned):
            raise RuntimeError("Accepted upload transport receipt has incomplete task links")
        tasks.log_intake_replay(db, operation_tasks, user.id, _server_request_id(request))
        intent_ids = outbox.pending_intake_intents(
            db, operation_id=operation.id, task_ids=[task.id for task in operation_tasks]
        )
        created = False
    else:
        source = IntakeSource.upload if len(accepted) == 1 else IntakeSource.batch
        operation_manifest = tasks.intake_manifest(source.value, [digest for _, _, digest in accepted])
        source_metadata = tasks.intake_source_metadata_manifest([filename for filename, _, _ in accepted])
        operation = tasks.IntakeOperation(
            user_id=user.id,
            idempotency_key=secrets.token_hex(24),
            manifest_sha256=operation_manifest,
            source_metadata_sha256=source_metadata,
        )
        db.add(operation)
        db.flush()
        operation_tasks = []
        request_id = _server_request_id(request)
        for item_index, (filename, content, _digest) in enumerate(accepted):
            try:
                document = tasks.store_document(db, filename, content, source, user.id)
            except tasks.ExistingDocumentUnavailable as exc:
                raise HTTPException(status_code=503, detail="Хранилище документа недоступно; проверьте результат перед повтором") from exc
            except OSError:
                logger.warning("Document intake storage unavailable request_id=%s", request_id or "unavailable")
                raise HTTPException(status_code=503, detail="Не удалось сохранить документ; проверьте хранилище перед повтором") from None
            intake_item = tasks.create_intake_item(
                db,
                operation_id=operation.id,
                item_index=item_index,
                document=document,
                filename=filename,
                source=source,
                received_by_id=user.id,
            )
            operation_tasks.append(tasks.create_task(
                db,
                document,
                user_id=user.id,
                request_id=request_id,
                intake_operation_id=operation.id,
                intake_item_id=intake_item.id,
            ))
        intent_ids = [
            outbox.create_intake_intent(db, task_id=task.id, operation_id=operation.id, job_kind="ocr")
            for task in operation_tasks
        ]
        db.add(UploadTransportChunkReceipt(
            batch_id=batch.id,
            chunk_index=chunk_index,
            operation_id=operation.id,
            accepted_by_id=user.id,
            batch_manifest_sha256=batch.manifest_sha256,
            operation_manifest_sha256=operation_manifest,
            source_metadata_sha256=source_metadata,
        ))
        created = True

    db.commit()
    try:
        for intent_id in intent_ids:
            outbox.dispatch_required(db, intent_id)
    except QueueUnavailable as exc:
        raise _queue_unavailable(exc, _server_request_id(request)) from exc
    return JSONResponse({
        "batch_id": batch.id,
        "chunk_index": chunk_index,
        "operation_id": operation.id,
        "task_ids": [task.id for task in operation_tasks],
        "accepted": True,
        "created": created,
    })
