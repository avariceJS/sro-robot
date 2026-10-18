"""Display-only task review responses for rejected stale submissions."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..db.models import AssignmentDraft, DebtorCardDraft, Task, TaskStatus, User
from ..services import fields, tasks
from ..services.assignments import PREPARATION_KEY
from ..web.templates import templates


def review_conflict_response(
    request: Request,
    task: Task,
    user: User,
    db: Session,
    submitted_values: dict[str, str],
    *,
    message: str = "Задание изменилось, введённые значения не сохранены.",
) -> HTMLResponse:
    """Keep unsaved operator input visible after a stale review submission."""
    current_fields = fields.review_fields(task)
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
            "fields": current_fields,
            "TaskStatus": TaskStatus,
            "status_ru": tasks.status_label(db, task),
            "internal_card": db.query(DebtorCardDraft).filter_by(source_task_id=task.id).one_or_none(),
            "internal_assignment": db.query(AssignmentDraft).filter_by(source_task_id=task.id).one_or_none(),
            "approval_current": bool(
                task.status == TaskStatus.approved and tasks.approval_matches(db, task)
            ),
            "preparation_reason": ((task.recognized or {}).get(PREPARATION_KEY) or {}).get("reason", ""),
            # Never pair stale values with a fresh token that could authorize
            # overwriting the concurrent update.
            "review_token": "",
            "internal_card_error": "",
            "review_conflict": True,
            "review_conflict_message": message,
            "submitted_review_values": submitted_values,
            "current_review_values": {field.attr: field.value for field in current_fields},
        },
        status_code=409,
    )
