"""PostgreSQL-backed ownership and fencing for retryable local OCR jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db.models import Document, IntakeSource, Task, TaskLease, TaskStatus
from . import tasks as task_service

OCR_JOB_KIND = "ocr"
OCR_JOB_TIMEOUT_SECONDS = 600
OCR_LEASE_RECOVERY_GRACE_SECONDS = 300
OCR_LEASE_SECONDS = OCR_JOB_TIMEOUT_SECONDS + OCR_LEASE_RECOVERY_GRACE_SECONDS
OCR_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class OcrClaim:
    task_id: int
    owner_token: str
    attempt: int
    generation: int
    lease_until: datetime


def claim_ocr(db: Session, task_id: int) -> OcrClaim | None:
    """Atomically claim an eligible local upload task or reclaim an expired lease."""
    task = db.scalar(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if task is None or task.document.source not in (IntakeSource.upload, IntakeSource.batch):
        return None
    if task.status not in (TaskStatus.new, TaskStatus.recognizing):
        return None

    now = db.scalar(select(func.clock_timestamp()))
    lease = db.scalar(
        select(TaskLease)
        .where(TaskLease.task_id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lease is not None and lease.job_kind != OCR_JOB_KIND:
        return None
    if lease is not None and lease.lease_until > now:
        return None
    if task.status == TaskStatus.recognizing and lease is None:
        # Legacy recognizing tasks have no fenced owner and require reconciliation.
        return None

    previous_attempts = lease.attempt if lease is not None else 0
    if previous_attempts >= OCR_MAX_ATTEMPTS:
        task.status = TaskStatus.failed
        task.error = "OCR worker attempts exhausted; operator review required"
        if lease is not None:
            lease.lease_until = now
        task_service.log(
            db,
            task,
            "ocr_attempts_exhausted",
            "Лимит попыток локального распознавания исчерпан; требуется проверка оператора",
            payload={"attempts": previous_attempts},
        )
        db.flush()
        return None

    attempt = previous_attempts + 1
    generation = (lease.generation + 1) if lease is not None else 1
    owner_token = str(uuid.uuid4())
    lease_until = now + timedelta(seconds=OCR_LEASE_SECONDS)
    if lease is None:
        lease = TaskLease(
            task_id=task_id,
            owner_token=owner_token,
            job_kind=OCR_JOB_KIND,
            attempt=attempt,
            generation=generation,
            lease_until=lease_until,
        )
        db.add(lease)
    else:
        lease.owner_token = owner_token
        lease.attempt = attempt
        lease.generation = generation
        lease.lease_until = lease_until
    task.status = TaskStatus.recognizing
    task.error = ""
    task_service.log(
        db,
        task,
        "ocr_claimed",
        "Локальное распознавание захвачено воркером",
        payload={"attempt": attempt, "generation": generation},
    )
    db.flush()
    return OcrClaim(task_id, owner_token, attempt, generation, lease_until)


def _locked_current_claim(db: Session, claim: OcrClaim) -> tuple[Task, TaskLease] | None:
    task = db.scalar(
        select(Task)
        .where(Task.id == claim.task_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if task is None:
        return None
    lease = db.scalar(
        select(TaskLease)
        .where(TaskLease.task_id == claim.task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lease is None:
        return None
    now = db.scalar(select(func.clock_timestamp()))
    if (
        task.status != TaskStatus.recognizing
        or lease.job_kind != OCR_JOB_KIND
        or lease.owner_token != claim.owner_token
        or lease.attempt != claim.attempt
        or lease.generation != claim.generation
        or lease.lease_until <= now
    ):
        return None
    return task, lease


def complete_ocr(db: Session, claim: OcrClaim, card) -> bool:
    """Persist recognized facts only while this generation still owns the lease."""
    current = _locked_current_claim(db, claim)
    if current is None:
        return False
    task, lease = current
    task_service.save_recognized(db, task, card)
    lease.lease_until = db.scalar(select(func.clock_timestamp()))
    task_service.log(
        db,
        task,
        "ocr_claim_completed",
        "Результат распознавания сохранён владельцем актуальной аренды",
        payload={"attempt": claim.attempt, "generation": claim.generation},
    )
    db.flush()
    return True


def fail_ocr(db: Session, claim: OcrClaim, error_type: str) -> bool:
    """Record terminal local OCR failure only for the current unexpired owner."""
    current = _locked_current_claim(db, claim)
    if current is None:
        return False
    task, lease = current
    task_service.fail(db, task, f"Document recognition failed ({error_type})")
    lease.lease_until = db.scalar(select(func.clock_timestamp()))
    task_service.log(
        db,
        task,
        "ocr_claim_failed",
        "Локальное распознавание завершилось ошибкой",
        payload={"attempt": claim.attempt, "generation": claim.generation},
    )
    db.flush()
    return True


def reconcilable_ocr_task_ids(db: Session) -> list[int]:
    """Return eligible upload tasks missing from RQ or with expired OCR leases."""
    now = db.scalar(select(func.clock_timestamp()))
    statement = (
        select(Task.id)
        .join(Document, Document.id == Task.document_id)
        .outerjoin(TaskLease, TaskLease.task_id == Task.id)
        .where(
            Document.source.in_((IntakeSource.upload, IntakeSource.batch)),
            or_(
                Task.status == TaskStatus.new,
                (Task.status == TaskStatus.recognizing)
                & (TaskLease.job_kind == OCR_JOB_KIND)
                & (TaskLease.lease_until <= now),
            ),
        )
        .order_by(Task.created_at, Task.id)
    )
    return list(db.scalars(statement))
