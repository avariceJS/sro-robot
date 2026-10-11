"""Задания: создание из документа и переходы по статусам.

Переходы собраны здесь, а не в роутах: их же будут вызывать приём с почты и
воркеры, у которых нет HTTP-запроса.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
import re
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from sro_robot.core.models import card_to_fields
from sro_robot.documents import DEFAULT_PROFILE
from sro_robot.scenarios import DEFAULT_SCENARIO
from sro_robot.core.validators import normalize_case_number

from ..db.models import Document, Event, IntakeSource, TASK_STATUS_RU, Task, TaskStatus
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


def values_digest(values: dict[str, str]) -> str:
    """Stable digest binding an operator approval to the exact reviewed fields."""
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def approval_matches(db: Session, task: Task) -> bool:
    """Return whether the latest approval event covers the current field values."""
    approval = db.scalar(
        select(Event)
        .where(Event.task_id == task.id, Event.kind == "approved")
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(1)
    )
    return bool(
        approval
        and approval.payload
        and approval.payload.get("values_sha256") == values_digest(task.values or {})
    )


def lock_task_for_update(db: Session, task: Task) -> None:
    """Refresh current task state and hold its row lock through transaction end."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": f"{settings.database_lock_timeout_ms}ms"},
        )
    db.refresh(task, with_for_update=True)


def _write_document_file(path: Path, content: bytes) -> None:
    """Create a private immutable document file without following or truncating paths."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    created_identity = None
    try:
        created_identity = os.fstat(descriptor)
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as document_file:
            descriptor = -1
            document_file.write(content)
            document_file.flush()
            os.fsync(document_file.fileno())
        if os.name == "posix":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created_identity is not None:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (
                    created_identity.st_dev,
                    created_identity.st_ino,
                ):
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        raise


def _existing_document_file_matches(path: Path, content_hash: str) -> bool:
    """Accept a stranded content-addressed file only when it is exact and private."""
    try:
        before_open = path.lstat()
        if not stat.S_ISREG(before_open.st_mode):
            return False
        if os.name == "posix" and stat.S_IMODE(before_open.st_mode) != 0o600:
            return False

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (before_open.st_dev, before_open.st_ino)
            ):
                return False
            if os.name == "posix":
                if stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_uid != os.geteuid():
                    return False

            with os.fdopen(descriptor, "rb") as document_file:
                descriptor = -1
                if hashlib.file_digest(document_file, "sha256").hexdigest() != content_hash:
                    return False
                os.fsync(document_file.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if os.name == "posix":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return True
    except OSError:
        return False


def _lock_document_hash(db: Session, content_hash: str) -> None:
    """Serialize same-content admission across PostgreSQL web processes."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    lock_digest = hashlib.sha256(
        b"sro-robot/document-hash-lock\0" + bytes.fromhex(content_hash)
    ).digest()
    lock_id = int.from_bytes(lock_digest[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})


def store_document(db: Session, filename: str, content: bytes, source: IntakeSource,
                   user_id: int | None = None) -> Document:
    """Кладёт файл на диск. Один и тот же скан из двух источников — один документ."""
    content_hash = hashlib.sha256(content).hexdigest()
    _lock_document_hash(db, content_hash)
    existing = db.scalar(select(Document).where(Document.content_hash == content_hash))
    if existing:
        return existing

    suffix = Path(filename).suffix.lower()
    path = settings.upload_dir / f"{content_hash}{suffix}"
    try:
        _write_document_file(path, content)
    except FileExistsError:
        if not _existing_document_file_matches(path, content_hash):
            raise

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
                profile: str = DEFAULT_PROFILE, user_id: int | None = None,
                request_id: str | None = None) -> Task:
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
    payload = {"request_id": request_id} if request_id else None
    log(db, task, "created", "Document received", user_id, payload=payload)
    return task


def create_kad_task(
    db: Session,
    case_number: str,
    user_id: int,
    request_id: str | None = None,
) -> Task:
    """Создаёт задание, чей PDF будет получен из карточки дела в КАД."""
    normalized = normalize_case_number(case_number)
    if not re.fullmatch(r"[А-ЯЁ]\d{1,3}-\d+/\d{2,4}", normalized):
        raise ValueError("Введите номер дела, например А99-12345/2099")

    token = uuid.uuid4().hex
    document = Document(
        filename=f"КАД {normalized}.pdf",
        stored_path=str(settings.upload_dir / f"{token}.pdf"),
        content_hash=hashlib.sha256(token.encode()).hexdigest(),
        source=IntakeSource.kad,
        uploaded_by_id=user_id,
    )
    db.add(document)
    db.flush()
    task = Task(
        document_id=document.id,
        scenario=DEFAULT_SCENARIO,
        profile=DEFAULT_PROFILE,
        assignee_id=user_id,
        status=TaskStatus.new,
        recognized={},
        values={},
        case_number=normalized,
    )
    db.add(task)
    db.flush()
    payload = {"request_id": request_id} if request_id else None
    log(
        db,
        task,
        "created_from_kad",
        "Получение судебного акта из КАД запланировано",
        user_id,
        payload=payload,
    )
    return task


def mark_recognizing(db: Session, task: Task) -> bool:
    """Claim a newly queued non-leased intake before its worker starts."""
    return claim_unleased_task(db, task)


def claim_unleased_task(db: Session, task: Task) -> bool:
    """Atomically claim one worker delivery only from the queued state.

    This is deliberately not a recovery mechanism: stale ``recognizing`` work
    needs the fenced lease path or explicit reconciliation. A duplicate RQ
    delivery must never overwrite review or approval state or repeat acquisition.
    """
    db.flush()
    from sqlalchemy import update

    claimed_id = db.scalar(
        update(Task)
        .where(Task.id == task.id, Task.status == TaskStatus.new)
        .values(status=TaskStatus.recognizing)
        .returning(Task.id)
        .execution_options(synchronize_session=False)
    )
    if claimed_id is None:
        return False
    db.refresh(task)
    return True


def save_recognized(db: Session, task: Task, card) -> bool:
    """Save OCR output only while the task is still owned by recognition.

    The refresh under a row lock prevents a late parser result from replacing a
    newer terminal or operator state held in another transaction.
    """
    lock_task_for_update(db, task)
    if task.status != TaskStatus.recognizing:
        return False
    task.recognized = card_to_fields(card)
    task.case_number = (card.case_number.value or "")[:64] if hasattr(card, "case_number") else ""
    task.status = TaskStatus.needs_review
    log(db, task, "recognized", "Документ распознан")
    db.flush()
    return True


def save_values(
    db: Session,
    task: Task,
    values: dict[str, str],
    user_id: int | None = None,
    request_id: str | None = None,
) -> None:
    """Правки оператора. Само по себе подтверждением не является."""
    lock_task_for_update(db, task)
    if task.status not in (TaskStatus.needs_review, TaskStatus.approved):
        raise ValueError(f"Нельзя редактировать поля задания в статусе {task.status.value}")
    normalized = {k: (v or "").strip() for k, v in values.items()}
    changed = normalized != (task.values or {})
    task.values = normalized
    payload = {
        "changed": changed,
        "field_count": len(normalized),
        "values_sha256": values_digest(normalized),
    }
    if request_id:
        payload["request_id"] = request_id
    log(
        db,
        task,
        "fields_saved",
        "Оператор сохранил поля задания",
        user_id,
        payload=payload,
    )
    if changed and task.status == TaskStatus.approved:
        task.status = TaskStatus.needs_review
        task.approved_by_id = None
        log(db, task, "approval_invalidated", "Поля изменены после подтверждения", user_id)
    db.flush()


def approve(
    db: Session,
    task: Task,
    user_id: int,
    request_id: str | None = None,
) -> None:
    lock_task_for_update(db, task)
    if task.status not in (TaskStatus.needs_review, TaskStatus.approved):
        raise ValueError(f"Нельзя подтверждать задание в статусе {task.status.value}")
    task.status = TaskStatus.approved
    task.approved_by_id = user_id
    log(
        db,
        task,
        "approved",
        "Оператор подтвердил поля",
        user_id,
        payload={
            "values_sha256": values_digest(task.values or {}),
            **({"request_id": request_id} if request_id else {}),
        },
    )
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


def fail(db: Session, task: Task, error: str) -> bool:
    """Fail local work only while its current state still permits that result.

    A delayed parser or validation error must not overwrite a newer terminal
    decision or any state whose outcome may require separate reconciliation.
    """
    from sqlalchemy import update

    db.flush()
    changed_id = db.scalar(
        update(Task)
        .where(
            Task.id == task.id,
            Task.status.in_((TaskStatus.recognizing, TaskStatus.approved)),
        )
        .values(status=TaskStatus.failed, error=error)
        .returning(Task.id)
        .execution_options(synchronize_session=False)
    )
    if changed_id is None:
        return False
    db.refresh(task)
    log(db, task, "failed", error)
    db.flush()
    return True


def mark_duplicate(db: Session, task: Task, names: list[str]) -> None:
    task.status = TaskStatus.duplicate
    log(
        db,
        task,
        "duplicate",
        "Duplicate document detected",
        payload={"count": len(names)},
    )
    db.flush()


def finish_local_validation(db: Session, task: Task, user_id: int | None = None) -> None:
    """Complete a local-only validation without opening or changing SRO."""
    task.status = TaskStatus.done
    task.dry_run = True
    task.record_url = ""
    task.error = ""
    log(
        db,
        task,
        "local_validation_completed",
        "Локальная проверка завершена; запись в СРО не выполнялась",
        user_id,
        payload={"execution_mode": "local_validation", "saved": False},
    )
    db.flush()


def status_labels(db: Session, task_rows: list[Task]) -> dict[int, str]:
    """Return clear labels with one event query for a task list."""
    candidates = [task for task in task_rows if task.status == TaskStatus.done and task.dry_run]
    latest_kinds: dict[int, str] = {}
    if candidates:
        latest = (
            select(
                Event.task_id.label("task_id"),
                Event.kind.label("kind"),
                func.row_number()
                .over(
                    partition_by=Event.task_id,
                    order_by=(Event.created_at.desc(), Event.id.desc()),
                )
                .label("row_number"),
            )
            .where(Event.task_id.in_([task.id for task in candidates]))
            .subquery()
        )
        latest_kinds = dict(db.execute(
            select(latest.c.task_id, latest.c.kind).where(latest.c.row_number == 1)
        ).all())

    labels: dict[int, str] = {}
    for task in task_rows:
        if task.status != TaskStatus.done or not task.dry_run:
            labels[task.id] = TASK_STATUS_RU[task.status]
        elif latest_kinds.get(task.id) == "local_validation_completed":
            labels[task.id] = "проверено локально"
        else:
            labels[task.id] = "старый прогон: запись не подтверждена"
    return labels


def status_label(db: Session, task: Task) -> str:
    """Disambiguate local validation from historical unverified dry-runs."""
    return status_labels(db, [task])[task.id]


def recent(db: Session, limit: int = 100) -> list[Task]:
    return list(db.scalars(
        select(Task).order_by(Task.created_at.desc()).limit(limit)
    ))
