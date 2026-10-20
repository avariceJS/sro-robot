"""Shared bulk-work view; transport chunks are grouped by shareable intake IDs."""

from __future__ import annotations

import math

from sqlalchemy import case, func, select, tuple_
from sqlalchemy.orm import Session, joinedload

from ..db.models import (
    TASK_STATUS_RU,
    AssignmentDraft,
    DebtorCardDraft,
    IntakeItem,
    IntakeOperation,
    IntakeSource,
    Task,
    TaskStatus,
    User,
)
from . import fields, tasks
from .assignments import PREPARATION_KEY

PAGE_SIZE = 50
ACTIVE = (TaskStatus.new, TaskStatus.recognizing, TaskStatus.writing, TaskStatus.awaiting_preview)
GROUPS = ("queued", "processing", "prepared", "needs_review", "failed", "other")
INTAKE_HISTORY_PAGE_SIZE = 50
UPLOAD_INTAKE_SOURCES = (IntakeSource.upload, IntakeSource.batch)


def bulk_task_context(
    db: Session, operation_ids=None, status=None, page=1, *, cursor=None,
) -> dict:
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
    task_rows = (
        select(Task, DebtorCardDraft.id, DebtorCardDraft.preparation_state, AssignmentDraft.id)
        .outerjoin(DebtorCardDraft, DebtorCardDraft.source_task_id == Task.id)
        .outerjoin(AssignmentDraft, AssignmentDraft.source_task_id == Task.id)
        .options(joinedload(Task.document), joinedload(Task.intake_item))
        .where(*conditions)
    )
    cursor_direction = cursor[0] if cursor else None
    cursor_created_at = cursor[1] if cursor else None
    cursor_id = cursor[2] if cursor else None
    if cursor_direction == "before":
        rows = db.execute(
            task_rows.where(tuple_(Task.created_at, Task.id) < (cursor_created_at, cursor_id))
            .order_by(Task.created_at.desc(), Task.id.desc()).limit(PAGE_SIZE)
        ).all()
    elif cursor_direction == "after":
        # Fetch the nearest newer page, so newly inserted head rows do not
        # shift records across an already traversed page boundary.
        rows = list(db.execute(
            task_rows.where(tuple_(Task.created_at, Task.id) > (cursor_created_at, cursor_id))
            .order_by(Task.created_at.asc(), Task.id.asc()).limit(PAGE_SIZE)
        ).all())
        rows.reverse()
    else:
        rows = db.execute(
            task_rows.order_by(Task.created_at.desc(), Task.id.desc())
            .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
        ).all()
    if cursor_direction and not rows:
        # A moving status filter may remove every row beyond an old cursor.
        # Recover at the current head instead of rendering a misleading empty page.
        rows = db.execute(
            task_rows.order_by(Task.created_at.desc(), Task.id.desc()).limit(PAGE_SIZE)
        ).all()
        page = 1
    if cursor_direction and rows:
        first_task = rows[0][0]
        preceding = db.scalar(
            select(func.count(Task.id)).select_from(Task)
            .outerjoin(DebtorCardDraft, DebtorCardDraft.source_task_id == Task.id)
            .outerjoin(AssignmentDraft, AssignmentDraft.source_task_id == Task.id)
            .where(*conditions, tuple_(Task.created_at, Task.id) >
                   (first_task.created_at, first_task.id))
        ) or 0
        page = min(pages, int(preceding) // PAGE_SIZE + 1)
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
    previous_cursor = None
    next_cursor = None
    if task_rows and page > 1:
        first_task = task_rows[0]["task"]
        previous_cursor = (first_task.created_at, first_task.id)
    if task_rows and page < pages:
        last_task = task_rows[-1]["task"]
        next_cursor = (last_task.created_at, last_task.id)
    return {"task_rows": task_rows, "counts": counts,
            "oldest_queued_age_seconds": oldest_queued_age_seconds,
            "should_poll": counts["queued"] + counts["processing"] > 0,
            "page": page, "pages": pages,
            "previous_cursor": previous_cursor, "next_cursor": next_cursor}


def intake_history_page(db: Session, page: int = 1) -> dict:
    """List exact accepted upload operations and their current task outcomes."""
    eligible_operation_ids = (
        select(IntakeItem.operation_id)
        .where(IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES))
        .distinct()
    )
    eligible_operations = eligible_operation_ids.subquery()
    total = int(db.scalar(
        select(func.count()).select_from(eligible_operations)
    ) or 0)
    pages = max(1, math.ceil(total / INTAKE_HISTORY_PAGE_SIZE))
    page = max(1, min(page, pages))
    operation_rows = db.execute(
        select(
            IntakeOperation.id,
            IntakeOperation.created_at,
            User.full_name,
            User.login,
        )
        .join(User, User.id == IntakeOperation.user_id)
        .where(IntakeOperation.id.in_(eligible_operation_ids))
        .order_by(IntakeOperation.created_at.desc(), IntakeOperation.id.desc())
        .offset((page - 1) * INTAKE_HISTORY_PAGE_SIZE)
        .limit(INTAKE_HISTORY_PAGE_SIZE)
    ).all()
    operation_ids = [row.id for row in operation_rows]
    rows_by_operation = {
        row.id: {
            "operation_id": row.id,
            "created_at": row.created_at,
            "operator": row.full_name.strip() or row.login,
            "item_count": 0,
            "task_count": 0,
            "filenames": [],
            "status_counts": {},
        }
        for row in operation_rows
    }
    if operation_ids:
        item_rows = db.execute(
            select(IntakeItem.operation_id, IntakeItem.filename, Task.status)
            .outerjoin(Task, Task.intake_item_id == IntakeItem.id)
            .where(
                IntakeItem.operation_id.in_(operation_ids),
                IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES),
            )
            .order_by(IntakeItem.operation_id, IntakeItem.item_index)
        ).all()
        for operation_id, filename, status in item_rows:
            row = rows_by_operation[operation_id]
            row["item_count"] += 1
            if len(row["filenames"]) < 3:
                row["filenames"].append(filename)
            if status is not None:
                row["task_count"] += 1
                label = TASK_STATUS_RU[status]
                row["status_counts"][label] = row["status_counts"].get(label, 0) + 1
    for row in rows_by_operation.values():
        row["omitted_filename_count"] = max(0, row["item_count"] - len(row["filenames"]))
        row["status_counts"] = [
            {"label": label, "count": count}
            for label, count in row["status_counts"].items()
        ]
    return {
        "operations": list(rows_by_operation.values()),
        "page": page,
        "pages": pages,
        "total": total,
    }
