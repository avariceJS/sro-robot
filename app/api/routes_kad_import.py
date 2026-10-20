"""Authenticated same-origin import of an ordinary-browser KAD PDF."""
import hashlib
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from .deps import current_user
from ..db.base import get_session
from ..db.models import IntakeOperation, IntakeSource, User
from ..observability import request_id_from_scope
from ..services import outbox, tasks
from ..services.kad_import import SOURCE_KEY, validate_source
from ..services import kad_operations
from ..settings import settings
from ..workers.queues import QueueUnavailable

router = APIRouter(tags=["tasks"])


@router.post("/kad-helper/import", name="kad_import")
async def import_pdf(request: Request, user: User = Depends(current_user), db: Session = Depends(get_session)):
    async with request.form(max_files=1, max_fields=3, max_part_size=16384) as form:
        required = {"file", "idempotency_key", "source"}
        if not required.issubset(form.keys()) or set(form.keys()) - (required | {"reprocess"}) or any(len(form.getlist(k)) != 1 for k in form):
            raise HTTPException(422, "Некорректная передача документа")
        key = form.get("idempotency_key")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{48}", key):
            raise HTTPException(422, "Некорректный ключ запуска")
        try:
            source = validate_source(json.loads(form["source"]))
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, "Не подтверждён источник документа") from None
        raw_reprocess = str(form.get("reprocess", "false"))
        if raw_reprocess not in {"true", "false"}:
            raise HTTPException(422, "Некорректный режим повторной обработки")
        reprocess = raw_reprocess == "true"
        upload = form["file"]
        if not isinstance(upload, UploadFile):
            raise HTTPException(422, "Нужен PDF")
        content = await upload.read(settings.upload_max_file_bytes + 1)
        if len(content) > settings.upload_max_file_bytes:
            raise HTTPException(413, "PDF превышает допустимый размер")
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-4096:]:
            raise HTTPException(422, "КАД не вернул целый PDF-документ")

    digest = hashlib.sha256(content).hexdigest()
    try:
        decision = kad_operations.begin_import(
            db,
            user_id=user.id,
            request_key=key,
            manifest_sha256=tasks.intake_manifest("kad_browser", [digest]),
            source=source,
            reprocess=reprocess,
        )
    except (tasks.IntakeOperationConflict, kad_operations.KadSourceConflict) as exc:
        raise HTTPException(
            409,
            {"code": "kad_source_conflict", "message": str(exc)},
        ) from None
    request_id = request_id_from_scope(request.scope)
    if decision.is_new:
        operation = decision.operation
        try:
            document = tasks.store_document(db, "kad-document.pdf", content, IntakeSource.kad, user.id)
        except (OSError, tasks.ExistingDocumentUnavailable):
            raise HTTPException(503, "Хранилище документа недоступно") from None
        item = tasks.create_intake_item(db, operation_id=operation.id, item_index=0,
                                       document=document, filename="kad-document.pdf",
                                       source=IntakeSource.kad, received_by_id=user.id)
        task = tasks.create_task(db, document, user_id=user.id, request_id=request_id,
                                 intake_operation_id=operation.id, intake_item_id=item.id)
        # Keep source and explicit run lineage per task, not on a content-shared Document.
        task_source = {
            **source,
            "sha256": digest,
            "run_generation": decision.generation,
        }
        if decision.parent_task_id is not None:
            task_source["reprocessed_from_task_id"] = decision.parent_task_id
        task.recognized = {SOURCE_KEY: task_source}
        task.case_number = source["caseNumber"]
        kad_operations.complete_import(db, decision, task, user.id)
    else:
        operation = decision.operation
        task = decision.task
        if task is None:
            raise HTTPException(409, "Не удалось подтвердить результат предыдущего приёма")
        tasks.log_intake_replay(db, [task], user.id, request_id)
    intent = outbox.create_intake_intent(db, task_id=task.id, operation_id=operation.id, job_kind="ocr")
    db.commit()
    queued = True
    try:
        outbox.dispatch_required(db, intent)
    except QueueUnavailable:
        queued = False  # Durable receipt is known; do not report it as a failed import.
    return {
        "task_url": f"/tasks/{task.id}",
        "queued": queued,
        "reused": not decision.is_new,
        "run_generation": decision.generation,
    }


@router.get("/kad-helper/receipt/{key}", name="kad_import_receipt")
def import_receipt(key: str, user: User = Depends(current_user), db: Session = Depends(get_session)):
    """Resolve an uncertain local receipt without starting/retrying any work."""
    if not re.fullmatch(r"[0-9a-f]{48}", key):
        raise HTTPException(422, "Некорректный ключ запуска")
    operation = kad_operations.receipt_operation(db, user.id, key)
    if operation is not None:
        linked = tasks.operation_tasks(db, operation.id)
        if len(linked) != 1 or SOURCE_KEY not in (linked[0].recognized or {}):
            raise HTTPException(409, "Не удалось восстановить результат предыдущего приёма")
        return {"task_url": f"/tasks/{linked[0].id}"}
    operation = db.scalar(select(IntakeOperation).where(
        IntakeOperation.user_id == user.id, IntakeOperation.idempotency_key == key))
    if operation is None:
        raise HTTPException(404, "Приём пока не подтверждён")
    linked = tasks.operation_tasks(db, operation.id)
    if len(linked) != 1 or SOURCE_KEY not in (linked[0].recognized or {}):
        raise HTTPException(409, "Ключ относится к другому приёму")
    return {"task_url": f"/tasks/{linked[0].id}"}
