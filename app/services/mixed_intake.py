"""Read-only, exact-receipt grouping for one shared mixed upload batch."""

from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from sro_robot.core.mixed_classification import MixedRoute, REVIEW, classify_card
from sro_robot.core.models import DebtorCard, card_from_fields
from sro_robot.core.entity_extract import EntityDebtorCard

from ..db.models import (
    Document,
    IntakeItem,
    OcrRetryRun,
    Task,
    TaskStatus,
    UploadTransportBatch,
    UploadTransportBatchItem,
    UploadTransportChunkReceipt,
)

ROUTE_KEY = "_mixed_route_v1"
APPLICANT_KEY = "_applicant_evidence_v1"
FOLDERS = ("persons", "organizations", "assignments", "review", "pending")


def route_for_task(task: Task | None) -> MixedRoute:
    if task is None or task.status in (TaskStatus.new, TaskStatus.recognizing):
        return MixedRoute("pending", None, "ocr_pending")
    if task.status == TaskStatus.failed:
        return MixedRoute("review", None, "ocr_failed")
    recognized = task.recognized or {}
    if task.scenario == "person_card":
        card_class = DebtorCard
    elif task.scenario == "entity_review":
        card_class = EntityDebtorCard
    else:
        return REVIEW
    card = card_from_fields(card_class, recognized)
    if isinstance(card, DebtorCard):
        suggestions = recognized.get("_review_suggestions_v1")
        card.review_suggestions = suggestions if isinstance(suggestions, list) else []
    derived = classify_card(card, applicant_evidence=recognized.get(APPLICANT_KEY))
    stored = recognized.get(ROUTE_KEY)
    if isinstance(stored, dict) and stored != derived.as_dict():
        # The receipt is OCR-owned, but a drifted route must not obscure the
        # underlying participant evidence on a read-only worklist either.
        return MixedRoute("review", None, "routing_evidence_drift")
    return derived


def group_planned_rows(
    rows: list[tuple[int, str, Task | None]],
    display_tasks: dict[int, Task] | None = None,
) -> dict:
    """Keep every file occurrence in manifest order, including missing work.

    Names, hashes and case numbers are not grouping keys. A source document
    submitted twice in two positions remains two visible rows.
    """
    groups: dict[str, list[dict]] = {folder: [] for folder in FOLDERS}
    seen_indices: set[int] = set()
    for file_index, filename, task in sorted(rows, key=lambda row: row[0]):
        if file_index in seen_indices:
            raise ValueError("Duplicate planned file index")
        seen_indices.add(file_index)
        shown = display_tasks.get(task.id, task) if task is not None and display_tasks else task
        route = route_for_task(shown)
        groups[route.registry].append({
            "file_index": file_index,
            "filename": filename,
            "task_id": task.id if task is not None else None,
            "display_task_id": shown.id if shown is not None else None,
            "status": shown.status.value if shown is not None else None,
            "subject_kind": route.subject_kind,
            "reason": route.reason,
        })
    return {"groups": groups, "counts": {name: len(items) for name, items in groups.items()},
            "total": len(rows)}


def batch_registry_groups(db: Session, batch_id: str) -> dict:
    """Read one immutable batch and only tasks bound to its accepted chunks.

    This does not create/retry tasks and never groups other operations that
    happen to have identical names, content hashes, cases or people.
    """
    batch = db.get(UploadTransportBatch, batch_id)
    if batch is None:
        raise ValueError("Batch not found")
    item_position = (
        UploadTransportBatchItem.file_index
        - UploadTransportBatchItem.chunk_index * batch.files_per_chunk
    )
    rows = db.execute(
        select(UploadTransportBatchItem.file_index, UploadTransportBatchItem.filename, Task)
        .outerjoin(
            UploadTransportChunkReceipt,
            and_(
                UploadTransportChunkReceipt.batch_id == UploadTransportBatchItem.batch_id,
                UploadTransportChunkReceipt.chunk_index == UploadTransportBatchItem.chunk_index,
            ),
        )
        .outerjoin(
            IntakeItem,
            and_(
                IntakeItem.operation_id == UploadTransportChunkReceipt.operation_id,
                IntakeItem.item_index == item_position,
                IntakeItem.filename == UploadTransportBatchItem.filename,
            ),
        )
        .outerjoin(
            Document,
            and_(
                Document.id == IntakeItem.document_id,
                Document.content_hash == UploadTransportBatchItem.content_sha256,
            ),
        )
        .outerjoin(
            Task,
            and_(
                Task.intake_item_id == IntakeItem.id,
                Task.intake_operation_id == UploadTransportChunkReceipt.operation_id,
                Task.document_id == Document.id,
            ),
        )
        .where(UploadTransportBatchItem.batch_id == batch_id)
        .order_by(UploadTransportBatchItem.file_index)
    ).all()
    if len(rows) != batch.file_count:
        raise ValueError("Batch manifest is incomplete")
    originals = {task.id: task for _index, _filename, task in rows if task is not None}
    display_tasks: dict[int, Task] = {}
    source_items = {task.intake_item_id for task in originals.values()
                    if task.intake_item_id is not None}
    if source_items:
        lineage = db.execute(
            select(OcrRetryRun, Task)
            .join(Task, Task.id == OcrRetryRun.child_task_id)
            .where(OcrRetryRun.source_intake_item_id.in_(source_items))
        ).all()
        by_parent = {run.parent_task_id: (run, child) for run, child in lineage}
        for original in originals.values():
            if original.status != TaskStatus.failed:
                continue
            current = original
            visited = {original.id}
            while current.id in by_parent:
                run, child = by_parent[current.id]
                if (child.id in visited
                        or run.source_intake_item_id != original.intake_item_id
                        or run.source_operation_id != original.intake_operation_id
                        or run.document_id != original.document_id
                        or child.document_id != original.document_id
                        or child.scenario != original.scenario
                        or child.profile != original.profile
                        or child.intake_item_id is not None
                        or child.intake_operation_id is not None):
                    break
                visited.add(child.id)
                current = child
            if current.id != original.id:
                display_tasks[original.id] = current
    return {"batch_id": batch_id, **group_planned_rows(rows, display_tasks)}
