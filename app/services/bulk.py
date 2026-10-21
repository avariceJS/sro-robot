"""Shared bulk-work view; transport chunks are grouped by shareable intake IDs."""

from __future__ import annotations

import math

from sqlalchemy import and_, case, func, literal, select, tuple_
from sqlalchemy.orm import Session, joinedload

from ..db.models import (
    TASK_STATUS_RU,
    AssignmentDraft,
    DebtorCardDraft,
    Document,
    IntakeItem,
    IntakeOperation,
    IntakeSource,
    OcrRetryRun,
    Task,
    TaskStatus,
    User,
)
from . import fields, ocr_retries, outbox, tasks
from .assignments import PREPARATION_KEY
from .ocr_provenance import local_ocr_task_eligibility

PAGE_SIZE = 50
ACTIVE = (TaskStatus.new, TaskStatus.recognizing, TaskStatus.writing, TaskStatus.awaiting_preview)
GROUPS = ("queued", "processing", "prepared", "needs_review", "failed", "other")
INTAKE_HISTORY_PAGE_SIZE = 50
UPLOAD_INTAKE_SOURCES = (IntakeSource.upload, IntakeSource.batch)
OCR_ATTEMPTS_PER_PAGE = 20
OCR_RETRY_REVIEW_PAGE_SIZE = 25


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


def next_upload_review_task_id(
    db: Session, operation_ids: list[int], current_task: Task,
) -> int | None:
    """Find the next unresolved upload task inside the exact selected operations.

    Continue older-first from the current task, then wrap to the newest
    outstanding task. Retry children without their own intake receipt are
    intentionally excluded from this receipt-scoped workflow.
    """
    if not operation_ids or current_task.intake_operation_id not in operation_ids:
        return None
    base = (
        select(Task.id)
        .join(IntakeItem, IntakeItem.id == Task.intake_item_id)
        .where(
            Task.intake_operation_id.in_(operation_ids),
            Task.status == TaskStatus.needs_review,
            IntakeItem.operation_id == Task.intake_operation_id,
            IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES),
        )
    )
    older_id = db.scalar(
        base.where(tuple_(Task.created_at, Task.id) < (current_task.created_at, current_task.id))
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(1)
    )
    if older_id is not None:
        return int(older_id)
    return db.scalar(
        base.where(Task.id != current_task.id)
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(1)
    )


def intake_history_page(db: Session, page: int = 1, filename_query: str = "") -> dict:
    """List exact accepted upload operations and their current task outcomes."""
    filename_query = filename_query.strip()
    escaped_query = (
        filename_query.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    filename_pattern = f"%{escaped_query}%" if filename_query else None

    def operation_has_items():
        item_query = select(IntakeItem.id).where(
            IntakeItem.operation_id == IntakeOperation.id,
            IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES),
        )
        if filename_pattern is not None:
            item_query = item_query.where(IntakeItem.filename.ilike(filename_pattern, escape="\\"))
        return item_query.exists()

    total = int(db.scalar(
        select(func.count()).select_from(IntakeOperation).where(operation_has_items())
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
        .where(operation_has_items())
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
            "failed_count": 0,
            "recoverable_count": 0,
            "recovery_withheld_count": 0,
            "filenames": [],
            "matching_filenames": [],
            "matching_filename_count": 0,
            "status_counts": {},
        }
        for row in operation_rows
    }
    if operation_ids:
        item_rows = db.execute(
            select(
                IntakeItem.operation_id,
                IntakeItem.filename,
                (
                    IntakeItem.filename.ilike(filename_pattern, escape="\\")
                    if filename_pattern is not None
                    else literal(False)
                ).label("filename_matches"),
                Task.status,
                local_ocr_task_eligibility().label("can_recover"),
            )
            .outerjoin(
                Task,
                and_(
                    Task.intake_item_id == IntakeItem.id,
                    Task.intake_operation_id == IntakeItem.operation_id,
                ),
            )
            .outerjoin(Document, Document.id == Task.document_id)
            .where(
                IntakeItem.operation_id.in_(operation_ids),
                IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES),
            )
            .order_by(IntakeItem.operation_id, IntakeItem.item_index)
        ).all()
        for operation_id, filename, filename_matches, status, can_recover in item_rows:
            row = rows_by_operation[operation_id]
            row["item_count"] += 1
            if filename_matches:
                row["matching_filename_count"] += 1
            if filename_matches and len(row["matching_filenames"]) < 3:
                row["matching_filenames"].append(filename)
            if len(row["filenames"]) < 3:
                row["filenames"].append(filename)
            if status is not None:
                row["task_count"] += 1
                if status == TaskStatus.failed:
                    row["failed_count"] += 1
                if status == TaskStatus.new and can_recover:
                    row["recoverable_count"] += 1
                elif status == TaskStatus.new:
                    row["recovery_withheld_count"] += 1
                label = TASK_STATUS_RU[status]
                row["status_counts"][label] = row["status_counts"].get(label, 0) + 1
    for row in rows_by_operation.values():
        row["omitted_filename_count"] = max(0, row["item_count"] - len(row["filenames"]))
        row["omitted_matching_filename_count"] = max(
            0, row["matching_filename_count"] - len(row["matching_filenames"]),
        ) if filename_pattern is not None else 0
        row["status_counts"] = [
            {"label": label, "count": count}
            for label, count in row["status_counts"].items()
        ]
    return {
        "operations": list(rows_by_operation.values()),
        "page": page,
        "pages": pages,
        "total": total,
        "filename_query": filename_query,
    }


def intake_ocr_attempts(db: Session, operation_id: int) -> dict:
    """List receipt roots for one operation without expanding retry chains."""
    operation = db.scalar(
        select(IntakeOperation)
        .join(IntakeItem, IntakeItem.operation_id == IntakeOperation.id)
        .where(
            IntakeOperation.id == operation_id,
            IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES),
        )
        .limit(1)
    )
    if operation is None:
        raise LookupError("Upload operation not found")

    items = db.execute(
        select(IntakeItem, Task)
        .outerjoin(
            Task,
            and_(
                Task.intake_item_id == IntakeItem.id,
                Task.intake_operation_id == IntakeItem.operation_id,
            ),
        )
        .where(
            IntakeItem.operation_id == operation_id,
            IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES),
        )
        .order_by(IntakeItem.item_index)
    ).all()
    task_objects = [task for _item, task in items if task is not None]
    labels = tasks.status_labels(db, task_objects) if task_objects else {}
    receipts = []
    for item, root in items:
        receipts.append({
            "item": item,
            "root": root,
            "root_status_ru": labels[root.id] if root is not None else None,
        })

    retry_review_count = int(db.scalar(
        select(func.count()).select_from(_ocr_retry_review_rows(operation_id).subquery())
    ) or 0)
    return {
        "operation": operation,
        "receipts": receipts,
        "retry_review_count": retry_review_count,
    }


def intake_ocr_attempt_chain(
    db: Session,
    *,
    operation_id: int,
    item_id: int,
    after_task_id: int | None = None,
) -> dict:
    """Read at most one bounded page of attempts for one exact receipt."""
    item = db.scalar(
        select(IntakeItem)
        .where(
            IntakeItem.id == item_id,
            IntakeItem.operation_id == operation_id,
            IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES),
        )
    )
    if item is None:
        raise LookupError("Upload receipt not found")
    operation = db.get(IntakeOperation, operation_id)
    root = db.scalar(
        select(Task)
        .where(
            Task.intake_item_id == item.id,
            Task.intake_operation_id == operation_id,
        )
        .limit(1)
    )
    if root is None:
        raise LookupError("Upload task not found")

    requester = None
    if after_task_id is None:
        current = root
    else:
        cursor_run = db.execute(
            select(OcrRetryRun, Task, User)
            .join(Task, Task.id == OcrRetryRun.child_task_id)
            .join(User, User.id == OcrRetryRun.requested_by_id)
            .where(
                OcrRetryRun.parent_task_id == after_task_id,
                OcrRetryRun.source_operation_id == operation_id,
                OcrRetryRun.source_intake_item_id == item.id,
                OcrRetryRun.document_id == item.document_id,
                Task.document_id == item.document_id,
            )
        ).one_or_none()
        if cursor_run is None:
            raise LookupError("Invalid OCR attempt cursor")
        _run, current, requester = cursor_run

    attempts = []
    next_after_task_id = None
    for _index in range(OCR_ATTEMPTS_PER_PAGE):
        if current is None:
            break
        attempt = {
            "task": current,
            "requested_by": requester,
            "retry_context": (
                ocr_retries.context_for_task(db, current)
                if current.status == TaskStatus.failed else None
            ),
        }
        attempts.append(attempt)
        outgoing = db.execute(
            select(OcrRetryRun, Task, User)
            .join(Task, Task.id == OcrRetryRun.child_task_id)
            .join(User, User.id == OcrRetryRun.requested_by_id)
            .where(
                OcrRetryRun.parent_task_id == current.id,
                OcrRetryRun.source_operation_id == operation_id,
                OcrRetryRun.source_intake_item_id == item.id,
                OcrRetryRun.document_id == item.document_id,
                Task.document_id == item.document_id,
            )
        ).one_or_none()
        if outgoing is None:
            current = None
            break
        run, child, requester = outgoing
        if run.document_id != child.document_id:
            current = None
            break
        attempt["outgoing_child_id"] = child.id
        if len(attempts) == OCR_ATTEMPTS_PER_PAGE:
            next_after_task_id = attempt["task"].id
            current = None
            break
        current = child

    task_rows = [attempt["task"] for attempt in attempts]
    labels = tasks.status_labels(db, task_rows) if task_rows else {}
    for attempt in attempts:
        attempt["status_ru"] = labels[attempt["task"].id]
    return {
        "operation": operation,
        "item": item,
        "attempts": attempts,
        "next_after_task_id": next_after_task_id,
    }


def _ocr_retry_review_rows(operation_id: int):
    """Exact source-receipt lineage for retry children currently awaiting review."""
    return (
        select(OcrRetryRun, Task, IntakeItem, User)
        .join(Task, Task.id == OcrRetryRun.child_task_id)
        .join(IntakeItem, IntakeItem.id == OcrRetryRun.source_intake_item_id)
        .join(User, User.id == OcrRetryRun.requested_by_id)
        .where(
            OcrRetryRun.source_operation_id == operation_id,
            IntakeItem.operation_id == OcrRetryRun.source_operation_id,
            IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES),
            IntakeItem.document_id == OcrRetryRun.document_id,
            Task.document_id == OcrRetryRun.document_id,
            Task.intake_item_id.is_(None),
            Task.intake_operation_id.is_(None),
            Task.status == TaskStatus.needs_review,
        )
    )


def _require_upload_retry_operation(db: Session, operation_id: int) -> IntakeOperation:
    operation = db.scalar(
        select(IntakeOperation)
        .join(IntakeItem, IntakeItem.operation_id == IntakeOperation.id)
        .where(
            IntakeOperation.id == operation_id,
            IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES),
        )
        .limit(1)
    )
    if operation is None:
        raise LookupError("Upload operation not found")
    sources = set(db.scalars(
        select(IntakeItem.source).where(IntakeItem.operation_id == operation_id)
    ))
    if not sources or not sources.issubset(UPLOAD_INTAKE_SOURCES):
        raise LookupError("Operation is not an upload retry-review scope")
    return operation


def intake_ocr_retry_review_queue(
    db: Session, operation_id: int, page: int = 1,
) -> dict:
    """Show only exact retry children that are still awaiting employee review."""
    operation = _require_upload_retry_operation(db, operation_id)
    base = _ocr_retry_review_rows(operation_id)
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    pages = max(1, math.ceil(total / OCR_RETRY_REVIEW_PAGE_SIZE))
    page = max(1, min(page, pages))
    rows = db.execute(
        base.order_by(Task.created_at, Task.id)
        .offset((page - 1) * OCR_RETRY_REVIEW_PAGE_SIZE)
        .limit(OCR_RETRY_REVIEW_PAGE_SIZE)
    ).all()
    reviews = [
        {
            "run": run,
            "task": task,
            "item": item,
            "requested_by": requester,
        }
        for run, task, item, requester in rows
    ]
    return {
        "operation": operation,
        "reviews": reviews,
        "page": page,
        "pages": pages,
        "total": total,
    }


def next_ocr_retry_review_task_id(
    db: Session, operation_id: int, current_task: Task,
) -> int | None:
    """Continue within one exact upload's retry children awaiting review."""
    if current_task.intake_item_id is not None or current_task.intake_operation_id is not None:
        return None
    member = db.scalar(
        select(OcrRetryRun.id)
        .join(Task, Task.id == OcrRetryRun.child_task_id)
        .join(IntakeItem, IntakeItem.id == OcrRetryRun.source_intake_item_id)
        .where(
            OcrRetryRun.child_task_id == current_task.id,
            OcrRetryRun.source_operation_id == operation_id,
            IntakeItem.operation_id == OcrRetryRun.source_operation_id,
            IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES),
            IntakeItem.document_id == OcrRetryRun.document_id,
            Task.document_id == OcrRetryRun.document_id,
            Task.intake_item_id.is_(None),
            Task.intake_operation_id.is_(None),
        )
        .limit(1)
    )
    base = _ocr_retry_review_rows(operation_id).with_only_columns(Task.id)
    if member is None:
        return None
    following = db.scalar(
        base.where(tuple_(Task.created_at, Task.id) >
                   (current_task.created_at, current_task.id))
        .order_by(Task.created_at, Task.id)
        .limit(1)
    )
    if following is not None:
        return int(following)
    first = db.scalar(base.order_by(Task.created_at, Task.id).limit(1))
    return int(first) if first is not None and first != current_task.id else None


def prepare_intake_recovery(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    request_id: str | None,
) -> list[int]:
    """Persist recovery intents for only queued tasks in one upload operation.

    The operation lock serializes retries for this request. Task rows are read
    without explicit FOR UPDATE locks; foreign-key validation may still take
    short key-share locks. Dispatch happens after those intents and
    actor-attributed audit entries commit, and active deliveries keep a durable
    delayed intent so an SKIP LOCKED worker cannot lose its delivery.
    """
    operation = db.scalar(
        select(IntakeOperation)
        .where(IntakeOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if operation is None:
        raise LookupError("Intake operation not found")

    source_values = list(db.scalars(
        select(IntakeItem.source).where(IntakeItem.operation_id == operation_id)
    ))
    if not source_values or any(source not in UPLOAD_INTAKE_SOURCES for source in source_values):
        raise LookupError("Intake operation is not a recoverable upload")

    queued_tasks = list(db.scalars(
        select(Task)
        .join(IntakeItem, IntakeItem.id == Task.intake_item_id)
        .where(
            Task.intake_operation_id == operation_id,
            IntakeItem.operation_id == operation_id,
            IntakeItem.source.in_(UPLOAD_INTAKE_SOURCES),
            Task.status == TaskStatus.new,
            local_ocr_task_eligibility(),
        )
        .order_by(Task.id)
        .execution_options(populate_existing=True)
    ))
    intent_ids = []
    for task in queued_tasks:
        intent_ids.append(outbox.create_intake_intent(
            db,
            task_id=task.id,
            operation_id=operation_id,
            job_kind="ocr",
        ))
        tasks.log(
            db,
            task,
            "intake_recovery_requested",
            user_id=user_id,
            payload={"request_id": request_id} if request_id else None,
        )
    db.flush()
    return intent_ids
