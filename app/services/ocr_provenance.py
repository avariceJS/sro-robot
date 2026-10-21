"""Receipt-based eligibility for local OCR without conflating shared documents."""

from __future__ import annotations

from sqlalchemy import Integer, and_, cast, exists, or_, select
from sqlalchemy.orm import aliased

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
)

UPLOAD_INTAKE_SOURCES = (IntakeSource.upload, IntakeSource.batch)
KAD_ACQUISITION_EVENTS = (
    "created_from_kad",
    "kad_lookup_started",
    "kad_document_received",
)


def _operation_has_kad_metadata(operation_id):
    return or_(
        exists(select(KadSourceRun.id).where(KadSourceRun.operation_id == operation_id)),
        exists(
            select(KadImportRequestReceipt.request_hash).where(
                KadImportRequestReceipt.operation_id == operation_id
            )
        ),
    )


def _operation_has_non_upload_item(operation_id):
    operation_item = aliased(IntakeItem)
    return exists(
        select(operation_item.id).where(
            operation_item.operation_id == operation_id,
            ~operation_item.source.in_(UPLOAD_INTAKE_SOURCES),
        )
    )


def _operation_has_kad_event(operation_id):
    source_task = aliased(Task)
    return exists(
        select(Event.id)
        .select_from(Event)
        .join(source_task, Event.task_id == source_task.id)
        .where(
            source_task.intake_operation_id == operation_id,
            Event.kind.in_(KAD_ACQUISITION_EVENTS),
        )
    )


def _task_has_kad_event(task_id):
    return exists(
        select(Event.id).where(
            Event.task_id == task_id,
            Event.kind.in_(KAD_ACQUISITION_EVENTS),
        )
    )


def _has_clean_upload_receipt(item_id, operation_id, document_id, task_id):
    receipt = aliased(IntakeItem)
    return and_(
        exists(
            select(receipt.id).where(
                receipt.id == item_id,
                receipt.operation_id == operation_id,
                receipt.document_id == document_id,
                receipt.source.in_(UPLOAD_INTAKE_SOURCES),
            )
        ),
        ~_operation_has_non_upload_item(operation_id),
        ~Task.recognized.has_key("_kad_source_v1"),
        ~exists(select(KadSourceRun.id).where(KadSourceRun.task_id == task_id)),
        ~_operation_has_kad_metadata(operation_id),
        ~_task_has_kad_event(task_id),
        ~_operation_has_kad_event(operation_id),
    )


def local_ocr_task_eligibility():
    """SQL predicate for receipt-rooted upload OCR and conservative legacy rows.

    A source occurrence, rather than a shared blob's first source, authorizes
    work. Retry children use their immutable retry receipt. Receipt-less legacy
    tasks retain the former document-source rule only when neither half of a
    receipt context is present.
    """
    direct_upload = _has_clean_upload_receipt(
        Task.intake_item_id,
        Task.intake_operation_id,
        Task.document_id,
        Task.id,
    )
    direct_upload = and_(
        direct_upload,
        ~exists(select(OcrRetryRun.id).where(OcrRetryRun.child_task_id == Task.id)),
    )
    retry = aliased(OcrRetryRun)
    retry_item = aliased(IntakeItem)
    retry_upload = and_(
        Task.intake_item_id.is_(None),
        Task.intake_operation_id.is_(None),
        exists(
            select(retry.id)
            .select_from(retry)
            .join(retry_item, retry_item.id == retry.source_intake_item_id)
            .where(
                retry.child_task_id == Task.id,
                retry.document_id == Task.document_id,
                retry_item.operation_id == retry.source_operation_id,
                retry_item.document_id == retry.document_id,
                retry_item.source.in_(UPLOAD_INTAKE_SOURCES),
            )
        ),
        ~exists(
            select(retry.id)
            .select_from(retry)
            .where(
                retry.child_task_id == Task.id,
                _operation_has_non_upload_item(retry.source_operation_id),
            )
        ),
        ~Task.recognized.has_key("_kad_source_v1"),
        ~exists(select(KadSourceRun.id).where(KadSourceRun.task_id == Task.id)),
        ~exists(
            select(retry.id)
            .select_from(retry)
            .where(
                retry.child_task_id == Task.id,
                _operation_has_kad_metadata(retry.source_operation_id),
            )
        ),
        ~exists(
            select(retry.id)
            .select_from(retry)
            .where(
                retry.child_task_id == Task.id,
                _operation_has_kad_event(retry.source_operation_id),
            )
        ),
        ~_task_has_kad_event(Task.id),
    )
    legacy_upload = and_(
        Task.intake_item_id.is_(None),
        Task.intake_operation_id.is_(None),
        ~exists(select(OcrRetryRun.id).where(OcrRetryRun.child_task_id == Task.id)),
        ~Task.recognized.has_key("_kad_source_v1"),
        ~exists(select(KadSourceRun.id).where(KadSourceRun.task_id == Task.id)),
        ~_task_has_kad_event(Task.id),
        Task.document.has(Document.source.in_(UPLOAD_INTAKE_SOURCES)),
    )
    return or_(direct_upload, retry_upload, legacy_upload)


def kad_browser_ocr_task_eligibility():
    """SQL predicate for the current exact, employee-selected KAD PDF helper."""
    item = aliased(IntakeItem)
    operation = aliased(IntakeOperation)
    run = aliased(KadSourceRun)
    receipt = aliased(KadImportRequestReceipt)
    operation_item = aliased(IntakeItem)
    return and_(
        Task.intake_item_id.is_not(None),
        Task.intake_operation_id.is_not(None),
        Task.recognized.has_key("_kad_source_v1"),
        exists(
            select(run.id)
            .select_from(run)
            .join(item, item.id == Task.intake_item_id)
            .join(operation, operation.id == Task.intake_operation_id)
            .join(receipt, receipt.operation_id == operation.id)
            .where(
                run.task_id == Task.id,
                run.operation_id == operation.id,
                run.generation == cast(
                    Task.recognized["_kad_source_v1"]["run_generation"].astext,
                    Integer,
                ),
                item.operation_id == operation.id,
                item.document_id == Task.document_id,
                item.source == IntakeSource.kad,
                Task.document.has(
                    Document.content_hash
                    == Task.recognized["_kad_source_v1"]["sha256"].astext
                ),
                receipt.source_metadata["cardUrl"].astext
                == Task.recognized["_kad_source_v1"]["cardUrl"].astext,
                receipt.source_metadata["selectedUrl"].astext
                == Task.recognized["_kad_source_v1"]["selectedUrl"].astext,
                operation.source_metadata_sha256.is_not(None),
                receipt.source_metadata_sha256 == operation.source_metadata_sha256,
                ~exists(
                    select(operation_item.id).where(
                        operation_item.operation_id == operation.id,
                        operation_item.source != IntakeSource.kad,
                    )
                ),
            )
        ),
    )


def ocr_worker_task_eligibility():
    """Admit receipt-proven local uploads and explicit current KAD helper tasks."""
    return or_(local_ocr_task_eligibility(), kad_browser_ocr_task_eligibility())
