"""Durable at-least-once delivery for safe internal background work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import uuid

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from ..db.models import JobOutbox
from ..workers.queues import QueueUnavailable

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
    inserted_id = db.scalar(
        postgresql_insert(JobOutbox)
        .values(
            task_id=task_id,
            job_kind=job_kind,
            dedupe_key=dedupe_key,
            actor_id=actor_id,
        )
        .on_conflict_do_nothing(constraint="uq_job_outbox_dedupe_key")
        .returning(JobOutbox.id)
    )
    if inserted_id is not None:
        return inserted_id
    existing_id = db.scalar(
        select(JobOutbox.id).where(JobOutbox.dedupe_key == dedupe_key)
    )
    if existing_id is None:
        raise RuntimeError("Conflicting job outbox intent could not be read back")
    return existing_id


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


def create_local_validation_intent(db: Session, *, task_id: int, actor_id: int) -> int:
    request_nonce = uuid.uuid4().hex
    key_material = f"sro-robot/local-validation/v1:{task_id}:{request_nonce}"
    dedupe_key = hashlib.sha256(key_material.encode("ascii")).hexdigest()
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


def dispatch_intent(db: Session, intent_id: int) -> bool:
    """Claim, enqueue and remove one intent; duplicate delivery is expected."""
    now = db.scalar(select(func.clock_timestamp()))
    intent = db.scalar(
        select(JobOutbox)
        .where(JobOutbox.id == intent_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if intent is None:
        return False
    if intent.job_kind not in DISPATCHABLE_JOB_KINDS:
        return False
    if intent.available_at > now:
        return False
    if intent.lease_token is not None and intent.lease_until is not None and intent.lease_until > now:
        return False

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
        return False
    db.delete(current)
    db.commit()
    return True


def dispatch_required(db: Session, intent_id: int) -> None:
    if not dispatch_intent(db, intent_id):
        raise QueueUnavailable("DispatchDeferred")


def dispatch_ready(db: Session, *, limit: int = 100) -> tuple[int, int]:
    """Dispatch eligible intents; return (confirmed, pending/leased)."""
    if limit < 1 or limit > 1000:
        raise ValueError("Outbox dispatch limit must be between 1 and 1000")
    now = db.scalar(select(func.clock_timestamp()))
    intent_ids = list(
        db.scalars(
            select(JobOutbox.id)
            .where(
                JobOutbox.job_kind.in_(DISPATCHABLE_JOB_KINDS),
                JobOutbox.available_at <= now,
                or_(JobOutbox.lease_until.is_(None), JobOutbox.lease_until <= now),
            )
            .order_by(JobOutbox.id)
            .limit(limit)
        )
    )
    confirmed = 0
    for intent_id in intent_ids:
        try:
            if dispatch_intent(db, intent_id):
                confirmed += 1
        except QueueUnavailable:
            continue
    remaining = pending_intent_count(db)
    return confirmed, remaining
