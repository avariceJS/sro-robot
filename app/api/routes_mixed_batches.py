"""Exact upload-batch worklists and explicit mixed-intake actions."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.base import get_session
from ..db.models import AssignmentDraft, Event, MixedBatchPrepareIntent, Task, TaskStatus, UploadTransportBatch, User
from ..settings import settings
from ..services import batch_actions, fields, mixed_intake, tasks, upload_transport
from ..workers.queues import QueueUnavailable, enqueue_sro_durable_save, enqueue_sro_form_preview
from .deps import current_user


router = APIRouter(tags=["mixed upload batches"])
_BATCH_ID = re.compile(r"[0-9a-f]{32}\Z")
_ACTION_KEY = re.compile(r"[0-9a-f]{48}\Z")


def _require_batch(db: Session, batch_id: str) -> UploadTransportBatch:
    if _BATCH_ID.fullmatch(batch_id) is None:
        raise HTTPException(status_code=404, detail="Партия загрузки не найдена")
    batch = db.get(UploadTransportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Партия загрузки не найдена")
    return batch


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"sro-robot/mixed-batch-snapshot/v1\0" + encoded).hexdigest()


def _batch_snapshot(db: Session, batch: UploadTransportBatch) -> str:
    """Bind actions to the immutable manifest and currently accepted chunks."""
    receipts = upload_transport.receipt_map(db, batch.id)
    return _digest({
        "batch_id": batch.id,
        "manifest_sha256": batch.manifest_sha256,
        "receipts": [(index, receipt.operation_id) for index, receipt in sorted(receipts.items())],
    })


def _row_snapshot(batch_snapshot: str, row: dict, task: Task | None) -> str:
    return _digest({
        "batch_snapshot": batch_snapshot,
        "file_index": row["file_index"],
        "task_id": row["task_id"],
        "status": row["status"],
        "registry": row["registry"],
        "recognized": task.recognized if task is not None else None,
        "values": task.values if task is not None else None,
        "preview_token": tasks.preview_token(task) if task is not None and task.screenshots else None,
    })


def mixed_batch_state(db: Session, batch_id: str, user_id: int) -> dict:
    """Read exact receipt membership; no work is started by this read path."""
    batch = _require_batch(db, batch_id)
    grouped = mixed_intake.batch_registry_groups(db, batch.id)
    snapshot = _batch_snapshot(db, batch)
    for registry, rows in grouped["groups"].items():
        for row in rows:
            row["registry"] = registry
            task = db.get(Task, row["task_id"]) if row["task_id"] is not None else None
            row["row_token"] = _row_snapshot(snapshot, row, task)
            row["task_url"] = f"/tasks/{task.id}?registry={registry}" if task is not None else None
            row["source_url"] = f"/tasks/{task.id}/document" if task is not None else None
            row["source_preview_url"] = (
                f"/tasks/{task.id}/document/preview"
                if task is not None and task.document is not None else None
            )
            row["preview_url"] = row["task_url"]
            row["screenshots"] = [
                {"url": f"/tasks/{task.id}/shot/{index}", "alt": f"Снимок формы {index + 1}"}
                for index, _ in enumerate(task.screenshots or [])
            ] if task is not None else []
            review_rows = fields.review_fields(task) if task is not None else []
            row["data_summary"] = "\n".join(
                f"{field.title}: {field.value[:512]}"
                for field in review_rows if field.value
            )
            intent = db.get(MixedBatchPrepareIntent, (batch.id, row["file_index"]))
            row["prepare_status"] = intent.status if intent is not None else "not_requested"
            owned_add_state = getattr(task, "owned_add_state", None) if task is not None else None
            if owned_add_state not in {"not_started", "started", "bound", "unknown"}:
                owned_add_state = "not_started"
            owned_bound = bool(
                task is not None and owned_add_state == "bound"
                and tasks._owned_binding_matches(task)
            )
            owned_principal = bool(
                task is not None and owned_bound
                and tasks._owned_principal_matches(db, task, user_id)
            )
            refresh_state = (getattr(task, "preview_refresh_state", None) or "idle") if task is not None else "idle"
            row["owned_add_state"] = owned_add_state
            row["role_review_required"] = bool(
                row["reason"] in {
                    "explicit_person_debtor", "explicit_entity_debtor", "review_person_candidate",
                }
                and (task is None or task.approved_by_id is None)
            )
            if owned_add_state == "unknown":
                technical_state = "add_unknown"
            elif refresh_state == "unknown":
                technical_state = "failed"
            elif refresh_state in {"requested", "started"}:
                technical_state = "preparing"
            elif task is not None and (task.status == TaskStatus.failed
                                       or task.browser_decision == "preview_failed"
                                       or (intent is not None and intent.status == "withheld")
                                       or (owned_add_state == "bound" and not owned_bound)):
                technical_state = "failed"
            elif (owned_bound and task is not None
                  and task.status == TaskStatus.awaiting_preview and task.screenshots):
                technical_state = "preview_ready"
            elif owned_add_state in {"started", "bound"} or (
                task is not None and task.status == TaskStatus.writing
            ):
                technical_state = "preparing"
            else:
                technical_state = "pending"
            row["technical_card_state"] = technical_state
            row["preview_uses_owned_card"] = bool(
                owned_bound and technical_state == "preview_ready"
            )
            assignment = db.scalar(select(AssignmentDraft).where(AssignmentDraft.source_task_id == task.id)) if task is not None else None
            row["assignment_url"] = f"/assignments/{assignment.id}" if assignment is not None else None
            row["assignment_status"] = assignment.status if assignment is not None else None
            row["assignment_needs_debtor"] = bool(
                assignment is not None and assignment.source_role == "applicant"
                and assignment.debtor_card_id is None
            )
            row["assignment_needs_practitioner"] = bool(
                assignment is not None and assignment.source_role == "applicant"
                and not str((assignment.values or {}).get("practitioner") or "").strip()
            )
            row["preview_token"] = (
                tasks.preview_token(task)
                if task is not None and task.status == TaskStatus.awaiting_preview else ""
            )
            row["preview_refresh_state"] = refresh_state
            save_readiness = (
                tasks._save_readiness_errors(task)
                if task is not None and task.preview_mode == "durable_closed"
                and task.status == TaskStatus.awaiting_preview else []
            )
            durable_reviewable = bool(
                task is not None and task.preview_mode == "durable_closed"
                and task.status == TaskStatus.awaiting_preview and not task.browser_decision
                and refresh_state == "idle" and not save_readiness
                and owned_principal
                and task.screenshots and task.preview_schema_version == 2
                and task.preview_values_sha256 == tasks.values_digest(task.values or {})
            )
            retryable_save_requested = False
            if (task is not None and task.preview_mode == "durable_closed"
                    and task.status == TaskStatus.awaiting_preview
                    and task.browser_decision == "save_requested" and task.preview_save_enabled
                    and refresh_state == "idle" and not save_readiness
                    and owned_principal
                    and task.screenshots and task.preview_schema_version == 2
                    and task.preview_values_sha256 == tasks.values_digest(task.values or {})):
                decision = db.scalar(select(Event).where(
                    Event.task_id == task.id, Event.kind == "decision_save",
                ).order_by(Event.created_at.desc(), Event.id.desc()).limit(1))
                retryable_save_requested = bool(
                    decision is not None and decision.user_id == user_id
                    and isinstance(decision.payload, dict)
                    and decision.payload.get("preview_token") == row["preview_token"]
                )
            row["can_save"] = bool(durable_reviewable or retryable_save_requested)
            row["save_retry"] = retryable_save_requested
            row["local_finalization_state"] = (
                task.local_finalization_state if task is not None else None
            )
            row["can_reconcile_local"] = bool(
                task is not None and task.preview_purpose == "new_record_review"
                and task.local_finalization_state in {"pending", "failed"}
                and tasks.saved_sro_record_url(task)
            )
            row["save_block_reason"] = (
                "" if row["can_save"] else
                "Обновление снимков продолжается" if refresh_state in {"requested", "started"} else
                "Исход обновления снимков неизвестен; нужна сверка" if refresh_state == "unknown" else
                "Поля карточки неполны; исправьте их и обновите снимки" if save_readiness else
                "Нужен свежий снимок заполненной формы и решение сотрудника"
            )
            retryable_admitted = bool(
                task is not None and intent is not None and intent.status == "admitted"
                and task.status == TaskStatus.writing and task.preview_mode == "durable_closed"
                and owned_add_state == "not_started"
                and not task.browser_decision and not task.screenshots
            )
            row["prepare_retry"] = retryable_admitted
            fresh_eligibility = (
                tasks.mixed_preview_eligibility(task)
                if task is not None and task.status == TaskStatus.needs_review
                and owned_add_state == "not_started"
                else {"eligible": False, "reason": "task_not_in_review"}
            )
            row["can_prepare"] = bool(
                task is not None
                and registry in {"persons", "organizations"}
                and row["reason"] in {"explicit_person_debtor", "explicit_entity_debtor", "review_person_candidate"}
                and (fresh_eligibility["eligible"] or retryable_admitted)
            )
            row["prepare_block_reason"] = (
                "" if row["can_prepare"] else
                "Ожидается распознавание" if task is None or registry == "pending" else
                "Исход Add неизвестен; нужна сверка в СРО" if owned_add_state == "unknown" else
                "Форма СРО для этого типа карточки ещё не квалифицирована" if fresh_eligibility["reason"] == "scenario_not_qualified" else
                "Кандидат неполон; проверьте поля по источнику" if fresh_eligibility["reason"] == "proposal_incomplete" else
                "Уточните роль или проверьте текущее состояние задания"
            )
            row["source_kind"] = Path(row["filename"]).suffix.casefold()
            if settings.sro_recognition_only:
                row["can_prepare"] = False
                row["prepare_retry"] = False
                row["can_save"] = False
                row["save_retry"] = False
                row["can_reconcile_local"] = False
                row["prepare_block_reason"] = "Локальный режим: только распознавание"
                row["save_block_reason"] = "Локальный режим: только распознавание"
    return {**grouped, "snapshot_token": snapshot,
            "recognition_only": settings.sro_recognition_only}


async def _json_action(request: Request, expected_keys: set[str], optional_keys: set[str] = frozenset()) -> dict:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Ожидается JSON запрос")
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(status_code=413, detail="Слишком большой запрос")
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Некорректный JSON") from exc
    if not isinstance(data, dict) or not expected_keys <= set(data) or set(data) - expected_keys - optional_keys:
        raise HTTPException(status_code=422, detail="Некорректное действие с партией")
    if not isinstance(data.get("snapshot_token"), str) or len(data["snapshot_token"]) != 64:
        raise HTTPException(status_code=422, detail="Обновите состояние партии")
    if not isinstance(data.get("request_key"), str) or _ACTION_KEY.fullmatch(data["request_key"]) is None:
        raise HTTPException(status_code=422, detail="Обновите страницу перед действием")
    return data


def _selected_rows(state: dict, file_indices: object) -> list[dict]:
    if (not isinstance(file_indices, list) or not 1 <= len(file_indices) <= state["total"]
            or any(type(index) is not int or index < 0 or index >= state["total"] for index in file_indices)
            or len(set(file_indices)) != len(file_indices)):
        raise HTTPException(status_code=422, detail="Выберите точные файлы партии без повторов")
    by_index = {row["file_index"]: row for rows in state["groups"].values() for row in rows}
    if set(file_indices) - set(by_index):
        raise HTTPException(status_code=409, detail="Состав партии изменился; обновите страницу")
    return [by_index[index] for index in file_indices]


def _assert_batch_snapshot(state: dict, expected: str) -> None:
    if expected != state["snapshot_token"]:
        raise HTTPException(status_code=409, detail="Состав принятых частей изменился; обновите страницу")


def registry_filter(request: Request) -> str | None:
    value = request.query_params.get("registry")
    if value is None or value == "":
        return None
    if value not in mixed_intake.FOLDERS:
        raise HTTPException(status_code=422, detail="Неизвестная папка партии")
    return value


def filtered_mixed_batch_state(state: dict, registry: str | None) -> dict:
    """Project one exact folder while retaining the full batch snapshot token."""
    if registry is None:
        return state
    rows = state["groups"][registry]
    return {**state, "groups": {registry: rows}, "counts": {registry: len(rows)},
            "total": len(rows), "registry": registry}


@router.post("/upload-batches/{batch_id}/mixed/prepare", name="prepare_mixed_upload_batch")
async def prepare_mixed_upload_batch(
    batch_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> JSONResponse:
    """One explicit upload action selects source occurrences, including pending OCR."""
    if settings.sro_recognition_only:
        raise HTTPException(status_code=409, detail="Локальный режим: только распознавание")
    # Manual row actions carry freshness tokens; whole-upload intent may race OCR.
    data = await _json_action(
        request, {"file_indices", "snapshot_token", "request_key"}, {"row_tokens"}
    )
    batch = _require_batch(db, batch_id)
    db.scalar(select(UploadTransportBatch).where(UploadTransportBatch.id == batch.id).with_for_update())
    state = mixed_batch_state(db, batch_id, user.id)
    _assert_batch_snapshot(state, data["snapshot_token"])
    selected = _selected_rows(state, data["file_indices"])
    row_tokens = data.get("row_tokens")
    if row_tokens is not None and (
        not isinstance(row_tokens, dict)
        or set(row_tokens) != {str(row["file_index"]) for row in selected}
        or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
               for value in row_tokens.values())
    ):
        raise HTTPException(status_code=422, detail="Некорректный снимок выбранных файлов")
    results = []
    enqueue: list[tuple[int, int, int]] = []
    for row in selected:
        file_index = row["file_index"]
        if row_tokens is not None and row_tokens[str(file_index)] != row["row_token"]:
            results.append({"file_index": file_index, "status": "stale"})
            continue
        task_id = row["task_id"]
        if task_id is None:
            results.append({"file_index": file_index, "status": "missing_task"})
            continue
        intent = db.get(MixedBatchPrepareIntent, (batch.id, file_index))
        if intent is None:
            intent = MixedBatchPrepareIntent(
                batch_id=batch.id, file_index=file_index, actor_id=user.id,
                task_id=task_id, status="pending_ocr",
            )
            db.add(intent)
            db.flush()
            created = True
        else:
            created = False
            if intent.task_id != task_id:
                raise HTTPException(status_code=409, detail="Квитанция файла не совпадает с заданием")
        task = db.get(Task, task_id)
        admitted = batch_actions.resolve_prepare_intent(db, task) if task is not None else None
        if admitted is not None:
            enqueue.append((file_index, *admitted))
        results.append({
            "file_index": file_index,
            "status": intent.status if created else "existing" if intent.status == "pending_ocr" else intent.status,
            "task_id": task_id,
        })
    db.commit()
    for file_index, task_id, actor_id in enqueue:
        try:
            enqueue_sro_form_preview(task_id, actor_id)
        except QueueUnavailable:
            for result in results:
                if result["file_index"] == file_index:
                    result["status"] = "queue_uncertain"
                    break
    return JSONResponse({
        "batch_id": batch.id, "results": results,
        "mixed_url": str(request.url_for("get_mixed_upload_batch", batch_id=batch.id)),
    }, status_code=202 if any(result["status"] in {"pending_ocr", "queue_uncertain"} for result in results) else 200,
       headers={"Cache-Control": "private, no-store"})


@router.post("/upload-batches/{batch_id}/mixed/save", name="save_mixed_upload_batch")
async def save_mixed_upload_batch(
    batch_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> JSONResponse:
    """Authorize only exact, individually reviewed closed previews for one Save each."""
    if settings.sro_recognition_only:
        raise HTTPException(status_code=409, detail="Локальный режим: только распознавание")
    data = await _json_action(request, {"items", "snapshot_token", "request_key"})
    state = mixed_batch_state(db, batch_id, user.id)
    _assert_batch_snapshot(state, data["snapshot_token"])
    items = data["items"]
    if (not isinstance(items, list) or not 1 <= len(items) <= state["total"]
            or any(not isinstance(item, dict) or set(item) != {"file_index", "preview_token"}
                   or type(item["file_index"]) is not int or not isinstance(item["preview_token"], str)
                   or re.fullmatch(r"[0-9a-f]{64}", item["preview_token"]) is None for item in items)):
        raise HTTPException(status_code=422, detail="Выберите снимки конкретных файлов")
    selected = _selected_rows(state, [item["file_index"] for item in items])
    by_index = {row["file_index"]: row for row in selected}
    results = []
    for item in items:
        file_index = item["file_index"]
        task_id = by_index[file_index]["task_id"]
        if task_id is None:
            results.append({"file_index": file_index, "status": "missing_task"})
            continue
        task = db.get(Task, task_id)
        if task is None:
            results.append({"file_index": file_index, "status": "missing_task"})
            continue
        token = item["preview_token"]
        request_id = hashlib.sha256(
            f"sro-robot/mixed-save/v1:{batch_id}:{file_index}:{user.id}:{data['request_key']}".encode()
        ).hexdigest()[:32]
        try:
            decision = db.scalar(select(Event).where(
                Event.task_id == task.id, Event.kind == "decision_save",
            ).order_by(Event.created_at.desc(), Event.id.desc()).limit(1))
            same_decision = bool(
                decision is not None and decision.user_id == user.id
                and isinstance(decision.payload, dict)
                and decision.payload.get("preview_token") == token
            )
            exact_replay = bool(same_decision and decision.payload.get("request_id") == request_id)
            current_decision = task.browser_decision or ""
            if (task.preview_mode == "durable_closed"
                    and current_decision in {"save_started", "saved", "save_unknown"}):
                # A returned response can be lost after the external one-shot
                # claim. Return its current state; never deliver another Save.
                outcome = current_decision if exact_replay else "stale_or_unavailable"
            elif task.preview_mode != "durable_closed" or task.status != TaskStatus.awaiting_preview:
                outcome = "not_ready"
            elif (current_decision == "save_requested" and same_decision
                  and task.preview_save_enabled and tasks._owned_binding_matches(task)):
                # Redis delivery may have failed before the worker's durable
                # claim. Re-delivery is safe while this state remains unclaimed.
                outcome = "save_requested"
            elif tasks.confirm_durable_preview(db, task, user.id, token, request_id) and tasks.request_durable_sro_save(
                db, task, user.id, token, request_id
            ):
                outcome = "save_requested"
            else:
                outcome = "stale_or_unavailable"
            db.commit()
        except Exception:
            db.rollback()
            results.append({"file_index": file_index, "task_id": task_id,
                            "status": "state_uncertain"})
            continue
        if outcome == "save_requested":
            try:
                enqueue_sro_durable_save(task.id, user.id, token)
            except QueueUnavailable:
                outcome = "queue_uncertain"
        results.append({"file_index": file_index, "task_id": task_id, "status": outcome})
    return JSONResponse({"batch_id": batch_id, "results": results},
                        headers={"Cache-Control": "private, no-store"})


@router.post("/upload-batches/{batch_id}/mixed/reconcile-local", name="reconcile_mixed_local_finalization")
async def reconcile_mixed_local_finalization(
    batch_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> JSONResponse:
    """Retry only local drafts after a verified external Save receipt."""
    if settings.sro_recognition_only:
        raise HTTPException(status_code=409, detail="Локальный режим: только распознавание")
    data = await _json_action(request, {"file_index", "snapshot_token", "request_key"})
    state = mixed_batch_state(db, batch_id, user.id)
    _assert_batch_snapshot(state, data["snapshot_token"])
    row = _selected_rows(state, [data["file_index"]])[0]
    task = db.get(Task, row["task_id"]) if row["task_id"] is not None else None
    if (task is None or task.preview_purpose != "new_record_review"
            or not tasks.saved_sro_record_url(task)):
        raise HTTPException(status_code=409, detail="Нет подтверждённой сохранённой карточки СРО")
    try:
        applied = tasks.reconcile_saved_local_finalization(db, task, user.id)
        db.commit()
    except Exception:
        db.rollback()
        return JSONResponse({"batch_id": batch_id, "file_index": row["file_index"],
                             "status": "state_uncertain"},
                            headers={"Cache-Control": "private, no-store"})
    return JSONResponse({"batch_id": batch_id, "file_index": row["file_index"],
                         "status": "succeeded" if applied else task.local_finalization_state},
                        headers={"Cache-Control": "private, no-store"})


@router.get("/upload-batches/{batch_id}/mixed", name="get_mixed_upload_batch")
def get_mixed_upload_batch(
    batch_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_session),
) -> JSONResponse:
    state = mixed_batch_state(db, batch_id, user.id)
    return JSONResponse(filtered_mixed_batch_state(state, registry_filter(request)),
                        headers={"Cache-Control": "private, no-store"})
