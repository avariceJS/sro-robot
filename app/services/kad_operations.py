"""Shared identity and replay rules for employee-selected KAD source PDFs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..db.models import (
    IntakeOperation,
    KadImportRequestReceipt,
    KadSourceRun,
    Task,
)
from . import kad_import, tasks


class KadSourceConflict(ValueError):
    """A request changes its identity or has an ambiguous legacy history."""


@dataclass(frozen=True)
class KadImportDecision:
    operation: IntakeOperation
    task: Task | None
    is_new: bool
    source_identity_sha256: str
    generation: int
    parent_run_id: int | None
    parent_task_id: int | None
    request_receipt: KadImportRequestReceipt


def source_identity(source: dict) -> str:
    """Identify the selected KAD PDF occurrence, never its case number or bytes."""
    material = (
        "sro-robot/kad-selected-pdf/v1\0"
        + source["cardUrl"]
        + "\0"
        + source["selectedUrl"]
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _request_hash(request_key: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{48}", request_key or ""):
        raise KadSourceConflict("Некорректный ключ приёма")
    return hashlib.sha256(bytes.fromhex(request_key)).hexdigest()


def _task_for_operation(db: Session, operation_id: int) -> Task:
    linked = list(db.scalars(
        select(Task).where(Task.intake_operation_id == operation_id).order_by(Task.id)
    ))
    if len(linked) != 1:
        raise KadSourceConflict("Не удалось однозначно восстановить задание этого источника")
    return linked[0]


def _legacy_runs(db: Session, source: dict) -> list[tuple[IntakeOperation, Task]]:
    source_json = Task.recognized["_kad_source_v1"]
    rows = db.execute(
        select(IntakeOperation, Task)
        .join(Task, Task.intake_operation_id == IntakeOperation.id)
        .where(
            source_json["cardUrl"].astext == source["cardUrl"],
            source_json["selectedUrl"].astext == source["selectedUrl"],
        )
        .order_by(Task.id)
    ).all()
    return list(rows)


def _assert_source_claim_matches(task: Task, source: dict) -> None:
    prior = (task.recognized or {}).get(kad_import.SOURCE_KEY)
    if not isinstance(prior, dict):
        raise KadSourceConflict("У выбранного PDF отсутствует подтверждённая история источника")
    if (
        prior.get("caseNumber") != source.get("caseNumber")
        or prior.get("actDate") != source.get("actDate")
    ):
        raise KadSourceConflict(
            "Для того же PDF указаны другие дело или дата акта. Сверьте выбранный документ."
        )


def _lock_source(db: Session, identity: str) -> None:
    lock_id = int.from_bytes(bytes.fromhex(identity)[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})


def begin_import(
    db: Session,
    *,
    user_id: int,
    request_key: str,
    manifest_sha256: str,
    source: dict,
    reprocess: bool,
) -> KadImportDecision:
    """Reserve a request and resolve its canonical selected-source run.

    Same-source, same-byte imports reuse the canonical task. Explicitly selected
    changed bytes create a linked source version, as does an explicit reprocess. The
    source-identity advisory lock serializes two employees arriving together.
    """
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Cross-employee KAD operation identity requires PostgreSQL")
    identity = source_identity(source)
    metadata_sha256 = kad_import.source_digest(source)
    request_hash = _request_hash(request_key)
    request = db.execute(
        pg_insert(KadImportRequestReceipt)
        .values(
            request_hash=request_hash,
            user_id=user_id,
            manifest_sha256=manifest_sha256,
            source_metadata_sha256=metadata_sha256,
            source_metadata=source,
            reprocess=reprocess,
        )
        .on_conflict_do_nothing(index_elements=[KadImportRequestReceipt.request_hash])
        .returning(KadImportRequestReceipt.request_hash)
    ).scalar_one_or_none()
    if request is None:
        receipt = db.scalar(
            select(KadImportRequestReceipt)
            .where(KadImportRequestReceipt.request_hash == request_hash)
            .with_for_update()
        )
        if (
            receipt is None
            or receipt.user_id != user_id
            or receipt.manifest_sha256 != manifest_sha256
            or receipt.source_metadata_sha256 != metadata_sha256
            or receipt.reprocess != reprocess
            or receipt.operation_id is None
        ):
            raise KadSourceConflict("Ключ приёма уже связан с другим действием")
        operation = db.get(IntakeOperation, receipt.operation_id)
        if operation is None:
            raise KadSourceConflict("Операция предыдущего приёма недоступна")
        task = _task_for_operation(db, operation.id)
        run = db.scalar(select(KadSourceRun).where(KadSourceRun.operation_id == operation.id))
        if run is None:
            raise KadSourceConflict("Не найдена история запуска выбранного источника")
        parent_task_id = None
        if run.parent_run_id is not None:
            parent = db.get(KadSourceRun, run.parent_run_id)
            parent_task_id = parent.task_id if parent else None
        return KadImportDecision(
            operation, task, False, identity, run.generation, run.parent_run_id,
            parent_task_id, receipt,
        )

    # Preserve compatibility with KAD requests accepted before source-level
    # receipts existed. The old per-user form key remains replay-safe.
    legacy_request = db.scalar(
        select(IntakeOperation).where(
            IntakeOperation.user_id == user_id,
            IntakeOperation.idempotency_key == request_key,
        ).with_for_update()
    )
    if legacy_request is not None:
        if (
            legacy_request.manifest_sha256 != manifest_sha256
            or legacy_request.source_metadata_sha256 != metadata_sha256
        ):
            raise KadSourceConflict("Ключ приёма уже использован для другого PDF")
        task = _task_for_operation(db, legacy_request.id)
        source_value = (task.recognized or {}).get(kad_import.SOURCE_KEY) or {}
        if source_value.get("selectedUrl") != source["selectedUrl"]:
            raise KadSourceConflict("Ключ приёма относится к другому выбранному PDF")
        _assert_source_claim_matches(task, source)
        _lock_source(db, identity)
        run = db.scalar(select(KadSourceRun).where(KadSourceRun.operation_id == legacy_request.id))
        if run is None:
            prior = _legacy_runs(db, source)
            if len(prior) != 1 or prior[0][0].id != legacy_request.id:
                raise KadSourceConflict("История повторной обработки источника неоднозначна")
            run = KadSourceRun(
                source_identity_sha256=identity,
                generation=1,
                operation_id=legacy_request.id,
                task_id=task.id,
                parent_run_id=None,
                created_by_id=legacy_request.user_id,
            )
            db.add(run)
            db.flush()
        receipt = db.get(KadImportRequestReceipt, request_hash)
        receipt.operation_id = legacy_request.id
        return KadImportDecision(
            legacy_request, task, False, identity, run.generation, run.parent_run_id,
            None, receipt,
        )

    _lock_source(db, identity)
    runs = list(db.scalars(
        select(KadSourceRun)
        .where(KadSourceRun.source_identity_sha256 == identity)
        .order_by(KadSourceRun.generation.desc())
        .with_for_update()
    ))
    if not runs:
        legacy = _legacy_runs(db, source)
        if len(legacy) > 1:
            raise KadSourceConflict(
                "Для выбранного PDF уже есть несколько заданий без однозначной связи. "
                "Проверьте их перед новым запуском."
            )
        if legacy:
            prior_operation, prior_task = legacy[0]
            run = KadSourceRun(
                source_identity_sha256=identity,
                generation=1,
                operation_id=prior_operation.id,
                task_id=prior_task.id,
                parent_run_id=None,
                created_by_id=prior_operation.user_id,
            )
            db.add(run)
            db.flush()
            runs = [run]

    latest = runs[0] if runs else None
    latest_task = None
    if latest is not None:
        latest_task = _task_for_operation(db, latest.operation_id)
        _assert_source_claim_matches(latest_task, source)
    if latest is not None and not reprocess:
        operation = db.get(IntakeOperation, latest.operation_id)
        if operation is None:
            raise KadSourceConflict("Операция выбранного PDF недоступна")
        if operation.manifest_sha256 == manifest_sha256:
            task = latest_task
            assert task is not None
            receipt = db.get(KadImportRequestReceipt, request_hash)
            receipt.operation_id = operation.id
            return KadImportDecision(
                operation, task, False, identity, latest.generation, latest.parent_run_id,
                None, receipt,
            )
        # KAD can regenerate a stamped PDF at the same selected URL. The employee
        # selected these actual bytes in a fresh request, so retain them as a new
        # linked version rather than discard them or overwrite the earlier file.
        # Existing request-key replay checks above still require exact bytes.

    if reprocess and latest is None:
        raise KadSourceConflict(
            "Для выбранного PDF ещё нет завершённого приёма; сначала запустите обычную обработку."
        )

    operation, created = tasks.begin_intake_operation(
        db,
        user_id=user_id,
        idempotency_key=request_key,
        manifest_sha256=manifest_sha256,
        source_metadata_sha256=metadata_sha256,
    )
    if not created:
        # A matching generic receipt can only be a pre-migration KAD request;
        # newer requests are handled by the request receipt above.
        task = _task_for_operation(db, operation.id)
        run = db.scalar(select(KadSourceRun).where(KadSourceRun.operation_id == operation.id))
        if run is None:
            raise KadSourceConflict("Не удалось восстановить повтор предыдущего запуска")
        receipt = db.get(KadImportRequestReceipt, request_hash)
        receipt.operation_id = operation.id
        return KadImportDecision(
            operation, task, False, identity, run.generation, run.parent_run_id,
            None, receipt,
        )

    return KadImportDecision(
        operation,
        None,
        True,
        identity,
        latest.generation + 1 if latest is not None else 1,
        latest.id if latest is not None else None,
        latest.task_id if latest is not None else None,
        db.get(KadImportRequestReceipt, request_hash),
    )


def complete_import(db: Session, decision: KadImportDecision, task: Task, user_id: int) -> None:
    if not decision.is_new or decision.request_receipt.operation_id is not None:
        raise RuntimeError("KAD import reservation is not awaiting task completion")
    decision.request_receipt.operation_id = decision.operation.id
    db.add(KadSourceRun(
        source_identity_sha256=decision.source_identity_sha256,
        generation=decision.generation,
        operation_id=decision.operation.id,
        task_id=task.id,
        parent_run_id=decision.parent_run_id,
        created_by_id=user_id,
    ))
    db.flush()


def receipt_operation(db: Session, user_id: int, request_key: str) -> IntakeOperation | None:
    request_hash = _request_hash(request_key)
    receipt = db.scalar(select(KadImportRequestReceipt).where(
        KadImportRequestReceipt.request_hash == request_hash,
        KadImportRequestReceipt.user_id == user_id,
    ))
    if receipt is None or receipt.operation_id is None:
        return None
    return db.get(IntakeOperation, receipt.operation_id)
