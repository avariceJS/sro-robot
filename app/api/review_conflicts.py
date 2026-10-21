"""Display-only task review responses for rejected stale submissions."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..db.models import AssignmentDraft, DebtorCardDraft, Task, TaskStatus, User
from ..services import case_packages, fields, ocr_retries, tasks
from ..services.assignments import PREPARATION_KEY
from ..web.templates import templates


def review_conflict_response(
    request: Request,
    task: Task,
    user: User,
    db: Session,
    submitted_values: dict[str, str],
    *,
    operation_ids: list[int] | None = None,
    message: str = "Задание изменилось, введённые значения не сохранены.",
) -> HTMLResponse:
    """Keep unsaved operator input visible after a stale review submission."""
    current_fields = fields.review_fields(task)
    current_values = {field.attr: field.value or "" for field in current_fields}
    submitted = {name: value or "" for name, value in submitted_values.items()}
    response = templates.TemplateResponse(
        request,
        "review.html",
        {
            **_review_context(request, task, user, db, current_fields, operation_ids),
            # A stale value must never be paired with a token that could
            # authorize overwriting a concurrent update.
            "review_token": "",
            "review_conflict": True,
            "review_recovery": False,
            "review_conflict_message": message,
            "submitted_review_values": submitted,
            "current_review_values": current_values,
            "review_conflict_dirty": any(
                current_values.get(name, "") != value for name, value in submitted.items()
            ),
        },
        status_code=409,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def review_recovery_response(
    request: Request,
    task: Task,
    user: User,
    db: Session,
    submitted_values: dict[str, str],
    *,
    operation_ids: list[int] | None = None,
) -> HTMLResponse:
    """Show current saved fields and let the operator transfer proposals one by one."""
    current_fields = fields.review_fields(task)
    current_values = {field.attr: field.value or "" for field in current_fields}
    submitted = {
        field.attr: str(submitted_values.get(field.attr, "") or "")
        for field in current_fields
        if field.attr in submitted_values
    }
    copyable = {}
    for field in current_fields:
        value = submitted.get(field.attr)
        if value is None or value == current_values.get(field.attr, ""):
            continue
        if field.choices:
            allowed = {choice for choice, _label in field.choices}
            if field.attr in {"entity_kind", "address_kind"}:
                allowed.add("")
            if value not in allowed:
                continue
        if field.attr == "case_number" and len(value) > 64:
            continue
        copyable[field.attr] = value
    editable = task.status in (TaskStatus.needs_review, TaskStatus.approved)
    response = templates.TemplateResponse(
        request,
        "review.html",
        {
            **_review_context(request, task, user, db, current_fields, operation_ids),
            "fields": current_fields,
            "review_token": tasks.review_token(task) if editable else "",
            "review_conflict": False,
            "review_recovery": True,
            "submitted_review_values": submitted,
            "current_review_values": current_values,
            "copyable_review_values": copyable,
            "review_conflict_dirty": any(
                current_values.get(name, "") != value for name, value in submitted.items()
            ),
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _review_context(request, task, user, db, current_fields, operation_ids):
    return {
        "user": user,
        "task": task,
        "case_packages": case_packages.active_case_packages_for_task(db, task.id),
        "document_preview_kind": (
            Path(str(getattr(task.document, "stored_path", ""))).suffix.lower()
            if task.document is not None else ""
        ),
        "fields": current_fields,
        "TaskStatus": TaskStatus,
        "status_ru": tasks.status_label(db, task),
        "internal_card": db.query(DebtorCardDraft).filter_by(source_task_id=task.id).one_or_none(),
        "internal_assignment": db.query(AssignmentDraft).filter_by(source_task_id=task.id).one_or_none(),
        "approval_current": bool(
            task.status == TaskStatus.approved and tasks.approval_matches(db, task)
        ),
        "preparation_reason": ((task.recognized or {}).get(PREPARATION_KEY) or {}).get("reason", ""),
        "internal_card_error": "",
        "ocr_retry_context": ocr_retries.context_for_task(db, task),
        "ocr_retry_notice": "",
        "review_operation_ids": operation_ids or [],
        "review_intake_query": ",".join(str(value) for value in operation_ids or []),
    }
