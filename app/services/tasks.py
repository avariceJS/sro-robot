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
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, selectinload

from sro_robot.core.models import card_to_fields
from sro_robot.documents import DEFAULT_PROFILE
from sro_robot.scenarios import DEFAULT_SCENARIO
from sro_robot.core.validators import normalize_case_number

from ..db.models import (
    Document,
    Event,
    IntakeItem,
    IntakeOperation,
    IntakeSource,
    TASK_STATUS_RU,
    Task,
    TaskStatus,
)
from ..settings import settings


def log(db: Session, task: Task | None, kind: str, message: str = "",
        user_id: int | None = None, payload: dict | None = None) -> None:
    """Queue one privacy-safe audit event using the fixed event contract.

    ``message`` remains accepted for source compatibility, but the persisted
    text comes from a fixed catalog. Payloads are small, typed, and allowlisted
    per event kind; document facts and arbitrary caller text do not belong here.
    """
    del message
    if not isinstance(kind, str) or kind not in _AUDIT_EVENT_MESSAGES:
        raise ValueError(f"Unsupported audit event kind: {kind}")
    event_payload = payload if payload is not None else {}
    if not isinstance(event_payload, dict):
        raise ValueError("Audit event payload must be an object")
    allowed_keys = _AUDIT_EVENT_PAYLOAD_KEYS[kind]
    if set(event_payload) - allowed_keys:
        raise ValueError("Audit event payload contains unsupported fields")
    for key, value in event_payload.items():
        if key in _AUDIT_COUNT_KEYS:
            valid = type(value) is int and value >= 0
        elif key in _AUDIT_BOOLEAN_KEYS:
            valid = type(value) is bool
        elif key == "request_id":
            valid = isinstance(value, str) and _AUDIT_REQUEST_ID.fullmatch(value) is not None
        elif key == "values_sha256":
            valid = isinstance(value, str) and _AUDIT_SHA256.fullmatch(value) is not None
        elif key == "claim_mode":
            valid = type(value) is str and value == "legacy_unleased"
        elif key == "action":
            valid = type(value) is str and value in OPERATOR_AUDIT_ACTIONS
        elif key == "reason":
            valid = type(value) is str and value == "precondition_not_met"
        elif key == "execution_mode":
            valid = type(value) is str and value == "local_validation"
        else:
            valid = False
        if not valid:
            raise ValueError(f"Audit event payload field is invalid: {key}")
    db.add(Event(
        task_id=task.id if task else None,
        user_id=user_id,
        kind=kind,
        message=_AUDIT_EVENT_MESSAGES[kind],
        payload=event_payload,
    ))


def values_digest(values: dict[str, str]) -> str:
    """Stable digest binding an operator approval to the exact reviewed fields."""
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IntakeOperationConflict(ValueError):
    """A previously accepted idempotency key was reused for another payload."""


class ExistingDocumentUnavailable(RuntimeError):
    """A referenced stored document is missing, unsafe, or no longer matches."""


def intake_manifest(source: str, item_digests: list[str]) -> str:
    """Hash ordered source item identities without storing source text or bytes."""
    digest = hashlib.sha256(b"sro-robot/intake-manifest/v1\0")
    digest.update(source.encode("ascii"))
    digest.update(b"\0")
    for item_digest in item_digests:
        try:
            digest.update(bytes.fromhex(item_digest))
        except ValueError as exc:
            raise ValueError("Intake manifest item digest must be hexadecimal") from exc
    return digest.hexdigest()


def begin_intake_operation(
    db: Session,
    *,
    user_id: int,
    idempotency_key: str,
    manifest_sha256: str,
) -> tuple[IntakeOperation, bool]:
    """Reserve or lock a user's durable idempotency key within this transaction."""
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Durable intake idempotency requires PostgreSQL")
    inserted_id = db.scalar(
        postgresql_insert(IntakeOperation)
        .values(
            user_id=user_id,
            idempotency_key=idempotency_key,
            manifest_sha256=manifest_sha256,
        )
        .on_conflict_do_nothing(constraint="uq_intake_operation_user_key")
        .returning(IntakeOperation.id)
    )
    if inserted_id is not None:
        operation = db.get(IntakeOperation, inserted_id)
        if operation is None:
            raise RuntimeError("New intake operation disappeared inside its transaction")
        return operation, True

    operation = db.scalar(
        select(IntakeOperation)
        .where(
            IntakeOperation.user_id == user_id,
            IntakeOperation.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )
    if operation is None:
        raise RuntimeError("Conflicting intake operation could not be read back")
    if operation.manifest_sha256 != manifest_sha256:
        raise IntakeOperationConflict("Idempotency key was already used for different input")
    return operation, False


def operation_tasks(db: Session, operation_id: int) -> list[Task]:
    return list(
        db.scalars(
            select(Task)
            .where(Task.intake_operation_id == operation_id)
            .order_by(Task.id)
        ).all()
    )


def create_intake_item(
    db: Session,
    *,
    operation_id: int,
    item_index: int,
    document: Document,
    filename: str,
    source: IntakeSource,
    received_by_id: int | None,
) -> IntakeItem:
    """Persist a source occurrence independently of content-addressed blob reuse."""
    item = IntakeItem(
        operation_id=operation_id,
        item_index=item_index,
        document_id=document.id,
        filename=filename,
        source=source,
        received_by_id=received_by_id,
    )
    db.add(item)
    db.flush()
    return item


def log_intake_replay(
    db: Session,
    task_rows: list[Task],
    user_id: int,
    request_id: str | None,
) -> None:
    for task in task_rows:
        log(
            db,
            task,
            "intake_replayed",
            "Повтор запроса приёма связан с существующим заданием",
            user_id,
            payload={"request_id": request_id} if request_id else None,
        )


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


class InvalidTaskTransition(ValueError):
    """A lifecycle operation was requested from a state it does not own."""


TASK_STATUS_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.new: frozenset(
        (TaskStatus.recognizing, TaskStatus.failed, TaskStatus.rejected)
    ),
    TaskStatus.recognizing: frozenset(
        (
            TaskStatus.recognizing,
            TaskStatus.needs_review,
            TaskStatus.failed,
            TaskStatus.duplicate,
        )
    ),
    TaskStatus.needs_review: frozenset((TaskStatus.approved, TaskStatus.rejected)),
    TaskStatus.approved: frozenset(
        (
            TaskStatus.needs_review,
            TaskStatus.approved,
            TaskStatus.rejected,
            TaskStatus.writing,
            TaskStatus.done,
            TaskStatus.failed,
        )
    ),
    TaskStatus.writing: frozenset((TaskStatus.done, TaskStatus.awaiting_preview)),
    TaskStatus.awaiting_preview: frozenset(),
    TaskStatus.done: frozenset(),
    TaskStatus.failed: frozenset(),
    TaskStatus.duplicate: frozenset(),
    TaskStatus.rejected: frozenset(),
}


# Audit text is a durable privacy boundary. Keep messages in a fixed catalog so
# exception text, filenames, OCR output, and operator input cannot be copied to
# the event stream by a future caller of ``log``.
_AUDIT_EVENT_MESSAGES = {
    "session_started": "Сотрудник вошёл в приложение",
    "session_login_failed": "Вход отклонён: учётные данные не подтверждены",
    "session_ended": "Сотрудник завершил браузерную сессию",
    "kad_lookup_started": "Поиск судебного акта в КАД начат",
    "kad_document_received": "Судебный акт получен; начинается разбор PDF",
    "not_approved": "Локальная проверка не выполнена: задание не подтверждено",
    "ocr_attempts_exhausted": "Лимит попыток локального распознавания исчерпан; требуется проверка оператора",
    "ocr_claimed": "Локальное распознавание захвачено воркером",
    "ocr_claim_completed": "Результат распознавания сохранён владельцем актуальной аренды",
    "ocr_claim_failed": "Локальное распознавание завершилось ошибкой",
    "approval_invalidated": "Подтверждение оператора больше не соответствует текущим полям",
    "operator_action_not_applied": "Операторское действие не применено: предусловие состояния не выполнено",
    "created": "Document received",
    "created_from_kad": "Получение судебного акта из КАД запланировано",
    "task_claimed": "Фоновая обработка принята воркером",
    "recognized": "Документ распознан",
    "fields_saved": "Оператор сохранил поля задания",
    "approved": "Оператор подтвердил поля",
    "rejected": "Оператор отклонил задание до активной обработки",
    "local_validation_requested": "Оператор запросил локальную проверку задания",
    "writing_started": "Task execution started",
    "done": "Task processing completed",
    "failed": "Обработка завершилась ошибкой; требуется проверка",
    "duplicate": "Duplicate document detected",
    "local_validation_completed": "Локальная проверка завершена; запись в СРО не выполнялась",
    "preview_started": "Operator preview is ready",
    "live_save_blocked": "Запись в реальный реестр СРО отключена",
    "decision_close": "Решение оператора по превью",
    "preview_timeout": "Время ожидания решения оператора истекло",
    "intake_replayed": "Повтор запроса приёма связан с существующим заданием",
}

_AUDIT_EVENT_PAYLOAD_KEYS = {
    "session_started": frozenset(("request_id",)),
    "session_login_failed": frozenset(("request_id",)),
    "session_ended": frozenset(("request_id",)),
    "kad_lookup_started": frozenset(),
    "kad_document_received": frozenset(),
    "not_approved": frozenset(),
    "ocr_attempts_exhausted": frozenset(("attempts",)),
    "ocr_claimed": frozenset(("attempt", "generation")),
    "ocr_claim_completed": frozenset(("attempt", "generation")),
    "ocr_claim_failed": frozenset(("attempt", "generation")),
    "approval_invalidated": frozenset(("request_id",)),
    "operator_action_not_applied": frozenset(("action", "reason", "request_id")),
    "created": frozenset(("request_id",)),
    "created_from_kad": frozenset(("request_id",)),
    "task_claimed": frozenset(("claim_mode",)),
    "recognized": frozenset(),
    "fields_saved": frozenset(("changed", "field_count", "values_sha256", "request_id")),
    "approved": frozenset(("values_sha256", "request_id")),
    "rejected": frozenset(("request_id",)),
    "local_validation_requested": frozenset(("execution_mode", "saved", "request_id")),
    "writing_started": frozenset(("dry_run",)),
    "done": frozenset(),
    "failed": frozenset(),
    "duplicate": frozenset(("count",)),
    "local_validation_completed": frozenset(("execution_mode", "saved")),
    "preview_started": frozenset(("screenshot_count",)),
    "live_save_blocked": frozenset(("saved", "request_id")),
    "decision_close": frozenset(("request_id",)),
    "preview_timeout": frozenset(),
    "intake_replayed": frozenset(("request_id",)),
}

_AUDIT_COUNT_KEYS = frozenset(
    ("attempts", "attempt", "generation", "count", "field_count", "screenshot_count")
)
_AUDIT_BOOLEAN_KEYS = frozenset(("changed", "saved", "dry_run"))
_AUDIT_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
_AUDIT_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def assert_task_status_transition(source: TaskStatus, target: TaskStatus) -> None:
    """Reject state changes outside the reviewed workflow transition graph."""
    if target not in TASK_STATUS_TRANSITIONS[source]:
        raise InvalidTaskTransition(
            f"Task status transition {source.value} -> {target.value} is not allowed"
        )


def transition_task_status(task: Task, target: TaskStatus) -> None:
    """Apply one allowed transition after the caller has established ownership."""
    assert_task_status_transition(task.status, target)
    task.status = target


OPERATOR_AUDIT_ACTIONS = frozenset(
    ("save_fields", "approve", "reject", "local_validation", "preview_decision")
)


def log_operator_action_not_applied(
    db: Session,
    task: Task,
    *,
    action: str,
    user_id: int,
    request_id: str | None = None,
) -> None:
    """Audit an authenticated action rejected by a current-state precondition."""
    if action not in OPERATOR_AUDIT_ACTIONS:
        raise ValueError("Unsupported operator audit action")
    payload = {"action": action, "reason": "precondition_not_met"}
    if request_id and re.fullmatch(r"[0-9a-f]{32}", request_id):
        payload["request_id"] = request_id
    log(
        db,
        task,
        "operator_action_not_applied",
        "Операторское действие не применено: предусловие состояния не выполнено",
        user_id,
        payload=payload,
    )
    db.flush()


def _lock_expected_status(
    db: Session,
    task: Task,
    allowed: tuple[TaskStatus, ...],
) -> None:
    lock_task_for_update(db, task)
    if task.status not in allowed:
        expected = ", ".join(status.value for status in allowed)
        raise InvalidTaskTransition(
            f"Task transition requires status in ({expected}); current status is {task.status.value}"
        )


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


def _stored_document_file_matches(path_value: str, content_hash: str) -> bool:
    """Check that a database-referenced document remains in private storage."""
    root = Path(os.path.abspath(settings.upload_dir))
    path = Path(os.path.abspath(path_value))
    if Path(os.path.abspath(path.parent)) != root:
        return False

    try:
        root_info = root.lstat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or (os.name == "posix" and root_info.st_uid != os.geteuid())
        ):
            return False
        if os.name == "posix" and stat.S_IMODE(root_info.st_mode) != 0o700:
            return False
    except OSError:
        return False

    return _existing_document_file_matches(path, content_hash)


def validate_stored_document_file(path_value: str, content_hash: str) -> None:
    """Raise a safe storage failure unless a stored reference is still intact."""
    if not _stored_document_file_matches(path_value, content_hash):
        raise ExistingDocumentUnavailable


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
        validate_stored_document_file(existing.stored_path, content_hash)
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
                request_id: str | None = None,
                intake_operation_id: int | None = None,
                intake_item_id: int | None = None) -> Task:
    task = Task(
        document_id=document.id,
        scenario=scenario,
        profile=profile,
        assignee_id=user_id,
        status=TaskStatus.new,
        recognized={},
        values={},
        intake_operation_id=intake_operation_id,
        intake_item_id=intake_item_id,
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
    intake_operation_id: int | None = None,
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
        intake_operation_id=intake_operation_id,
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

    # The SQL predicate remains the atomic concurrent-state fence; this
    # assertion documents the intended edge independently of row contents.
    assert_task_status_transition(TaskStatus.new, TaskStatus.recognizing)
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
    log(
        db,
        task,
        "task_claimed",
        "Фоновая обработка принята воркером",
        payload={"claim_mode": "legacy_unleased"},
    )
    db.flush()
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
    transition_task_status(task, TaskStatus.needs_review)
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
        transition_task_status(task, TaskStatus.needs_review)
        task.approved_by_id = None
        log(
            db,
            task,
            "approval_invalidated",
            "Поля изменены после подтверждения",
            user_id,
            payload={"request_id": request_id} if request_id else None,
        )
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
    transition_task_status(task, TaskStatus.approved)
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


def reject(
    db: Session,
    task: Task,
    user_id: int,
    reason: str = "",
    request_id: str | None = None,
) -> None:
    _lock_expected_status(
        db,
        task,
        (TaskStatus.new, TaskStatus.needs_review, TaskStatus.approved),
    )
    transition_task_status(task, TaskStatus.rejected)
    task.approved_by_id = None
    payload = {"request_id": request_id} if request_id else None
    log(
        db,
        task,
        "rejected",
        reason or "Оператор отклонил задание до активной обработки",
        user_id,
        payload=payload,
    )
    db.flush()


def request_local_validation(
    db: Session,
    task: Task,
    user_id: int,
    request_id: str | None = None,
) -> int | None:
    """Audit and admit a local validation request only for the current approval."""
    lock_task_for_update(db, task)
    if task.status != TaskStatus.approved or not approval_matches(db, task):
        return None
    log(
        db,
        task,
        "local_validation_requested",
        "Оператор запросил локальную проверку задания",
        user_id,
        payload={
            "execution_mode": "local_validation",
            "saved": False,
            **({"request_id": request_id} if request_id else {}),
        },
    )
    db.flush()
    from .outbox import create_local_validation_intent

    return create_local_validation_intent(db, task_id=task.id, actor_id=user_id)


def mark_writing(db: Session, task: Task, dry_run: bool) -> None:
    _lock_expected_status(db, task, (TaskStatus.approved,))
    if not approval_matches(db, task):
        raise InvalidTaskTransition("Task approval does not match its current values")
    transition_task_status(task, TaskStatus.writing)
    task.dry_run = dry_run
    task.error = ""
    log(
        db,
        task,
        "writing_started",
        "Task execution started",
        payload={"dry_run": dry_run},
    )
    db.flush()


def finish(db: Session, task: Task, record_url: str = "") -> None:
    _lock_expected_status(db, task, (TaskStatus.writing,))
    transition_task_status(task, TaskStatus.done)
    task.record_url = record_url
    log(db, task, "done", "Task processing completed")
    db.flush()


def fail(db: Session, task: Task, error: str) -> bool:
    """Fail local work only while its current state still permits that result.

    A delayed parser or validation error must not overwrite a newer terminal
    decision or any state whose outcome may require separate reconciliation.
    """
    from sqlalchemy import update

    db.flush()
    for source in (TaskStatus.recognizing, TaskStatus.approved):
        assert_task_status_transition(source, TaskStatus.failed)
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
    _lock_expected_status(db, task, (TaskStatus.recognizing,))
    transition_task_status(task, TaskStatus.duplicate)
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
    _lock_expected_status(db, task, (TaskStatus.approved,))
    if not approval_matches(db, task):
        raise InvalidTaskTransition("Task approval does not match its current values")
    transition_task_status(task, TaskStatus.done)
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


def begin_preview(
    db: Session,
    task: Task,
    screenshots: list[str],
) -> None:
    """Move a writing task to its operator preview state under a fresh row lock."""
    _lock_expected_status(db, task, (TaskStatus.writing,))
    task.screenshots = list(screenshots)
    task.browser_decision = ""
    transition_task_status(task, TaskStatus.awaiting_preview)
    log(
        db,
        task,
        "preview_started",
        "Operator preview is ready",
        payload={"screenshot_count": len(screenshots)},
    )
    db.flush()


def decide_preview(
    db: Session,
    task: Task,
    decision: str,
    user_id: int,
    request_id: str | None = None,
) -> bool:
    """Apply an operator preview decision only while preview is still current."""
    if decision not in ("save", "close"):
        raise ValueError("Preview decision must be save or close")
    lock_task_for_update(db, task)
    if task.status != TaskStatus.awaiting_preview or task.browser_decision in (
        "save",
        "close",
        "expired",
    ):
        return False
    task.browser_decision = decision
    if decision == "save":
        log(
            db,
            task,
            "live_save_blocked",
            "Запись в реальный реестр СРО отключена",
            user_id,
            payload={
                "saved": False,
                **({"request_id": request_id} if request_id else {}),
            },
        )
    else:
        log(
            db,
            task,
            f"decision_{decision}",
            "Решение оператора по превью",
            user_id,
            payload={"request_id": request_id} if request_id else None,
        )
    db.flush()
    return True


def expire_preview(db: Session, task: Task) -> bool:
    """Record the preview timeout only if the task is still awaiting review."""
    lock_task_for_update(db, task)
    if task.status != TaskStatus.awaiting_preview or task.browser_decision in (
        "save",
        "close",
        "expired",
    ):
        return False
    task.error = "Оператор не ответил, черновик закрыт"
    task.browser_decision = "expired"
    log(db, task, "preview_timeout", "Время ожидания решения оператора истекло")
    db.flush()
    return True


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
        select(Task)
        .options(selectinload(Task.intake_item))
        .order_by(Task.created_at.desc())
        .limit(limit)
    ))
