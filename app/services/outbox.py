"""Durable at-least-once delivery for safe internal background work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
import hashlib
import uuid

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from ..db.models import JobOutbox, Task, TaskStatus
from ..workers.queues import QueueUnavailable, active_ocr_task_ids

OUTBOX_LEASE_SECONDS = 120
OUTBOX_RETRY_BASE_SECONDS = 5
OUTBOX_RETRY_MAX_SECONDS = 300
# Historical `kad` rows may still exist. They are not safe to create or
# dispatch; status_summary counts them as withheld.
SAFE_JOB_KINDS = frozenset(("ocr", "local_validation"))
DISPATCHABLE_JOB_KINDS = frozenset(("ocr", "local_validation"))


@dataclass(frozen=True)
class _DispatchTarget:
    task_id: int
    job_kind: str
    actor_id: int | None


@dataclass(frozen=True)
class OutboxSummary:
    pending: int
    eligible: int
    leased: int
    delayed: int
    oldest_age_seconds: int | None
    last_error_counts: tuple[tuple[str, int], ...]
    withheld: int = 0


class DispatchOutcome(StrEnum):
    CONFIRMED = "confirmed"
    DEFERRED = "deferred"
    ALREADY_STARTED = "already_started"


def _insert_intent(
    db: Session,
    *,
    task_id: int,
    job_kind: str,
    dedupe_key: str,
    actor_id: int | None = None,
) -> int:
    if job_kind not in SAFE_JOB_KINDS:
        raise ValueError("Unsupported outbox job kind")
    if (job_kind == "local_validation") != (actor_id is not None):
        raise ValueError("Outbox actor attribution does not match job kind")
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Durable job dispatch requires PostgreSQL")
    intent_insert = postgresql_insert(JobOutbox).values(
        task_id=task_id,
        job_kind=job_kind,
        dedupe_key=dedupe_key,
        actor_id=actor_id,
    )
    # This no-op update pins an existing dedupe row until the caller commits.
    # A dispatcher can no longer delete it between DO NOTHING and a follow-up
    # SELECT, while attempts, leases, delay and error state stay untouched.
    intent_id = db.scalar(
        intent_insert
        .on_conflict_do_update(
            constraint="uq_job_outbox_dedupe_key",
            set_={"dedupe_key": intent_insert.excluded.dedupe_key},
        )
        .returning(JobOutbox.id)
    )
    if intent_id is None:
        raise RuntimeError("Conflicting job outbox intent could not be read back")
    return intent_id


def create_intake_intent(
    db: Session,
    *,
    task_id: int,
    operation_id: int,
    job_kind: str,
) -> int:
    key_material = f"sro-robot/intake-dispatch/v1:{operation_id}:{task_id}:{job_kind}"
    dedupe_key = hashlib.sha256(key_material.encode("ascii")).hexdigest()
    return _insert_intent(
        db,
        task_id=task_id,
        job_kind=job_kind,
        dedupe_key=dedupe_key,
    )


def pending_intake_intents(db: Session, *, operation_id: int, task_ids: list[int]) -> list[int]:
    """Return pending retries and recreate missing delivery intents for lost OCR jobs."""
    if not task_ids:
        return []
    dedupe_by_task = {
        task_id: hashlib.sha256(
            f"sro-robot/intake-dispatch/v1:{operation_id}:{task_id}:ocr".encode("ascii")
        ).hexdigest()
        for task_id in task_ids
    }
    pending_rows = list(
        db.execute(
            select(JobOutbox.id, JobOutbox.dedupe_key)
            .where(
                JobOutbox.job_kind == "ocr",
                JobOutbox.dedupe_key.in_(tuple(dedupe_by_task.values())),
            )
            .order_by(JobOutbox.id)
        )
    )
    pending_keys = {row.dedupe_key for row in pending_rows}
    intent_ids = [row.id for row in pending_rows]
    unbacked_new_task_ids = [
        task.id
        for task in db.scalars(select(Task).where(Task.id.in_(task_ids)))
        if task.status == TaskStatus.new and dedupe_by_task[task.id] not in pending_keys
    ]
    active_tasks = (
        active_ocr_task_ids(set(unbacked_new_task_ids)) if unbacked_new_task_ids else set()
    )
    for task_id in unbacked_new_task_ids:
        if task_id not in active_tasks:
            intent_ids.append(
                create_intake_intent(
                    db,
                    task_id=task_id,
                    operation_id=operation_id,
                    job_kind="ocr",
                )
            )
    return intent_ids


def local_validation_dedupe_key(task_id: int, approval_event_id: int) -> str:
    key_material = f"sro-robot/local-validation/v2:{task_id}:{approval_event_id}"
    return hashlib.sha256(key_material.encode("ascii")).hexdigest()


def create_local_validation_intent(
    db: Session,
    *,
    task_id: int,
    actor_id: int,
    approval_event_id: int,
) -> int:
    dedupe_key = local_validation_dedupe_key(task_id, approval_event_id)
    return _insert_intent(
        db,
        task_id=task_id,
        job_kind="local_validation",
        dedupe_key=dedupe_key,
        actor_id=actor_id,
    )


def pending_intent_count(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(JobOutbox)) or 0)


def status_summary(db: Session) -> OutboxSummary:
    """Return aggregate dispatch state without exposing task or document data."""
    now = db.scalar(select(func.clock_timestamp()))
    eligible = and_(
        JobOutbox.job_kind.in_(DISPATCHABLE_JOB_KINDS),
        JobOutbox.available_at <= now,
        or_(JobOutbox.lease_until.is_(None), JobOutbox.lease_until <= now),
    )
    active_lease = and_(
        JobOutbox.job_kind.in_(DISPATCHABLE_JOB_KINDS),
        JobOutbox.lease_until > now,
    )
    delayed = and_(
        JobOutbox.job_kind.in_(DISPATCHABLE_JOB_KINDS),
        JobOutbox.available_at > now,
        or_(JobOutbox.lease_until.is_(None), JobOutbox.lease_until <= now),
    )
    withheld = ~JobOutbox.job_kind.in_(DISPATCHABLE_JOB_KINDS)
    row = db.execute(
        select(
            func.count(),
            func.sum(case((eligible, 1), else_=0)),
            func.sum(case((active_lease, 1), else_=0)),
            func.sum(case((delayed, 1), else_=0)),
            func.sum(case((withheld, 1), else_=0)),
            func.min(JobOutbox.created_at),
        ).select_from(JobOutbox)
    ).one()
    last_errors = tuple(
        (str(error_type), int(count))
        for error_type, count in db.execute(
            select(JobOutbox.last_error_type, func.count())
            .where(JobOutbox.last_error_type.is_not(None))
            .group_by(JobOutbox.last_error_type)
            .order_by(func.count().desc(), JobOutbox.last_error_type)
            .limit(5)
        )
    )
    oldest_created = row[5]
    oldest_age = (
        max(0, int((now - oldest_created).total_seconds()))
        if oldest_created is not None
        else None
    )
    return OutboxSummary(
        pending=int(row[0] or 0),
        eligible=int(row[1] or 0),
        leased=int(row[2] or 0),
        delayed=int(row[3] or 0),
        oldest_age_seconds=oldest_age,
        last_error_counts=last_errors,
        withheld=int(row[4] or 0),
    )


def _enqueue(intent: JobOutbox) -> None:
    from ..workers.queues import enqueue_recognize, enqueue_write

    if intent.job_kind == "ocr":
        enqueue_recognize(intent.task_id)
    elif intent.job_kind == "local_validation":
        enqueue_write(
            intent.task_id,
            live=False,
            user_id=intent.actor_id,
        )
    else:  # The database constraint and _insert_intent both fail closed.
        raise RuntimeError("Unsupported persisted outbox job kind")


def dispatch_intent(
    db: Session,
    intent_id: int,
    *,
    active_ocr_ids: set[int] | None = None,
) -> DispatchOutcome:
    """Claim one intent and distinguish confirmed, deferred and obsolete work."""
    now = db.scalar(select(func.clock_timestamp()))
    intent = db.scalar(
        select(JobOutbox)
        .where(JobOutbox.id == intent_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if intent is None:
        return DispatchOutcome.DEFERRED
    if intent.job_kind not in DISPATCHABLE_JOB_KINDS:
        return DispatchOutcome.DEFERRED
    if intent.available_at > now:
        return DispatchOutcome.DEFERRED
    if intent.lease_token is not None and intent.lease_until is not None and intent.lease_until > now:
        return DispatchOutcome.DEFERRED

    token = str(uuid.uuid4())
    intent.attempts += 1
    intent.lease_token = token
    intent.lease_until = now + timedelta(seconds=OUTBOX_LEASE_SECONDS)
    task_id = intent.task_id
    job_kind = intent.job_kind
    actor_id = intent.actor_id
    db.commit()

    claimed = _DispatchTarget(
        task_id=task_id,
        job_kind=job_kind,
        actor_id=actor_id,
    )
    try:
        if job_kind == "ocr":
            # Outbox recovery can race a queue job already being claimed. Do
            # this check only after the caller's task locks have been released:
            # an OCR worker uses SKIP LOCKED and would otherwise drop the
            # existing delivery while this intent is suppressed as "active".
            task_status = db.scalar(select(Task.status).where(Task.id == task_id))
            current = db.scalar(
                select(JobOutbox)
                .where(JobOutbox.id == intent_id, JobOutbox.lease_token == token)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current is None:
                db.rollback()
                return DispatchOutcome.DEFERRED
            if task_status != TaskStatus.new:
                db.delete(current)
                db.commit()
                return DispatchOutcome.ALREADY_STARTED
            active_ids = (
                active_ocr_ids
                if active_ocr_ids is not None
                else active_ocr_task_ids({task_id})
            )
            if task_id in active_ids:
                # Keep the durable intent: an already-active delivery can
                # have skipped a row locked by a concurrent recovery request.
                # Retry after a short delay, once no task lock is held here.
                current.available_at = db.scalar(select(func.clock_timestamp())) + timedelta(
                    seconds=OUTBOX_RETRY_BASE_SECONDS
                )
                current.lease_token = None
                current.lease_until = None
                db.commit()
                return DispatchOutcome.DEFERRED
        _enqueue(claimed)
    except QueueUnavailable as exc:
        current = db.scalar(
            select(JobOutbox)
            .where(JobOutbox.id == intent_id, JobOutbox.lease_token == token)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current is not None:
            delay = min(
                OUTBOX_RETRY_BASE_SECONDS * (2 ** min(current.attempts - 1, 6)),
                OUTBOX_RETRY_MAX_SECONDS,
            )
            retry_at = db.scalar(select(func.clock_timestamp())) + timedelta(seconds=delay)
            current.available_at = retry_at
            current.last_error_type = exc.error_type[:64]
            current.lease_token = None
            current.lease_until = None
        db.commit()
        raise QueueUnavailable(exc.error_type) from None

    current = db.scalar(
        select(JobOutbox)
        .where(JobOutbox.id == intent_id, JobOutbox.lease_token == token)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is None:
        db.rollback()
        return DispatchOutcome.DEFERRED
    db.delete(current)
    db.commit()
    return DispatchOutcome.CONFIRMED


def dispatch_required(db: Session, intent_id: int) -> None:
    outcome = dispatch_intent(db, intent_id)
    if outcome == DispatchOutcome.DEFERRED:
        raise QueueUnavailable("DispatchDeferred")


def dispatch_ready(db: Session, *, limit: int = 100) -> tuple[int, int]:
    """Dispatch eligible intents; return (confirmed, pending/leased)."""
    if limit < 1 or limit > 1000:
        raise ValueError("Outbox dispatch limit must be between 1 and 1000")
    now = db.scalar(select(func.clock_timestamp()))
    intent_rows = list(
        db.execute(
            select(JobOutbox.id, JobOutbox.task_id, JobOutbox.job_kind)
            .where(
                JobOutbox.job_kind.in_(DISPATCHABLE_JOB_KINDS),
                JobOutbox.available_at <= now,
                or_(JobOutbox.lease_until.is_(None), JobOutbox.lease_until <= now),
            )
            .order_by(JobOutbox.id)
            .limit(limit)
        )
    )
    ocr_intent_ids = [row.id for row in intent_rows if row.job_kind == "ocr"]
    active_ids: set[int] | None = None
    blocked_ocr_ids: set[int] = set()
    if ocr_intent_ids:
        ocr_task_ids = {row.task_id for row in intent_rows if row.job_kind == "ocr"}
        try:
            active_ids = active_ocr_task_ids(ocr_task_ids)
        except QueueUnavailable as exc:
            blocked_ocr_ids = set(ocr_intent_ids)
            for intent_id in ocr_intent_ids:
                _defer_unclaimed_intent(db, intent_id, exc.error_type)
    confirmed = 0
    for row in intent_rows:
        if row.id in blocked_ocr_ids:
            continue
        try:
            if row.job_kind == "ocr":
                dispatched = dispatch_intent(db, row.id, active_ocr_ids=active_ids)
            else:
                dispatched = dispatch_intent(db, row.id)
            if dispatched == DispatchOutcome.CONFIRMED:
                confirmed += 1
        except QueueUnavailable:
            continue
    remaining = pending_intent_count(db)
    return confirmed, remaining


def _defer_unclaimed_intent(db: Session, intent_id: int, error_type: str) -> None:
    """Back off one eligible intent after queue state could not be read."""
    now = db.scalar(select(func.clock_timestamp()))
    current = db.scalar(
        select(JobOutbox)
        .where(JobOutbox.id == intent_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if current is None:
        db.rollback()
        return
    if current.lease_token is not None and current.lease_until is not None and current.lease_until > now:
        db.commit()
        return
    if current.available_at > now:
        db.commit()
        return
    current.attempts += 1
    delay = min(OUTBOX_RETRY_BASE_SECONDS * (2 ** min(current.attempts - 1, 6)), OUTBOX_RETRY_MAX_SECONDS)
    current.available_at = now + timedelta(seconds=delay)
    current.last_error_type = error_type[:64]
    current.lease_token = None
    current.lease_until = None
    db.commit()
