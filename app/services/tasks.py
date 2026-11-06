"""Задания: создание из документа и переходы по статусам.

Переходы собраны здесь, а не в роутах: их же будут вызывать приём с почты и
воркеры, у которых нет HTTP-запроса.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from sro_robot.config import Config
from pathlib import Path
import re

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from sro_robot.core.models import card_to_fields
from sro_robot.documents import DEFAULT_PROFILE
from sro_robot.scenarios import DEFAULT_SCENARIO
from ..db.models import (
    Document,
    Event,
    IntakeItem,
    IntakeOperation,
    IntakeSource,
    JobOutbox,
    TASK_STATUS_RU,
    Task,
    TaskLocalFinalizationAttempt,
    TaskRoleAttestation,
    TaskStatus,
    User,
)
from .ocr_provenance import ocr_worker_task_eligibility
from ..settings import settings

REVIEW_SUGGESTIONS_KEY = "_review_suggestions_v1"
REVIEW_SUGGESTIONS_COMPLETE_KEY = "_review_suggestions_complete_v1"
MIXED_ROUTE_KEY = "_mixed_route_v1"
APPLICANT_EVIDENCE_KEY = "_applicant_evidence_v1"


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
    if kind in _AUDIT_EVENT_ACTOR_REQUIRED and type(user_id) is not int:
        raise ValueError("Audit event requires an attributable user")
    if kind in _AUDIT_EVENT_TASK_REQUIRED and (
        task is None or type(task.id) is not int
    ):
        raise ValueError("Audit event requires its task context")
    allowed_keys = _AUDIT_EVENT_PAYLOAD_KEYS[kind]
    if set(event_payload) - allowed_keys:
        raise ValueError("Audit event payload contains unsupported fields")
    if _AUDIT_EVENT_REQUIRED_PAYLOAD_KEYS.get(kind, frozenset()) - event_payload.keys():
        raise ValueError("Audit event payload is missing required fields")
    for key, value in event_payload.items():
        if key in _AUDIT_COUNT_KEYS:
            valid = type(value) is int and value >= 0
        elif key in _AUDIT_BOOLEAN_KEYS:
            valid = type(value) is bool
        elif key == "request_id":
            valid = isinstance(value, str) and _AUDIT_REQUEST_ID.fullmatch(value) is not None
        elif key in ("values_sha256", "preview_token"):
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


def intake_source_metadata_manifest(filenames: list[str]) -> str:
    """Hash the ordered exact source filenames without copying them into operation rows."""
    digest = hashlib.sha256(b"sro-robot/intake-source-metadata/v1\0")
    digest.update(len(filenames).to_bytes(4, "big"))
    for filename in filenames:
        if not isinstance(filename, str):
            raise ValueError("Intake source filenames must be strings")
        encoded = filename.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def begin_intake_operation(
    db: Session,
    *,
    user_id: int,
    idempotency_key: str,
    manifest_sha256: str,
    source_metadata_sha256: str | None,
) -> tuple[IntakeOperation, bool]:
    """Reserve or lock a user's durable idempotency key within this transaction."""
    if source_metadata_sha256 is not None and re.fullmatch(
        r"[0-9a-f]{64}", source_metadata_sha256
    ) is None:
        raise ValueError("Intake source metadata digest must be lowercase SHA-256")
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Durable intake idempotency requires PostgreSQL")
    inserted_id = db.scalar(
        postgresql_insert(IntakeOperation)
        .values(
            user_id=user_id,
            idempotency_key=idempotency_key,
            manifest_sha256=manifest_sha256,
            source_metadata_sha256=source_metadata_sha256,
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
    if (
        operation.source_metadata_sha256 is not None
        and operation.source_metadata_sha256 != source_metadata_sha256
    ):
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


def _latest_approval_event(db: Session, task_id: int) -> Event | None:
    return db.scalar(
        select(Event)
        .where(Event.task_id == task_id, Event.kind == "approved")
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(1)
    )


def _local_validation_request_for_approval(
    db: Session,
    task_id: int,
    approval_event_id: int,
) -> Event | None:
    return db.scalar(
        select(Event)
        .where(
            Event.task_id == task_id,
            Event.kind == "local_validation_requested",
            Event.id > approval_event_id,
        )
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(1)
    )


def approval_matches(db: Session, task: Task) -> bool:
    """Return whether the latest approval event covers the current field values."""
    approval = _latest_approval_event(db, task.id)
    return bool(
        approval
        and approval.payload
        and approval.payload.get("values_sha256") == values_digest(task.values or {})
    )


def approved_review_replay_matches(
    db: Session,
    task: Task,
    values: dict[str, str],
    user_id: int,
) -> bool:
    """Check under the task lock whether a stale form repeats committed approval data."""
    lock_task_for_update(db, task)
    normalized = {name: (value or "").strip() for name, value in values.items()}
    return bool(
        task.status == TaskStatus.approved
        and task.approved_by_id == user_id
        and (task.values or {}) == normalized
        and approval_matches(db, task)
    )


def saved_review_replay_matches(
    db: Session,
    task: Task,
    values: dict[str, str],
    user_id: int,
) -> bool:
    """Check whether this employee's exact field save was already committed."""
    lock_task_for_update(db, task)
    normalized = {name: (value or "").strip() for name, value in values.items()}
    latest_save = db.scalar(
        select(Event)
        .where(Event.task_id == task.id, Event.kind == "fields_saved")
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(1)
    )
    return bool(
        task.status in (TaskStatus.needs_review, TaskStatus.approved)
        and (task.values or {}) == normalized
        and latest_save is not None
        and latest_save.user_id == user_id
        and latest_save.payload
        and latest_save.payload.get("values_sha256") == values_digest(normalized)
    )


def rejected_action_replay_matches(db: Session, task: Task, user_id: int) -> bool:
    """Check whether this employee's terminal rejection already committed."""
    lock_task_for_update(db, task)
    latest_rejection = db.scalar(
        select(Event)
        .where(Event.task_id == task.id, Event.kind == "rejected")
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(1)
    )
    return bool(
        task.status == TaskStatus.rejected
        and latest_rejection is not None
        and latest_rejection.user_id == user_id
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
    "preview_claimed": "Подготовка формы СРО закреплена за воркером",
    "decision_save": "Оператор запросил сохранение показанной формы СРО",
    "preview_save_started": "Начата однократная попытка сохранения формы СРО",
    "preview_saved": "Новая карточка СРО сохранена и проверена",
    "preview_closed": "Форма СРО закрыта без сохранения",
    "preview_failed": "Подготовка или закрытие формы СРО не завершены; требуется проверка",
    "preview_save_unknown": "Результат сохранения СРО неизвестен; повтор запрещён",
    "live_save_blocked": "Запись в реальный реестр СРО отключена",
    "decision_close": "Решение оператора по превью",
    "preview_timeout": "Время ожидания решения оператора истекло",
    "intake_replayed": "Повтор запроса приёма связан с существующим заданием",
    "intake_recovery_requested": "Сотрудник запросил восстановление доставки OCR",
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
    "preview_started": frozenset(("screenshot_count", "preview_token")),
    "preview_claimed": frozenset(),
    "decision_save": frozenset(("preview_token", "request_id")),
    "preview_save_started": frozenset(("preview_token",)),
    "preview_saved": frozenset(),
    "preview_closed": frozenset(),
    "preview_failed": frozenset(),
    "preview_save_unknown": frozenset(),
    "live_save_blocked": frozenset(("saved", "request_id")),
    "decision_close": frozenset(("request_id",)),
    "preview_timeout": frozenset(),
    "intake_replayed": frozenset(("request_id",)),
    "intake_recovery_requested": frozenset(("request_id",)),
}

_AUDIT_EVENT_REQUIRED_PAYLOAD_KEYS = {
    "decision_save": frozenset(("preview_token",)),
    "preview_save_started": frozenset(("preview_token",)),
    "ocr_attempts_exhausted": frozenset(("attempts",)),
    "ocr_claimed": frozenset(("attempt", "generation")),
    "ocr_claim_completed": frozenset(("attempt", "generation")),
    "ocr_claim_failed": frozenset(("attempt", "generation")),
    "operator_action_not_applied": frozenset(("action", "reason")),
    "task_claimed": frozenset(("claim_mode",)),
    "fields_saved": frozenset(("changed", "field_count", "values_sha256")),
    "approved": frozenset(("values_sha256",)),
    "local_validation_requested": frozenset(("execution_mode", "saved")),
    "writing_started": frozenset(("dry_run",)),
    "duplicate": frozenset(("count",)),
    "local_validation_completed": frozenset(("execution_mode", "saved")),
    "preview_started": frozenset(("screenshot_count",)),
    "live_save_blocked": frozenset(("saved",)),
}

_AUDIT_EVENT_ACTOR_REQUIRED = frozenset(
    (
        "session_started",
        "session_ended",
        "operator_action_not_applied",
        "created",
        "created_from_kad",
        "fields_saved",
        "approved",
        "rejected",
        "local_validation_requested",
        "live_save_blocked",
        "decision_close",
        "preview_claimed",
        "decision_save",
        "preview_save_started",
        "preview_saved",
        "preview_closed",
        "preview_failed",
        "preview_save_unknown",
        "intake_replayed",
        "intake_recovery_requested",
    )
)
_AUDIT_EVENT_TASK_REQUIRED = frozenset(_AUDIT_EVENT_MESSAGES) - frozenset(
    ("session_started", "session_login_failed", "session_ended")
)

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
    directory_fd = -1
    descriptor = -1
    try:
        # Pin the parent before inspecting or opening the leaf. Checking the root
        # pathname and then reopening a child by pathname leaves a rename/symlink
        # window in which a valid-looking file outside storage could be accepted.
        if not hasattr(os, "O_NOFOLLOW"):
            return False
        parent_before_open = path.parent.lstat()
        if not stat.S_ISDIR(parent_before_open.st_mode):
            return False
        if os.name == "posix" and stat.S_IMODE(parent_before_open.st_mode) != 0o700:
            return False
        if os.name == "posix" and parent_before_open.st_uid != os.geteuid():
            return False

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
        directory_fd = os.open(path.parent, directory_flags)
        parent_opened = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(parent_opened.st_mode)
            or (parent_opened.st_dev, parent_opened.st_ino)
            != (parent_before_open.st_dev, parent_before_open.st_ino)
        ):
            return False
        if os.name == "posix" and (
            stat.S_IMODE(parent_opened.st_mode) != 0o700
            or parent_opened.st_uid != os.geteuid()
        ):
            return False

        before_open = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(before_open.st_mode):
            return False
        if os.name == "posix" and stat.S_IMODE(before_open.st_mode) != 0o600:
            return False

        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before_open.st_dev, before_open.st_ino)
        ):
            return False
        if os.name == "posix" and (
            stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_uid != os.geteuid()
        ):
            return False

        with os.fdopen(descriptor, "rb") as document_file:
            descriptor = -1
            if hashlib.file_digest(document_file, "sha256").hexdigest() != content_hash:
                return False
            os.fsync(document_file.fileno())

        if os.name == "posix":
            os.fsync(directory_fd)
        return True
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd >= 0:
            os.close(directory_fd)


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


@dataclass(frozen=True)
class HashedDocumentContent:
    """Bind one locally computed digest to immutable request bytes, never a client hash."""

    content: bytes = field(repr=False)
    sha256: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("Document content must be immutable bytes")
        object.__setattr__(self, "sha256", hashlib.sha256(self.content).hexdigest())


def store_document(db: Session, filename: str, content: bytes | HashedDocumentContent, source: IntakeSource,
                   user_id: int | None = None) -> Document:
    """Кладёт файл на диск. Один и тот же скан из двух источников — один документ."""
    prepared = content if isinstance(content, HashedDocumentContent) else HashedDocumentContent(content)
    content_hash = prepared.sha256
    content = prepared.content
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


def create_task(db: Session, document: Document, *, user_id: int,
                scenario: str = DEFAULT_SCENARIO, profile: str = DEFAULT_PROFILE,
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
        .where(
            Task.id == task.id,
            Task.status == TaskStatus.new,
            ocr_worker_task_eligibility(),
        )
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
    from .kad_import import SOURCE_KEY, check_case
    from sro_robot.core.entity_extract import EntityDebtorCard
    source = (task.recognized or {}).get(SOURCE_KEY)
    check_case(source, card)
    task.recognized = card_to_fields(card)
    suggestions = getattr(card, "review_suggestions", None)
    if suggestions:
        task.recognized[REVIEW_SUGGESTIONS_KEY] = suggestions
        task.recognized[REVIEW_SUGGESTIONS_COMPLETE_KEY] = (
            getattr(card, "review_suggestions_complete", False) is True
        )
    from sro_robot.core.mixed_classification import classify_card
    applicant_evidence = getattr(card, "applicant_evidence", None)
    if isinstance(applicant_evidence, dict):
        task.recognized[APPLICANT_EVIDENCE_KEY] = applicant_evidence
    task.recognized[MIXED_ROUTE_KEY] = classify_card(
        card, applicant_evidence=applicant_evidence,
    ).as_dict()
    if isinstance(card, EntityDebtorCard):
        task.scenario = "entity_review"
    if source is not None:
        task.recognized = {**task.recognized, SOURCE_KEY: source}
    task.case_number = (card.case_number.value or "")[:64] if hasattr(card, "case_number") else ""
    transition_task_status(task, TaskStatus.needs_review)
    log(db, task, "recognized", "Документ распознан")
    db.flush()
    # Preparation is in the same fenced transaction as OCR completion. It does
    # not approve the task and cannot dispatch an external operation.
    from .assignments import prepare_debtor_card_from_ocr
    prepare_debtor_card_from_ocr(db, task, card)
    route = (task.recognized or {}).get(MIXED_ROUTE_KEY)
    actor_id = getattr(task, "assignee_id", None)
    if (isinstance(route, dict) and route.get("registry") == "assignments"
            and route.get("reason") == "explicit_applicant" and type(actor_id) is int):
        # The OCR transaction creates only an internal, reviewable draft.
        # Source-task uniqueness makes worker redelivery replay-safe.
        from .assignments import create_applicant_assignment_draft
        create_applicant_assignment_draft(db, task.id, actor_id)
    return True


def review_token(task: Task) -> str:
    """Opaque snapshot token; include source data to detect concurrent OCR/edits."""
    snapshot = {"recognized": task.recognized or {}, "values": task.values or {},
                "status": task.status.value, "approved_by_id": task.approved_by_id,
                "updated_at": str(task.updated_at)}
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=True,
                                     default=str).encode()).hexdigest()


def assert_review_token(db: Session, task: Task, expected: str) -> None:
    lock_task_for_update(db, task)
    if not expected or expected != review_token(task):
        raise ValueError("Задание изменилось. Обновите страницу и проверьте актуальные данные")


def save_values(
    db: Session,
    task: Task,
    values: dict[str, str],
    user_id: int,
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
    approval = _latest_approval_event(db, task.id)
    if approval is None:
        return None
    existing_request = _local_validation_request_for_approval(db, task.id, approval.id)
    if existing_request is not None:
        if existing_request.user_id != user_id:
            return None
        from .outbox import local_validation_dedupe_key

        return db.scalar(
            select(JobOutbox.id)
            .where(
                JobOutbox.dedupe_key == local_validation_dedupe_key(task.id, approval.id),
            )
            .order_by(JobOutbox.id.desc())
            .limit(1)
        )
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

    return create_local_validation_intent(
        db,
        task_id=task.id,
        actor_id=user_id,
        approval_event_id=approval.id,
    )


def local_validation_request_replay_matches(
    db: Session,
    task: Task,
    user_id: int,
) -> bool:
    """Check whether this employee already requested validation for this approval."""
    lock_task_for_update(db, task)
    if task.status not in (TaskStatus.approved, TaskStatus.done) or not approval_matches(db, task):
        return False
    approval = _latest_approval_event(db, task.id)
    if approval is None:
        return False
    existing_request = _local_validation_request_for_approval(db, task.id, approval.id)
    if existing_request is None or existing_request.user_id != user_id:
        return False
    if task.status == TaskStatus.approved:
        return True
    completion = db.scalar(
        select(Event)
        .where(
            Event.task_id == task.id,
            Event.kind == "local_validation_completed",
            Event.id > existing_request.id,
        )
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(1)
    )
    return bool(completion and completion.user_id == user_id)


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


def retry_sro_preview_after_reconciliation(db: Session, task: Task) -> bool:
    """Admit an operator-requested retry only when no form reached preview."""
    lock_task_for_update(db, task)
    if (
        task.status != TaskStatus.writing
        or not task.dry_run
        or not task.error
        or task.screenshots
        or task.browser_decision
        or not approval_matches(db, task)
    ):
        return False
    task.error = ""
    task.browser_decision = "retry"
    log(
        db,
        task,
        "writing_started",
        "Task execution started",
        payload={"dry_run": True},
    )
    db.flush()
    return True


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


def claim_sro_preview(db: Session, task: Task, user_id: int) -> bool:
    """Durably claim preparation once; a dead worker must not reopen a form."""
    lock_task_for_update(db, task)
    durable = getattr(task, "preview_mode", "live_session") == "durable_closed"
    if durable:
        from .fields import scenario_of
        qualified = (task.preview_purpose == "new_record_review"
                     and task.scenario in ("person_card", "entity_review")
                     and getattr(scenario_of(task), "supports_prepare", False))
    else:
        qualified = task.scenario == "person_card"
    if (
        task.status != TaskStatus.writing
        or not qualified
        or not task.dry_run
        or task.browser_decision not in ("", "retry")
        or task.screenshots
        or (not durable and not approval_matches(db, task))
    ):
        return False
    task.browser_decision = "preparing"
    log(db, task, "preview_claimed", user_id=user_id)
    db.flush()
    return True


def _sro_principal_digest(principal: str) -> str:
    normalized = principal.strip().casefold() if isinstance(principal, str) else ""
    if not normalized or len(normalized) > 255:
        raise ValueError("Verified SRO principal is required")
    return hashlib.sha256(b"sro-robot/owned-principal/v1\0" + normalized.encode("utf-8")).hexdigest()


def _owned_baseline_digest(baseline: dict[str, str]) -> str:
    if (not isinstance(baseline, dict) or not baseline or len(baseline) > 32
            or any(not isinstance(key, str) or not key or len(key) > 64
                   or not isinstance(value, str) or len(value) > 512
                   for key, value in baseline.items())):
        raise ValueError("Owned SRO draft baseline is invalid")
    encoded = json.dumps(baseline, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    if len(encoded) > 16384:
        raise ValueError("Owned SRO draft baseline is too large")
    return hashlib.sha256(b"sro-robot/owned-baseline/v1\0" + encoded).hexdigest()


def _owned_binding_matches(task: Task) -> bool:
    try:
        return bool(
            task.owned_add_state == "bound"
            and type(task.owned_add_actor_id) is int
            and task.owned_add_started_at is not None
            and task.owned_add_bound_at is not None
            and task.owned_baseline_schema_version == 1
            and re.fullmatch(r"[0-9a-f]{64}", task.owned_principal_sha256 or "")
            and task.owned_baseline_sha256 == _owned_baseline_digest(task.owned_baseline)
            and _verified_task_record_url(task, task.owned_draft_url)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _owned_principal_matches(db: Session, task: Task, user_id: int) -> bool:
    if not _owned_binding_matches(task):
        return False
    user = db.get(User, user_id)
    try:
        return bool(user is not None and user.is_active
                    and _sro_principal_digest(user.sro_login) == task.owned_principal_sha256)
    except ValueError:
        return False


def claim_owned_sro_add(
    db: Session, task: Task, user_id: int, principal: str,
) -> bool:
    """Commit this one-shot marker before any external Add request."""
    lock_task_for_update(db, task)
    user = db.get(User, user_id)
    try:
        principal_sha256 = _sro_principal_digest(principal)
        principal_matches = bool(user is not None and user.is_active
                                 and _sro_principal_digest(user.sro_login) == principal_sha256)
    except ValueError:
        return False
    if (not principal_matches or task.status != TaskStatus.writing
            or task.preview_mode != "durable_closed"
            or task.preview_purpose != "new_record_review"
            or task.browser_decision != "preparing"
            or task.owned_add_state != "not_started"
            or not task.dry_run):
        return False
    task.owned_add_state = "started"
    task.owned_add_actor_id = user_id
    task.owned_add_started_at = db.scalar(select(func.clock_timestamp()))
    task.owned_principal_sha256 = principal_sha256
    db.flush()
    return True


def complete_owned_sro_add(
    db: Session, task: Task, user_id: int, principal: str,
    record_url: str, baseline: dict[str, str],
) -> bool:
    """Persist exact Add-return GUID and blank readback before form filling."""
    lock_task_for_update(db, task)
    if (task.status != TaskStatus.writing or task.browser_decision != "preparing"
            or task.owned_add_state != "started"
            or task.owned_add_actor_id != user_id
            or not _verified_task_record_url(task, record_url)):
        return False
    try:
        if task.owned_principal_sha256 != _sro_principal_digest(principal):
            return False
        baseline_sha256 = _owned_baseline_digest(baseline)
    except ValueError:
        return False
    task.owned_add_state = "bound"
    task.owned_add_bound_at = db.scalar(select(func.clock_timestamp()))
    task.owned_draft_url = record_url
    task.owned_baseline = dict(baseline)
    task.owned_baseline_sha256 = baseline_sha256
    task.owned_baseline_schema_version = 1
    db.flush()
    return True


def fail_owned_sro_add(db: Session, task: Task, user_id: int) -> bool:
    """Hold an uncertain Add; a bound GUID is never discarded or retried."""
    lock_task_for_update(db, task)
    if task.owned_add_state != "started" or task.owned_add_actor_id != user_id:
        return False
    task.owned_add_state = "unknown"
    db.flush()
    return True


def preview_token(task: Task) -> str:
    """Bind decisions to the exact rendered form, without mutable lifecycle fields."""
    durable = getattr(task, "preview_mode", "live_session") == "durable_closed"
    snapshot = {
        "task_id": task.id,
        "screenshots": task.screenshots or [],
        "recognized": getattr(task, "recognized", None) or {},
        "values": task.values or {},
        "approved_by_id": None if durable else task.approved_by_id,
    }
    if durable:
        snapshot.update({
            "preview_mode": "durable_closed",
            "preview_purpose": task.preview_purpose,
            "preview_values_sha256": task.preview_values_sha256,
            "preview_schema_version": task.preview_schema_version,
            "preview_screenshot_sha256": task.preview_screenshot_sha256,
            "preview_destination": task.preview_destination,
            "owned_draft_url": getattr(task, "owned_draft_url", None),
            "owned_principal_sha256": getattr(task, "owned_principal_sha256", None),
            "owned_baseline_sha256": getattr(task, "owned_baseline_sha256", None),
            "owned_baseline_schema_version": getattr(task, "owned_baseline_schema_version", None),
            "preview_proposal_sha256": getattr(task, "preview_proposal_sha256", None),
            "preview_source_occurrence": getattr(task, "preview_source_occurrence", None),
            "preview_refresh_generation": getattr(task, "preview_refresh_generation", 0),
        })
    return hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _preview_snapshot_matches(db: Session, task: Task, user_id: int, token: str) -> bool:
    if not token or not task.dry_run or not task.screenshots or token != preview_token(task):
        return False
    durable = getattr(task, "preview_mode", "live_session") == "durable_closed"
    if durable and (
        getattr(task, "preview_refresh_state", "idle") != "idle"
        or
        task.preview_schema_version != 2
        or task.preview_values_sha256 != values_digest(task.values or {})
        or not _owned_binding_matches(task)
        or not isinstance(getattr(task, "preview_source_occurrence", None), dict)
        or not task.preview_source_occurrence
        or task.preview_proposal_sha256 != _preview_proposal_digest(task, task.preview_source_occurrence)
    ):
        return False
    if durable:
        destination = getattr(task, "preview_destination", None)
        if task.scenario == "entity_review":
            if (not isinstance(destination, dict)
                    or set(destination) != {"opf_label", "opf_value"}
                    or any(not isinstance(value, str) or not value.strip()
                           for value in destination.values())):
                return False
        elif destination != {}:
            return False
    if durable:
        from .artifacts import read_screenshot
        hashes = getattr(task, "preview_screenshot_sha256", None)
        if not isinstance(hashes, list) or len(hashes) != len(task.screenshots):
            return False
        try:
            if any(hashlib.sha256(read_screenshot(path)).hexdigest() != digest
                   for path, digest in zip(task.screenshots, hashes)):
                return False
        except (OSError, ValueError, TypeError):
            return False
    event = db.scalar(
        select(Event).where(Event.task_id == task.id, Event.kind == "preview_started")
        .order_by(Event.created_at.desc(), Event.id.desc()).limit(1)
    )
    if (
        event is None or (not durable and event.user_id != user_id)
        or not event.payload or event.payload.get("preview_token") != token
        or not isinstance(event.created_at, datetime)
    ):
        return False
    created_at = event.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    # Event timestamps originate in PostgreSQL. Compare against that same
    # clock so a worker/DB clock skew cannot invalidate a fresh preview.
    now = db.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime):
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age = (now - created_at).total_seconds()
    return (durable or 0 <= age <= 600) and (durable or approval_matches(db, task))


def can_save_preview(db: Session, task: Task, user_id: int) -> bool:
    """Return whether this employee can authorize this fresh screenshot snapshot."""
    return bool(
        task.status == TaskStatus.awaiting_preview and not task.browser_decision
        and _preview_snapshot_matches(db, task, user_id, preview_token(task))
        and (getattr(task, "preview_mode", "live_session") != "durable_closed"
             or (task.approved_by_id is not None and approval_matches(db, task)))
    )


def begin_preview(
    db: Session,
    task: Task,
    screenshots: list[str],
    user_id: int | None = None,
) -> None:
    """Publish screenshots; authorization remains separate from opening the form."""
    _lock_expected_status(db, task, (TaskStatus.writing,))
    task.screenshots = list(screenshots)
    task.browser_decision = ""
    transition_task_status(task, TaskStatus.awaiting_preview)
    log(
        db, task, "preview_started", user_id=user_id,
        payload={"screenshot_count": len(screenshots), "preview_token": preview_token(task)},
    )
    db.flush()


def _canonical_sha256(domain: bytes, payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(domain + encoded).hexdigest()


def _preview_proposal_digest(task: Task, occurrence: dict) -> str:
    return _canonical_sha256(b"sro-robot/preview-proposal/v1\0", {
        "recognized": task.recognized or {},
        "occurrence": occurrence,
    })


def _mixed_preview_proposal(task: Task) -> tuple[dict[str, str], dict, str] | None:
    """Return one source-bound, scenario-valid proposal without changing OCR role."""
    from sro_robot.core.entity_extract import EntityDebtorCard
    from sro_robot.core.mixed_classification import classify_card
    from sro_robot.core.models import DebtorCard, card_from_fields
    from .fields import review_fields_with_occurrence

    recognized = task.recognized or {}
    route = recognized.get(MIXED_ROUTE_KEY)
    expected = {
        "person_card": {("persons", "explicit_person_debtor"),
                        ("persons", "review_person_candidate")},
        "entity_review": {("organizations", "explicit_entity_debtor"),
                          ("organizations", "review_entity_candidate")},
    }.get(task.scenario)
    if not isinstance(route, dict) or expected is None:
        return None
    route_key = (route.get("registry"), route.get("reason"))
    if route_key not in expected:
        return None
    card_class = DebtorCard if task.scenario == "person_card" else EntityDebtorCard
    card = card_from_fields(card_class, recognized)
    card.review_suggestions = recognized.get(REVIEW_SUGGESTIONS_KEY) or []
    actual = classify_card(card, applicant_evidence=recognized.get(APPLICANT_EVIDENCE_KEY))
    if (actual.registry, actual.reason) != route_key:
        return None
    values = dict(task.values or {})
    if route_key[1] == "review_entity_candidate":
        from sro_robot.core.entity_extract import ENTITY_CANDIDATE_SOURCE
        from sro_robot.core.validators import digits, valid_inn
        kind = str((recognized.get("entity_kind") or {}).get("value") or "").strip()
        name_attr = "legal_name" if kind == "legal_entity" else "debtor_name"
        if kind not in ("legal_entity", "sole_proprietor"):
            return None
        attributes = ("entity_kind", name_attr, "inn")
        source_fields = {attr: recognized.get(attr) for attr in attributes}
        if any(not isinstance(source_fields[attr], dict)
               or source_fields[attr].get("source") != ENTITY_CANDIDATE_SOURCE
               or not str(source_fields[attr].get("value") or "").strip()
               or not str(source_fields[attr].get("warning") or "").strip()
               or any(type(source_fields[attr].get(key)) is not int
                      for key in ("source_variant", "source_start", "source_end"))
               or source_fields[attr]["source_start"] >= source_fields[attr]["source_end"]
               for attr in attributes):
            return None
        inn = str(source_fields["inn"]["value"]).strip()
        if (not valid_inn(inn) or len(digits(inn)) != (10 if kind == "legal_entity" else 12)
                or len({source_fields[attr]["source_variant"] for attr in attributes}) != 1):
            return None
        suggestions = [item for item in card.review_suggestions
                       if isinstance(item, dict) and item.get("kind") == "entity_candidate"]
        valid_ids = {digits(str((item.get("fields") or {}).get("inn", {}).get("value") or ""))
                     for item in suggestions if valid_inn(str((item.get("fields") or {}).get("inn", {}).get("value") or ""))}
        if valid_ids != {digits(inn)}:
            return None
        def matches(item: dict) -> bool:
            fields = item.get("fields")
            if not isinstance(fields, dict):
                return False
            return all(isinstance(fields.get(attr), dict)
                       and all(fields[attr].get(key) == source_fields[attr].get(key)
                               for key in ("value", "source_variant", "source_start", "source_end"))
                       for attr in attributes)
        selected = [(index, item) for index, item in enumerate(suggestions) if matches(item)]
        if len(selected) != 1:
            return None
        index, chosen = selected[0]
        rows, _unused = review_fields_with_occurrence(task)
        shown = {row.attr: row.value.strip() for row in rows}
        if any(shown.get(attr) != str(source_fields[attr]["value"]).strip()
               for attr in attributes):
            return None
        for attr in ("short_name", "legal_form", "ogrn", "ogrnip", "kpp",
                     "address", "address_kind"):
            if attr in shown:
                shown[attr] = ""
        occurrence = {
            "kind": "review_entity_candidate",
            "suggestion_index": index,
            "source_variant": source_fields[name_attr]["source_variant"],
            "name_start": source_fields[name_attr]["source_start"],
            "name_end": source_fields[name_attr]["source_end"],
            "inn_start": source_fields["inn"]["source_start"],
            "inn_end": source_fields["inn"]["source_end"],
            "evidence_sha256": _canonical_sha256(
                b"sro-robot/entity-occurrence/v1\0", chosen,
            ),
        }
        return shown, occurrence, route_key[1]
    if route_key[1] != "review_person_candidate":
        occurrence = {
            "kind": route_key[1],
            "evidence_sha256": _canonical_sha256(b"sro-robot/explicit-role/v1\0", recognized),
        }
        return values, occurrence, route_key[1]

    fio_raw = recognized.get("fio_raw")
    if isinstance(fio_raw, dict):
        warning = str(fio_raw.get("warning") or "").casefold()
        if any(marker in warning for marker in (
            "несколько лиц в секции должника",
            "разных должников",
            "разные значения в секциях должника",
            "расходятся по полю должника",
        )):
            return None

    rows, selected = review_fields_with_occurrence(task)
    rows_by_attr = {row.attr: row for row in rows}
    if selected is None and isinstance(fio_raw, dict) and fio_raw.get("source") == "role_partial":
        # Bind the complete role-marked FIO to its own exact OCR span. Review
        # suggestions may contain near spellings or no winning cohort; none
        # of their identifiers or addresses enter this technical form.
        role_name = {attr: recognized.get(attr) for attr in
                     ("last_name", "first_name", "middle_name")}
        if (all(isinstance(role_name[attr], dict) for attr in role_name)
                and all(str(role_name[attr].get("value") or "").strip()
                        for attr in ("last_name", "first_name"))
                and all(str(role_name[attr].get("value") or "").strip()
                        == str(getattr(rows_by_attr.get(attr), "value", "") or "").strip()
                        for attr in role_name)):
            span = tuple(fio_raw.get(key) for key in
                         ("source_variant", "source_start", "source_end"))
            blocked_identifiers = any(
                isinstance(recognized.get(attr), dict)
                and recognized[attr].get("source") == "role_ambiguous"
                and any(marker in str(recognized[attr].get("warning") or "").casefold()
                        for marker in ("разные значения", "разных должников", "противореч", "расходятся"))
                for attr in ("inn", "snils")
            )
            if (not blocked_identifiers
                    and all(type(part) is int and part >= 0 for part in span)
                    and span[1] < span[2]
                    and all(tuple(role_name[attr].get(key) for key in
                                  ("source_variant", "source_start", "source_end")) == span
                            for attr in role_name if role_name[attr].get("value"))):
                occurrence = {
                    "kind": "role_partial_person_candidate",
                    "source_variant": span[0],
                    "source_page": role_name["last_name"].get("source_page"),
                    "source_start": span[1],
                    "source_end": span[2],
                    "evidence_sha256": _canonical_sha256(
                        b"sro-robot/role-partial-person/v1\0",
                        {"fio_raw": fio_raw, "name_fields": role_name},
                    ),
                }
                proposal_values = {row.attr: row.value.strip() for row in rows}
                for attr in ("inn", "snils", "address", "address_kind"):
                    if attr in proposal_values:
                        proposal_values[attr] = ""
                return proposal_values, occurrence, route_key[1]

    suggestions = [item for item in card.review_suggestions
                   if isinstance(item, dict) and item.get("kind") == "application_person"]
    if not suggestions:
        return None

    def field(item: dict, name: str) -> dict:
        fields = item.get("fields")
        candidate = fields.get(name) if isinstance(fields, dict) else None
        return candidate if isinstance(candidate, dict) else {}

    def value(item: dict, name: str) -> str:
        raw = field(item, name).get("value")
        return raw.strip() if isinstance(raw, str) else ""

    names = {
        tuple(value(item, attr).casefold() for attr in ("last_name", "first_name", "middle_name"))
        for item in suggestions
    }
    if (not names or any(not all(name[:2]) for name in names)
            or any(not {"last_name", "first_name"}.issubset(set(item.get("copyable_fields") or []))
                   for item in suggestions)):
        return None
    if len(names) > 1:
        authoritative_name = tuple(
            str((recognized.get(attr) or {}).get("value") or "").strip().casefold()
            for attr in ("last_name", "first_name", "middle_name")
        )
        if (isinstance(fio_raw, dict) and fio_raw.get("source") == "role_partial"
                and authoritative_name in names):
            chosen_name = authoritative_name
        else:
            # OCR-near spellings can arise from one person across variants.
            # A distinct-variant 4:1 majority is a review-only proposal, not
            # automatic debtor attribution. Comparable cohorts stay held.
            groups: dict[tuple[str, str, str], set[int | str]] = {}
            for index, item in enumerate(suggestions):
                name = tuple(value(item, attr).casefold()
                             for attr in ("last_name", "first_name", "middle_name"))
                variant = field(item, "last_name").get("source_variant")
                source_key = variant if type(variant) is int else f"item_{index}"
                groups.setdefault(name, set()).add(source_key)
            ranked = sorted(groups, key=lambda name: len(groups[name]), reverse=True)
            if (len(ranked) != 2 or len(groups[ranked[0]]) < 3
                    or len(groups[ranked[0]]) < 3 * len(groups[ranked[1]])
                    or sum(a != b for a, b in zip(ranked[0], ranked[1])) != 1):
                return None
            chosen_name = ranked[0]
        suggestions_for_identity = [item for item in suggestions if tuple(
            value(item, attr).casefold() for attr in ("last_name", "first_name", "middle_name")
        ) == chosen_name]
    else:
        suggestions_for_identity = suggestions
    for attr in ("inn", "snils"):
        observed = {value(item, attr).casefold() for item in suggestions_for_identity if value(item, attr)}
        if len(observed) > 1:
            return None
    if selected is None:
        return None
    index, chosen = selected
    if chosen not in suggestions_for_identity:
        return None
    for attr in ("last_name", "first_name", "middle_name", "inn", "snils"):
        selected_value = value(chosen, attr).casefold()
        shown_value = str(getattr(rows_by_attr.get(attr), "value", "") or "").strip().casefold()
        if selected_value and shown_value and selected_value != shown_value:
            return None
    proposal_values = {row.attr: row.value.strip() for row in rows}
    name_source = field(chosen, "last_name")
    occurrence = {
        "kind": "review_person_candidate",
        "suggestion_index": index,
        "source_variant": name_source.get("source_variant"),
        "source_page": name_source.get("source_page"),
        "source_start": name_source.get("source_start"),
        "source_end": name_source.get("source_end"),
        "evidence_sha256": _canonical_sha256(b"sro-robot/person-occurrence/v1\0", chosen),
    }
    return proposal_values, occurrence, route_key[1]


def _preview_identity_valid(task: Task, card: object) -> bool:
    """Check only identity needed to bind a technical blank SRO card."""
    if task.scenario == "person_card":
        return bool(getattr(getattr(card, "last_name", None), "value", None)
                    and getattr(getattr(card, "first_name", None), "value", None))
    if task.scenario == "entity_review":
        kind = getattr(getattr(card, "entity_kind", None), "value", None)
        name_attr = "legal_name" if kind == "legal_entity" else "debtor_name"
        return bool(kind in ("legal_entity", "sole_proprietor")
                    and getattr(getattr(card, name_attr, None), "value", None))
    return False


def _save_readiness_errors(task: Task) -> list[str]:
    """Keep deterministic missing/unmapped fields out of a one-shot Save claim."""
    from .fields import card_of, scenario_of
    scenario = scenario_of(task)
    card = card_of(task)
    missing = list(scenario.validate(card))
    save_blockers = getattr(scenario, "save_blockers", None)
    if callable(save_blockers):
        missing.extend(save_blockers(card))
    return missing


def _refresh_identity(task: Task, values: dict[str, str]) -> tuple[str, ...] | None:
    """The owned GUID stays attached to its original debtor identity."""
    from .fields import scenario_of
    from sro_robot.core.models import card_from_fields
    scenario = scenario_of(task)
    card = scenario.card_from_values(
        values, base=card_from_fields(scenario.card_class, task.recognized or {}),
    )
    if not _preview_identity_valid(task, card):
        return None
    if task.scenario == "person_card":
        attrs = ("last_name", "first_name", "middle_name")
    elif task.scenario == "entity_review":
        attrs = ("entity_kind", "legal_name", "debtor_name")
    else:
        return None
    return tuple(str(getattr(getattr(card, attr, None), "value", "") or "").strip().casefold()
                 for attr in attrs)


def mixed_refresh_eligibility(db: Session, task: Task, user_id: int) -> dict[str, str | bool]:
    """Advisory only; request rechecks the exact preview token under lock."""
    if task.preview_mode != "durable_closed" or task.status != TaskStatus.awaiting_preview:
        return {"eligible": False, "reason": "preview_not_ready"}
    if getattr(task, "preview_refresh_state", "idle") != "idle":
        return {"eligible": False, "reason": "refresh_in_progress_or_unknown"}
    if (task.browser_decision or task.preview_save_enabled or task.approved_by_id is not None
            or db.get(TaskRoleAttestation, task.id) is not None):
        return {"eligible": False, "reason": "save_already_authorized"}
    if not _owned_principal_matches(db, task, user_id):
        return {"eligible": False, "reason": "owned_principal_mismatch"}
    if not _preview_snapshot_matches(db, task, user_id, preview_token(task)):
        return {"eligible": False, "reason": "preview_changed"}
    return {"eligible": True, "reason": "same_owned_draft"}


def request_owned_preview_refresh(
    db: Session, task: Task, user_id: int, expected_token: str,
    new_values: dict[str, str], request_id: str,
) -> bool:
    """Record an edit intent; the worker consumes it immediately before browser I/O."""
    lock_task_for_update(db, task)
    if not isinstance(request_id, str) or not _AUDIT_REQUEST_ID.fullmatch(request_id):
        return False
    from .fields import scenario_of
    scenario = scenario_of(task)
    allowed = {spec.attr for spec in scenario.fields}
    if (not isinstance(new_values, dict) or not set(new_values).issubset(allowed)
            or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 2048
                   for k, v in new_values.items())):
        return False
    normalized = {key: value.strip() for key, value in new_values.items()}
    new_digest = values_digest(normalized)
    if getattr(task, "preview_refresh_state", "idle") == "requested":
        return bool(task.preview_refresh_actor_id == user_id
                    and _owned_principal_matches(db, task, user_id)
                    and task.preview_refresh_request_id == request_id
                    and task.preview_refresh_previous_token == expected_token
                    and task.preview_refresh_values_sha256 == new_digest
                    and values_digest(task.values or {}) == new_digest)
    if (not mixed_refresh_eligibility(db, task, user_id)["eligible"]
            or not _preview_snapshot_matches(db, task, user_id, expected_token)
            or normalized == (task.values or {})
            or _refresh_identity(task, normalized) is None):
        return False
    task.values = normalized
    task.preview_refresh_state = "requested"
    task.preview_refresh_actor_id = user_id
    task.preview_refresh_request_id = request_id
    task.preview_refresh_previous_token = expected_token
    task.preview_refresh_values_sha256 = new_digest
    log(db, task, "fields_saved", user_id=user_id,
        payload={"changed": True, "field_count": len(normalized),
                 "values_sha256": new_digest, "request_id": request_id})
    db.flush()
    return True


def claim_owned_preview_refresh(
    db: Session, task: Task, user_id: int, request_id: str,
) -> bool:
    """Consume one queued refresh before any same-GUID GET or form filling."""
    lock_task_for_update(db, task)
    if (task.preview_refresh_state != "requested"
            or task.preview_refresh_actor_id != user_id
            or task.preview_refresh_request_id != request_id
            or task.preview_refresh_values_sha256 != values_digest(task.values or {})
            or task.status != TaskStatus.awaiting_preview
            or task.browser_decision or task.preview_save_enabled
            or task.approved_by_id is not None
            or not _owned_principal_matches(db, task, user_id)):
        return False
    task.preview_refresh_state = "started"
    log(db, task, "preview_claimed", user_id=user_id)
    db.flush()
    return True


def complete_owned_preview_refresh(
    db: Session, task: Task, user_id: int, screenshots: list[str],
    destination_snapshot: dict[str, str] | None = None,
) -> bool:
    """Publish post-Close screenshots of the same verified owned GUID."""
    lock_task_for_update(db, task)
    if (task.preview_refresh_state != "started"
            or task.preview_refresh_actor_id != user_id
            or task.preview_refresh_values_sha256 != values_digest(task.values or {})
            or task.status != TaskStatus.awaiting_preview
            or task.browser_decision or task.preview_save_enabled
            or not screenshots or not _owned_principal_matches(db, task, user_id)):
        return False
    if task.scenario == "entity_review":
        if (not isinstance(destination_snapshot, dict)
                or set(destination_snapshot) != {"opf_label", "opf_value"}
                or any(not isinstance(value, str) or not value.strip()
                       for value in destination_snapshot.values())):
            return False
    elif destination_snapshot not in (None, {}):
        return False
    from .artifacts import read_screenshot
    hashes = [hashlib.sha256(read_screenshot(path)).hexdigest() for path in screenshots]
    task.screenshots = list(screenshots)
    task.preview_screenshot_sha256 = hashes
    task.preview_values_sha256 = values_digest(task.values or {})
    task.preview_destination = dict(destination_snapshot or {})
    task.preview_refresh_generation += 1
    task.preview_refresh_state = "idle"
    log(db, task, "preview_started", user_id=user_id,
        payload={"screenshot_count": len(screenshots), "preview_token": preview_token(task)})
    db.flush()
    return True


def fail_owned_preview_refresh(db: Session, task: Task, user_id: int) -> bool:
    """Hold an uncertain form refresh; neither Add nor Save may be retried."""
    lock_task_for_update(db, task)
    if task.preview_refresh_state != "started" or task.preview_refresh_actor_id != user_id:
        return False
    task.preview_refresh_state = "unknown"
    log(db, task, "preview_failed", user_id=user_id)
    db.flush()
    return True


def mixed_preview_eligibility(task: Task) -> dict[str, str | bool]:
    """Read-only reason for the batch Prepare button; admission still locks."""
    if task.status != TaskStatus.needs_review:
        return {"eligible": False, "reason": "task_not_in_review"}
    if getattr(task, "owned_add_state", "not_started") != "not_started":
        return {"eligible": False, "reason": "owned_add_already_attempted"}
    proposal = _mixed_preview_proposal(task)
    if proposal is None:
        return {"eligible": False, "reason": "identity_or_role_ambiguous"}
    from .fields import scenario_of
    scenario = scenario_of(task)
    if not getattr(scenario, "supports_prepare", False):
        return {"eligible": False, "reason": "scenario_not_qualified"}
    values, _occurrence, reason = proposal
    from sro_robot.core.models import card_from_fields
    card = scenario.card_from_values(values, base=card_from_fields(scenario.card_class, task.recognized or {}))
    if not _preview_identity_valid(task, card):
        return {"eligible": False, "reason": "identity_incomplete"}
    return {"eligible": True, "reason": reason}


def admit_mixed_preview(db: Session, task: Task) -> bool:
    """Admit OCR evidence to an unsaved form preview without employee approval."""
    lock_task_for_update(db, task)
    proposal = _mixed_preview_proposal(task)
    if (task.status != TaskStatus.needs_review or proposal is None
            or getattr(task, "owned_add_state", "not_started") != "not_started"
            or task.approved_by_id is not None or task.browser_decision or task.screenshots):
        return False
    proposal_values, occurrence, _reason = proposal
    from .fields import scenario_of
    scenario = scenario_of(task)
    if not getattr(scenario, "supports_prepare", task.scenario == "person_card"):
        return False
    from sro_robot.core.models import card_from_fields
    card = scenario.card_from_values(
        proposal_values,
        base=card_from_fields(scenario.card_class, task.recognized or {}),
    )
    if not _preview_identity_valid(task, card):
        return False
    task.values = proposal_values
    task.preview_source_occurrence = occurrence
    task.preview_proposal_sha256 = _preview_proposal_digest(task, occurrence)
    task.preview_mode = "durable_closed"
    task.preview_purpose = "new_record_review"
    task.preview_save_enabled = False
    task.dry_run = True
    # This route has its own narrower database-enforced transition guard.
    task.status = TaskStatus.writing
    log(db, task, "writing_started", payload={"dry_run": True})
    db.flush()
    return True


def confirm_durable_preview(
    db: Session, task: Task, user_id: int, expected_token: str,
    request_id: str | None = None,
) -> bool:
    """Employee confirms the exact closed form snapshot after local review."""
    lock_task_for_update(db, task)
    if (task.preview_mode != "durable_closed" or task.preview_purpose != "new_record_review"
            or task.status != TaskStatus.awaiting_preview
            or task.browser_decision or not _preview_snapshot_matches(db, task, user_id, expected_token)
            or not _owned_principal_matches(db, task, user_id)
            or _save_readiness_errors(task)
            or task.preview_values_sha256 != values_digest(task.values or {})):
        return False
    existing = db.get(TaskRoleAttestation, task.id)
    occurrence_hash = _canonical_sha256(
        b"sro-robot/source-occurrence/v1\0", task.preview_source_occurrence,
    )
    if existing is not None:
        return bool(
            existing.actor_id == user_id and existing.role == "debtor"
            and existing.preview_token == expected_token
            and existing.proposal_sha256 == task.preview_proposal_sha256
            and existing.occurrence_sha256 == occurrence_hash
            and existing.values_sha256 == values_digest(task.values or {})
            and task.approved_by_id == user_id and approval_matches(db, task)
        )
    db.add(TaskRoleAttestation(
        task_id=task.id, actor_id=user_id, role="debtor",
        preview_token=expected_token,
        proposal_sha256=task.preview_proposal_sha256,
        occurrence_sha256=occurrence_hash,
        values_sha256=values_digest(task.values or {}),
    ))
    task.approved_by_id = user_id
    log(db, task, "approved", user_id=user_id,
        payload={"values_sha256": values_digest(task.values or {}),
                 **({"request_id": request_id} if request_id else {})})
    db.flush()
    return True


def _role_attestation_matches(db: Session, task: Task, token: str) -> bool:
    attestation = db.get(TaskRoleAttestation, task.id)
    if attestation is None or not isinstance(task.preview_source_occurrence, dict):
        return False
    return bool(
        attestation.role == "debtor"
        and attestation.actor_id == task.approved_by_id
        and attestation.preview_token == token
        and attestation.proposal_sha256 == task.preview_proposal_sha256
        and attestation.occurrence_sha256 == _canonical_sha256(
            b"sro-robot/source-occurrence/v1\0", task.preview_source_occurrence,
        )
        and attestation.values_sha256 == values_digest(task.values or {})
    )


def begin_durable_sro_preview(
    db: Session, task: Task, screenshots: list[str], user_id: int | None = None,
    *, destination_snapshot: dict[str, str] | None = None,
) -> None:
    """Record a verified, already closed blank form for later employee review.

    The caller must capture and check the form and Close result before entering
    this transaction. A preview alone never authorizes an external write.
    """
    _lock_expected_status(db, task, (TaskStatus.writing,))
    if (task.preview_mode != "durable_closed" or task.preview_purpose != "new_record_review" or not task.dry_run
            or task.browser_decision != "preparing" or not screenshots
            or not _owned_binding_matches(task)
            or not task.preview_source_occurrence
            or task.preview_proposal_sha256 != _preview_proposal_digest(task, task.preview_source_occurrence)
            or (task.approved_by_id is not None and not approval_matches(db, task))):
        raise InvalidTaskTransition("Durable closed preview prerequisites are missing")
    if task.scenario == "entity_review":
        if (not isinstance(destination_snapshot, dict)
                or set(destination_snapshot) != {"opf_label", "opf_value"}
                or any(not isinstance(value, str) or not value.strip()
                       for value in destination_snapshot.values())):
            raise InvalidTaskTransition("Qualified firm destination snapshot is missing")
    elif destination_snapshot not in (None, {}):
        raise InvalidTaskTransition("Person preview cannot carry a firm destination")
    task.screenshots = list(screenshots)
    task.browser_decision = ""
    task.preview_values_sha256 = values_digest(task.values or {})
    task.preview_schema_version = 2
    task.preview_destination = dict(destination_snapshot or {})
    from .artifacts import read_screenshot
    task.preview_screenshot_sha256 = [hashlib.sha256(read_screenshot(path)).hexdigest()
                                      for path in screenshots]
    task.preview_save_enabled = False
    transition_task_status(task, TaskStatus.awaiting_preview)
    log(db, task, "preview_started", user_id=user_id,
        payload={"screenshot_count": len(screenshots), "preview_token": preview_token(task)})
    db.flush()


def request_durable_sro_save(
    db: Session, task: Task, user_id: int, expected_token: str, request_id: str,
) -> bool:
    """An exact employee action authorizes one later, fresh form write."""
    lock_task_for_update(db, task)
    if (task.preview_mode != "durable_closed" or task.preview_purpose != "new_record_review"
            or task.status != TaskStatus.awaiting_preview
            or not _owned_principal_matches(db, task, user_id)
            or _save_readiness_errors(task)
            or task.browser_decision or task.preview_save_enabled or not request_id
            or not _preview_snapshot_matches(db, task, user_id, expected_token)
            or not _role_attestation_matches(db, task, expected_token)
            or task.approved_by_id is None or not approval_matches(db, task)):
        return False
    task.preview_save_enabled = True
    task.browser_decision = "save_requested"
    log(db, task, "decision_save", user_id=user_id,
        payload={"preview_token": expected_token, "request_id": request_id})
    db.flush()
    return True


def claim_durable_sro_save(
    db: Session, task: Task, user_id: int, expected_token: str,
) -> bool:
    """Consume authorization before browser I/O; an uncertain result is held."""
    lock_task_for_update(db, task)
    if (task.preview_mode != "durable_closed" or task.preview_purpose != "new_record_review"
            or task.status != TaskStatus.awaiting_preview
            or not _owned_principal_matches(db, task, user_id)
            or _save_readiness_errors(task)
            or task.browser_decision != "save_requested" or not task.preview_save_enabled
            or not _preview_snapshot_matches(db, task, user_id, expected_token)
            or not _role_attestation_matches(db, task, expected_token)
            or task.approved_by_id is None or not approval_matches(db, task)):
        return False
    decision = db.scalar(select(Event).where(
        Event.task_id == task.id, Event.kind == "decision_save",
    ).order_by(Event.created_at.desc(), Event.id.desc()).limit(1))
    if (decision is None or decision.user_id != user_id or not decision.payload
            or decision.payload.get("preview_token") != expected_token
            or not decision.payload.get("request_id")):
        return False
    task.browser_decision = "save_started"
    log(db, task, "preview_save_started", user_id=user_id,
        payload={"preview_token": expected_token})
    db.flush()
    return True


def decide_preview(
    db: Session,
    task: Task,
    decision: str,
    user_id: int,
    request_id: str | None = None,
    expected_token: str = "",
) -> bool:
    """Serialize operator choices; saving requires the current attributable snapshot."""
    if decision not in ("save", "close"):
        raise ValueError("Unsupported SRO preview decision")
    lock_task_for_update(db, task)
    if getattr(task, "preview_mode", "live_session") == "durable_closed":
        return False
    if task.status != TaskStatus.awaiting_preview or task.browser_decision:
        return False
    if decision == "save" and not _preview_snapshot_matches(db, task, user_id, expected_token):
        return False
    task.browser_decision = "save_requested" if decision == "save" else "close"
    payload = {"request_id": request_id} if request_id else {}
    if decision == "save":
        payload["preview_token"] = expected_token
    log(db, task, "decision_save" if decision == "save" else "decision_close",
        user_id=user_id, payload=payload)
    db.flush()
    return True


def consume_preview_save(db: Session, task: Task, user_id: int, expected_token: str) -> bool:
    """Commit this marker before network I/O; its absence never proves no side effect."""
    lock_task_for_update(db, task)
    if getattr(task, "preview_mode", "live_session") == "durable_closed":
        return False
    if (
        task.status != TaskStatus.awaiting_preview
        or task.browser_decision != "save_requested"
        or not _preview_snapshot_matches(db, task, user_id, expected_token)
    ):
        return False
    decision = db.scalar(
        select(Event).where(Event.task_id == task.id, Event.kind == "decision_save")
        .order_by(Event.created_at.desc(), Event.id.desc()).limit(1)
    )
    if (decision is None or decision.user_id != user_id or not decision.payload
            or decision.payload.get("preview_token") != expected_token):
        return False
    task.browser_decision = "save_started"
    log(db, task, "preview_save_started", user_id=user_id,
        payload={"preview_token": expected_token})
    db.flush()
    return True


def _verified_person_record_url(url: str) -> bool:
    """Accept receipts only for an identified card at the fixed trusted SRO origin."""
    if not isinstance(url, str) or len(url) > 1024:
        return False
    try:
        target = urlsplit(url)
        trusted = urlsplit(Config.base_url)
        query = parse_qs(target.query, keep_blank_values=True)
        return bool(
            target.scheme == "https" and target.netloc == trusted.netloc
            and target.path.casefold() == "/register/person_card.aspx"
            and set(query) == {"id"} and len(query["id"]) == 1
            and query["id"][0].strip() and not target.fragment
        )
    except ValueError:
        return False


def _verified_firm_record_url(url: str) -> bool:
    """Accept only a persisted firm-card receipt at the trusted SRO origin."""
    if not isinstance(url, str) or len(url) > 1024:
        return False
    try:
        target = urlsplit(url)
        trusted = urlsplit(Config.base_url)
        query = parse_qs(target.query, keep_blank_values=True)
        return bool(
            target.scheme == "https" and target.netloc == trusted.netloc
            and target.path.casefold() == "/register/firm_card.aspx"
            and set(query) == {"id"} and len(query["id"]) == 1
            and query["id"][0].strip() and not target.fragment
        )
    except ValueError:
        return False


def _verified_task_record_url(task: Task, url: str) -> bool:
    if task.scenario == "person_card":
        return _verified_person_record_url(url)
    if task.scenario == "entity_review":
        return _verified_firm_record_url(url)
    return False


def saved_sro_record_url(task: Task | None) -> str | None:
    """Project only a verified persisted record, never an unsaved form preview."""
    if (task is not None and getattr(task, "browser_decision", "") == "saved"
        and getattr(task, "dry_run", True) is False
        and _verified_task_record_url(task, getattr(task, "record_url", ""))
        and (getattr(task, "preview_mode", "live_session") != "durable_closed"
             or (_owned_binding_matches(task) and task.record_url == task.owned_draft_url))):
        return task.record_url
    return None


def complete_sro_preview(db: Session, task: Task, result, user_id: int) -> bool:
    """Keep the immutable preview status; only verified persistence becomes saved."""
    lock_task_for_update(db, task)
    if task.status != TaskStatus.awaiting_preview:
        return False
    if task.browser_decision == "save_started":
        if not (result.ok and result.saved and _verified_task_record_url(task, result.record_url)
                and (getattr(task, "preview_mode", "live_session") != "durable_closed"
                     or (_owned_binding_matches(task) and result.record_url == task.owned_draft_url
                         and _role_attestation_matches(db, task, preview_token(task))))):
            return fail_sro_preview(db, task, user_id)
        task.browser_decision = "saved"
        task.dry_run = False
        task.record_url = result.record_url
        task.error = ""
        log(db, task, "preview_saved", user_id=user_id)
        if getattr(task, "preview_purpose", "legacy") == "new_record_review":
            # Persist the verified external receipt before attempting local
            # projection. A projection failure rolls back only local drafts;
            # it must never turn a known external Save into a retryable job.
            task.local_finalization_state = "pending"
            db.flush()
            try:
                from .assignments import finalize_saved_sro_card_and_assignment
                with db.begin_nested():
                    finalize_saved_sro_card_and_assignment(db, task, user_id)
            except Exception:
                task.local_finalization_state = "failed"
                task.error = "Карточка СРО сохранена; внутренние карточка и назначение требуют сверки. Повтор Save запрещён."
            else:
                task.local_finalization_state = "succeeded"
            db.add(TaskLocalFinalizationAttempt(
                task_id=task.id, actor_id=user_id, outcome=task.local_finalization_state,
            ))
    elif not result.saved and result.ok and task.browser_decision in ("", "close", "expired"):
        if task.browser_decision != "expired":
            task.browser_decision = "closed"
            task.error = ""
        else:
            task.error = "Время ожидания истекло; форма закрыта без записи"
        log(db, task, "preview_closed", user_id=user_id)
    else:
        return fail_sro_preview(db, task, user_id)
    db.flush()
    return True


def reconcile_saved_local_finalization(db: Session, task: Task, user_id: int) -> bool:
    """Retry only local projection after a verified Save; never reopen SRO."""
    lock_task_for_update(db, task)
    if (getattr(task, "preview_purpose", "legacy") != "new_record_review"
            or not saved_sro_record_url(task)
            or task.local_finalization_state not in ("pending", "failed")):
        return False
    from .assignments import finalize_saved_sro_card_and_assignment
    try:
        with db.begin_nested():
            finalize_saved_sro_card_and_assignment(db, task, user_id)
    except Exception:
        task.local_finalization_state = "failed"
        task.error = "Карточка СРО сохранена; внутренние карточка и назначение требуют сверки. Повтор Save запрещён."
        db.add(TaskLocalFinalizationAttempt(task_id=task.id, actor_id=user_id, outcome="failed"))
        db.flush()
        return False
    task.local_finalization_state = "succeeded"
    task.error = ""
    db.add(TaskLocalFinalizationAttempt(task_id=task.id, actor_id=user_id, outcome="succeeded"))
    db.flush()
    return True


SRO_PREVIEW_FAILURE_MESSAGES = {
    "person_card_exists": "Карточка уже существует в СРОАрбитре: найдено точное совпадение ИНН. Новая карточка не создавалась.",
    "person_card_possible_duplicate": "В СРОАрбитре найдены возможные совпадения. Проверьте их перед созданием новой карточки.",
    "firm_card_exists": "Карточка организации уже существует в СРОАрбитре. Новая карточка не создавалась.",
    "firm_card_possible_duplicate": "В СРОАрбитре найдены возможные совпадения организации. Проверьте их перед созданием новой карточки.",
    "firm_add_unqualified": "Открытие нового бланка организации в СРО не подтверждено. Подготовка остановлена до сверки реестра.",
    "firm_preview_drift": "Данные организации или выбранный тип формы изменились после снимка. Сохранение остановлено; требуется новый проверенный снимок.",
    "person_lookup_invalid": "Не удалось проверить существующую карточку в реестре СРО. Новая форма не открывалась.",
    "new_person_url_invalid": "СРО открыл неожиданную страницу вместо новой карточки.",
    "new_person_identity_unbound": "Не удалось связать открытую форму СРО с добавлением новой карточки.",
    "new_person_not_blank": "Новая форма СРО содержит данные; заполнение остановлено до внесения изменений.",
    "person_readback_mismatch": "СРО не принял одно из заполненных полей. Проверьте результат подготовки формы.",
    "person_close_unverified": "Не удалось подтвердить закрытие бланка СРО без сохранения. Проверьте состояние вручную; повтор подготовки заблокирован.",
    "person_owned_drift": "Техническая карточка СРО изменилась после подготовки. Сохранение заблокировано до сверки точного GUID и полей.",
    "external_session_expired": "СРОАрбитр завершил сессию. Войдите в приложение снова, чтобы подключить СРО.",
}


def fail_sro_preview(db: Session, task: Task, user_id: int, *, failure_code: str | None = None) -> bool:
    """Record uncertainty without retrying or claiming the remote form was closed."""
    lock_task_for_update(db, task)
    if task.status not in (TaskStatus.writing, TaskStatus.awaiting_preview):
        return False
    if task.browser_decision in ("saved", "save_unknown", "closed", "preview_failed"):
        return False
    if task.browser_decision == "save_started":
        task.browser_decision = "save_unknown"
        task.dry_run = False
        task.error = "Результат сохранения СРО неизвестен. Повтор запрещён; требуется сверка реестра."
        kind = "preview_save_unknown"
    else:
        task.browser_decision = "preview_failed"
        task.error = SRO_PREVIEW_FAILURE_MESSAGES.get(
            failure_code, "Подготовка или закрытие формы СРО не завершены; требуется проверка."
        )
        kind = "preview_failed"
    log(db, task, kind, user_id=user_id)
    db.flush()
    return True


def expire_preview(db: Session, task: Task) -> bool:
    """Expire only an unconsumed choice; never erase an external-write marker."""
    lock_task_for_update(db, task)
    if getattr(task, "preview_mode", "live_session") == "durable_closed":
        return False
    if task.status != TaskStatus.awaiting_preview or task.browser_decision not in ("", "save_requested"):
        return False
    task.error = "Время ожидания истекло; запрошено закрытие формы без записи"
    task.browser_decision = "expired"
    log(db, task, "preview_timeout")
    db.flush()
    return True


def status_labels(db: Session, task_rows: list[Task]) -> dict[int, str]:
    """Return clear labels with one event query for a task list."""
    labels: dict[int, str] = {}
    for task in task_rows:
        if task.status == TaskStatus.writing and task.dry_run:
            labels[task.id] = (
                "ошибка формы СРО; требуется проверка"
                if getattr(task, "browser_decision", "") == "preview_failed"
                else "подготовка формы СРО без записи"
            )
        elif task.status == TaskStatus.awaiting_preview:
            if task.browser_decision == "close":
                labels[task.id] = "закрытие формы без записи"
            elif task.browser_decision == "closed":
                labels[task.id] = "форма закрыта без записи"
            elif task.browser_decision == "save_requested":
                labels[task.id] = "сохранение запрошено"
            elif task.browser_decision == "save_started":
                labels[task.id] = "карточка сохраняется"
            elif task.browser_decision == "saved":
                labels[task.id] = "карточка СРО сохранена и проверена"
            elif task.browser_decision == "save_unknown":
                labels[task.id] = "результат сохранения неизвестен; требуется сверка"
            elif task.browser_decision == "preview_failed":
                labels[task.id] = "ошибка формы СРО; требуется проверка"
            elif task.browser_decision == "expired":
                labels[task.id] = "время ожидания решения истекло"
            else:
                labels[task.id] = "форма открыта для проверки"
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

    for task in task_rows:
        if task.id in labels:
            continue
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
