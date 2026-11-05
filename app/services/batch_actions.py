"""Durable, exact-occurrence admission for mixed upload preview work."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import MixedBatchPrepareIntent, Task, TaskStatus
from ..settings import settings
from . import assignments, mixed_intake, tasks


def resolve_prepare_intent(db: Session, task: Task) -> tuple[int, int] | None:
    """Admit only a selected, receipt-bound OCR result to an unsaved preview.

    The caller commits before queue delivery. Repeating this check is safe only
    before the worker claims the blank form; the worker claim is the external
    Add boundary and prevents a second browser entry.
    """
    if settings.sro_recognition_only:
        return None
    intent = db.scalar(
        select(MixedBatchPrepareIntent)
        .where(MixedBatchPrepareIntent.task_id == task.id)
        .with_for_update()
    )
    if intent is None:
        return None
    grouped = mixed_intake.batch_registry_groups(db, intent.batch_id)
    matches = [row for rows in grouped["groups"].values()
               for row in rows if row["file_index"] == intent.file_index]
    if len(matches) != 1 or matches[0]["task_id"] != task.id:
        intent.status = "withheld"
        db.flush()
        return None
    tasks.lock_task_for_update(db, task)
    if intent.status == "admitted":
        return (task.id, intent.actor_id) if (
            task.status == TaskStatus.writing and task.preview_mode == "durable_closed"
            and task.owned_add_state == "not_started"
            and not task.browser_decision and not task.screenshots
        ) else None
    if intent.status != "pending_ocr":
        return None
    if task.status in (TaskStatus.new, TaskStatus.recognizing):
        return None
    if task.owned_add_state != "not_started":
        intent.status = "withheld"
        db.flush()
        return None
    if task.status == TaskStatus.needs_review and tasks.admit_mixed_preview(db, task):
        intent.status = "admitted"
        db.flush()
        return task.id, intent.actor_id
    route = mixed_intake.route_for_task(task)
    if route.registry == "assignments" and route.reason == "explicit_applicant":
        try:
            assignments.create_applicant_assignment_draft(db, task.id, intent.actor_id)
        except (assignments.DraftNotReady, assignments.DraftConflict):
            pass  # Keep the exact source in staff review; never invent identity.
    intent.status = "withheld"
    db.flush()
    return None
