"""Explicit, fail-closed retries for confirmed upload/batch OCR failures."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (
    Document,
    Event,
    IntakeItem,
    IntakeOperation,
    IntakeSource,
    KadImportRequestReceipt,
    KadSourceRun,
    OcrRetryRun,
    Task,
    TaskStatus,
    TaskStatusHistory,
    User,
)
from ..settings import settings
from . import outbox, tasks
from .artifacts import read_document


OCR_RETRY_EXCLUDED_EVENTS = frozenset(
    {
        "created_from_kad",
        "kad_lookup_started",
        "kad_document_received",
        "recognized",
        "fields_saved",
        "approved",
        "approval_invalidated",
        "local_validation_requested",
        "writing_started",
        "local_validation_completed",
        "preview_started",
        "decision_close",
        "preview_timeout",
        "live_save_blocked",
        "not_approved",
        "done",
    }
)


class OcrRetryNotEligible(ValueError):
    """The exact failure and source occurrence do not prove a safe OCR retry."""


class OcrRetryConflict(ValueError):
    """A request supplied a stale or different failure witness."""


class OcrRetrySourceUnavailable(ValueError):
    """The source artifact cannot be verified for another local OCR run."""


@dataclass(frozen=True)
class OcrRetryContext:
    eligible: bool
    failure_history_id: int | None = None
    incoming_run: OcrRetryRun | None = None
    outgoing_run: OcrRetryRun | None = None
    source_item: IntakeItem | None = None
    source_operation: IntakeOperation | None = None
    incoming_requested_by: User | None = None
    outgoing_requested_by: User | None = None


def _source_for_parent(db: Session, parent: Task) -> tuple[IntakeItem, IntakeOperation]:
    if parent.intake_item_id is not None:
        item = db.get(IntakeItem, parent.intake_item_id)
        operation = db.get(IntakeOperation, parent.intake_operation_id)
        if item is None or operation is None:
            raise OcrRetryNotEligible
        if item.operation_id != operation.id or item.document_id != parent.document_id:
            raise OcrRetryNotEligible
        return item, operation

    prior = db.scalar(select(OcrRetryRun).where(OcrRetryRun.child_task_id == parent.id))
    if prior is None:
        raise OcrRetryNotEligible
    item = db.get(IntakeItem, prior.source_intake_item_id)
    operation = db.get(IntakeOperation, prior.source_operation_id)
    if item is None or operation is None or item.document_id != parent.document_id:
        raise OcrRetryNotEligible
    return item, operation


def _eligible_failure(db: Session, parent: Task, expected_history_id: int | None = None):
    if parent.status != TaskStatus.failed:
        raise OcrRetryNotEligible
    history = list(
        db.scalars(
            select(TaskStatusHistory)
            .where(TaskStatusHistory.task_id == parent.id)
            .order_by(TaskStatusHistory.id)
        )
    )
    if (
        len(history) != 2
        or history[0].from_status != TaskStatus.new.value
        or history[0].to_status != TaskStatus.recognizing.value
        or history[1].from_status != TaskStatus.recognizing.value
        or history[1].to_status != TaskStatus.failed.value
        or (expected_history_id is not None and history[1].id != expected_history_id)
    ):
        raise OcrRetryNotEligible
    document = db.get(Document, parent.document_id)
    if document is None or document.source not in (IntakeSource.upload, IntakeSource.batch):
        raise OcrRetryNotEligible
    if parent.recognized and "_kad_source_v1" in parent.recognized:
        raise OcrRetryNotEligible
    if db.scalar(select(KadSourceRun.id).where(KadSourceRun.task_id == parent.id)) is not None:
        raise OcrRetryNotEligible
    if parent.intake_operation_id is not None and (
        db.scalar(
            select(KadSourceRun.id).where(KadSourceRun.operation_id == parent.intake_operation_id)
        ) is not None
        or db.scalar(
            select(KadImportRequestReceipt.request_hash).where(
                KadImportRequestReceipt.operation_id == parent.intake_operation_id
            )
        ) is not None
    ):
        raise OcrRetryNotEligible
    if db.scalar(
        select(Event.id).where(
            Event.task_id == parent.id,
            Event.kind.in_(OCR_RETRY_EXCLUDED_EVENTS),
        ).limit(1)
    ) is not None:
        raise OcrRetryNotEligible
    source_item, source_operation = _source_for_parent(db, parent)
    if source_item.source not in (IntakeSource.upload, IntakeSource.batch):
        raise OcrRetryNotEligible
    return history[1], source_item, source_operation, document


def context_for_task(db: Session, task: Task) -> OcrRetryContext:
    outgoing = db.scalars(
        select(OcrRetryRun).where(OcrRetryRun.parent_task_id == task.id)
    ).first()
    incoming = db.scalars(
        select(OcrRetryRun).where(OcrRetryRun.child_task_id == task.id)
    ).first()
    lineage = incoming or outgoing
    item = db.get(IntakeItem, lineage.source_intake_item_id) if lineage else None
    operation = db.get(IntakeOperation, lineage.source_operation_id) if lineage else None
    incoming_requested_by = db.get(User, incoming.requested_by_id) if incoming else None
    outgoing_requested_by = db.get(User, outgoing.requested_by_id) if outgoing else None
    try:
        if outgoing is not None:
            raise OcrRetryNotEligible
        witness, item, operation, _document = _eligible_failure(db, task)
    except OcrRetryNotEligible:
        return OcrRetryContext(
            eligible=False,
            failure_history_id=None,
            incoming_run=incoming,
            outgoing_run=outgoing,
            source_item=item,
            source_operation=operation,
            incoming_requested_by=incoming_requested_by,
            outgoing_requested_by=outgoing_requested_by,
        )
    return OcrRetryContext(
        eligible=True,
        failure_history_id=witness.id,
        incoming_run=incoming,
        outgoing_run=outgoing,
        source_item=item,
        source_operation=operation,
        incoming_requested_by=incoming_requested_by,
        outgoing_requested_by=outgoing_requested_by,
    )


def request_retry(
    db: Session,
    *,
    parent_task_id: int,
    failure_history_id: int,
    user_id: int,
    request_id: str | None = None,
) -> tuple[OcrRetryRun, Task, int | None, bool]:
    """Create or read the single child for an immutable confirmed OCR failure."""
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Explicit OCR retry requires PostgreSQL")
    parent = db.scalar(
        select(Task)
        .where(Task.id == parent_task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if parent is None:
        raise LookupError(parent_task_id)
    existing = db.scalar(
        select(OcrRetryRun).where(OcrRetryRun.parent_task_id == parent_task_id)
    )
    if existing is not None:
        if existing.failure_history_id != failure_history_id:
            raise OcrRetryConflict
        child = db.get(Task, existing.child_task_id)
        if child is None:
            raise RuntimeError("OCR retry child disappeared")
        return existing, child, None, False

    try:
        witness, item, operation, document = _eligible_failure(
            db, parent, expected_history_id=failure_history_id
        )
    except OcrRetryNotEligible:
        raise OcrRetryConflict from None
    try:
        read_document(
            document.stored_path,
            document.content_hash,
            settings.upload_dir,
            settings.upload_max_file_bytes,
        )
    except (OSError, ValueError, TypeError):
        raise OcrRetrySourceUnavailable from None

    child = tasks.create_task(
        db,
        document,
        user_id=user_id,
        scenario=parent.scenario,
        profile=parent.profile,
        request_id=request_id,
    )
    run = OcrRetryRun(
        parent_task_id=parent.id,
        failure_history_id=witness.id,
        child_task_id=child.id,
        source_intake_item_id=item.id,
        source_operation_id=operation.id,
        document_id=document.id,
        requested_by_id=user_id,
    )
    db.add(run)
    db.flush()
    intent_id = outbox.create_ocr_retry_intent(
        db,
        task_id=child.id,
        retry_run_id=run.id,
        failure_history_id=witness.id,
    )
    return run, child, intent_id, True
