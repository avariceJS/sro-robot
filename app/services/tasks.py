"""Задания: создание из документа и переходы по статусам.

Переходы собраны здесь, а не в роутах: их же будут вызывать приём с почты и
воркеры, у которых нет HTTP-запроса.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from sro_robot.core.models import card_to_fields
from sro_robot.documents import DEFAULT_PROFILE
from sro_robot.scenarios import DEFAULT_SCENARIO

from ..db.models import Document, Event, IntakeSource, Task, TaskStatus
from ..settings import settings


def log(db: Session, task: Task | None, kind: str, message: str = "",
        user_id: int | None = None, payload: dict | None = None) -> None:
    db.add(Event(
        task_id=task.id if task else None,
        user_id=user_id,
        kind=kind,
        message=message,
        payload=payload or {},
    ))


def store_document(db: Session, filename: str, content: bytes, source: IntakeSource,
                   user_id: int | None = None) -> Document:
    """Кладёт файл на диск. Один и тот же скан из двух источников — один документ."""
    content_hash = hashlib.sha256(content).hexdigest()
    existing = db.scalar(select(Document).where(Document.content_hash == content_hash))
    if existing:
        return existing

    suffix = Path(filename).suffix.lower()
    path = settings.upload_dir / f"{content_hash}{suffix}"
    path.write_bytes(content)

    document = Document(
        filename=filename,
        stored_path=str(path),
        content_hash=content_hash,
        source=source,
        uploaded_by_id=user_id,
    )
    db.add(document)
    db.flush()
    return document


def create_task(db: Session, document: Document, scenario: str = DEFAULT_SCENARIO,
                profile: str = DEFAULT_PROFILE, user_id: int | None = None) -> Task:
    task = Task(
        document_id=document.id,
        scenario=scenario,
        profile=profile,
        assignee_id=user_id,
        status=TaskStatus.new,
        recognized={},
        values={},
    )
    db.add(task)
    db.flush()
    log(db, task, "created", f"Документ «{document.filename}»", user_id)
    return task


def mark_recognizing(db: Session, task: Task) -> None:
    task.status = TaskStatus.recognizing
    db.flush()


def save_recognized(db: Session, task: Task, card) -> None:
    task.recognized = card_to_fields(card)
    task.case_number = (card.case_number.value or "")[:64] if hasattr(card, "case_number") else ""
    task.status = TaskStatus.needs_review
    log(db, task, "recognized", "Документ распознан")
    db.flush()


def save_values(db: Session, task: Task, values: dict[str, str], user_id: int | None = None) -> None:
    """Правки оператора. Само по себе подтверждением не является."""
    task.values = {k: (v or "").strip() for k, v in values.items()}
    db.flush()


def approve(db: Session, task: Task, user_id: int) -> None:
    task.status = TaskStatus.approved
    task.approved_by_id = user_id
    log(db, task, "approved", "Оператор подтвердил поля", user_id)
    db.flush()


def reject(db: Session, task: Task, user_id: int, reason: str = "") -> None:
    task.status = TaskStatus.rejected
    log(db, task, "rejected", reason, user_id)
    db.flush()


def mark_writing(db: Session, task: Task, dry_run: bool) -> None:
    task.status = TaskStatus.writing
    task.dry_run = dry_run
    task.error = ""
    db.flush()


def finish(db: Session, task: Task, record_url: str = "", message: str = "") -> None:
    task.status = TaskStatus.done
    task.record_url = record_url
    log(db, task, "done", message)
    db.flush()


def fail(db: Session, task: Task, error: str) -> None:
    task.status = TaskStatus.failed
    task.error = error
    log(db, task, "failed", error)
    db.flush()


def mark_duplicate(db: Session, task: Task, names: list[str]) -> None:
    task.status = TaskStatus.duplicate
    log(db, task, "duplicate", "; ".join(names[:10]), payload={"count": len(names)})
    db.flush()


def recent(db: Session, limit: int = 100) -> list[Task]:
    return list(db.scalars(
        select(Task).order_by(Task.created_at.desc()).limit(limit)
    ))
