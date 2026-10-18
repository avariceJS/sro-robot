"""Shared bulk-work view; transport chunks are grouped by shareable intake IDs."""

from __future__ import annotations

import math

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, joinedload

from ..db.models import AssignmentDraft, DebtorCardDraft, Task, TaskStatus
from . import fields, tasks
from .assignments import PREPARATION_KEY

PAGE_SIZE = 50
ACTIVE = (TaskStatus.new, TaskStatus.recognizing, TaskStatus.writing, TaskStatus.awaiting_preview)
GROUPS = ("queued", "processing", "prepared", "needs_review", "failed", "other")


def bulk_task_context(db: Session, operation_ids=None, status=None, page=1) -> dict:
    # One card per source task is enforced by PostgreSQL; this join cannot
    # multiply documents or counts. Counters cover the whole selection/page set.
    category = case(
        (Task.status == TaskStatus.new, "queued"),
        (Task.status.in_(ACTIVE[1:]), "processing"),
        (Task.status == TaskStatus.failed, "failed"),
        (DebtorCardDraft.id.is_not(None), "prepared"),
        (Task.status.in_((TaskStatus.needs_review, TaskStatus.approved)), "needs_review"),
        else_="other",
    )
    conditions = [Task.intake_operation_id.in_(operation_ids)] if operation_ids else []
    counts = dict.fromkeys(GROUPS, 0)
    oldest_queued_timestamp = func.min(
        case((Task.status == TaskStatus.new, Task.created_at), else_=None)
    )
    oldest_queued_age = func.extract("epoch", func.now() - oldest_queued_timestamp)
    statement = (select(category.label("category"), func.count(Task.id))
                 .add_columns(oldest_queued_age.label("oldest_queued_age"))
                 .outerjoin(DebtorCardDraft, DebtorCardDraft.source_task_id == Task.id)
                 .where(*conditions).group_by(category))
    oldest_queued_age_seconds = None
    for key, count, age_seconds in db.execute(statement):
        counts[key] = count
        if key == "queued" and age_seconds is not None:
            oldest_queued_age_seconds = max(0, int(age_seconds))
    counts["total"] = sum(counts.values())
    assignment_count = db.scalar(
        select(func.count(AssignmentDraft.id))
        .join(Task, Task.id == AssignmentDraft.source_task_id)
        .where(*conditions)
    ) or 0
    counts["assignment_drafts"] = int(assignment_count)
    selected_count = counts.get(status, counts["total"]) if status else counts["total"]
    pages = max(1, math.ceil(selected_count / PAGE_SIZE))
    page = max(1, min(page, pages))
    if status:
        conditions.append(category == status)
    rows = db.execute(
        select(Task, DebtorCardDraft.id, DebtorCardDraft.preparation_state, AssignmentDraft.id)
        .outerjoin(DebtorCardDraft, DebtorCardDraft.source_task_id == Task.id)
        .outerjoin(AssignmentDraft, AssignmentDraft.source_task_id == Task.id)
        .options(joinedload(Task.document), joinedload(Task.intake_item))
        .where(*conditions).order_by(Task.created_at.desc(), Task.id.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ).all()
    labels = tasks.status_labels(db, [row[0] for row in rows])
    task_rows = []
    for task, card_id, card_state, assignment_id in rows:
        preparation = (task.recognized or {}).get(PREPARATION_KEY) or {}
        task_rows.append({
            "task": task, "summary": fields.summary(task), "status_ru": labels[task.id],
            "internal_card_id": card_id, "internal_card_state": card_state,
            "assignment_id": assignment_id,
            "preparation_reason": preparation.get("reason", ""),
            "possible_card_id": preparation.get("possible_card_id"),
        })
    return {"task_rows": task_rows, "counts": counts,
            "oldest_queued_age_seconds": oldest_queued_age_seconds,
            "should_poll": counts["queued"] + counts["processing"] > 0,
            "page": page, "pages": pages}
