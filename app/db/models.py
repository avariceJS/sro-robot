"""Database tables for the workflow platform.

The core stores scenario-defined fields as JSONB. New workflows may still need
schema migrations for their identity, reservation, provenance or lifecycle needs.
SRO credentials and browser passwords must never be stored here.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class TaskStatus(str, enum.Enum):
    new = "new"
    recognizing = "recognizing"
    needs_review = "needs_review"   # распознано, ждёт живого человека
    approved = "approved"           # оператор подтвердил поля
    writing = "writing"
    awaiting_preview = "awaiting_preview"  # форма заполнена, робот ждёт решения
    done = "done"
    failed = "failed"
    duplicate = "duplicate"         # в реестре уже есть похожая запись
    rejected = "rejected"


class IntakeSource(str, enum.Enum):
    upload = "upload"
    batch = "batch"
    mailbox = "mailbox"
    folder = "folder"
    kad = "kad"  # Browser-helper PDF intake and historical KAD source records.


MAX_INTAKE_FILENAME_CHARACTERS = 255
MAX_USER_FIELD_CHARACTERS = 255


class User(Base):
    """Сотрудник.

    Логин — в том же виде, что и в СРО: «Иванова М.И.». Почты у сотрудников
    может не быть вовсе, поэтому проверки формата почты здесь нет.
    """

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(MAX_USER_FIELD_CHARACTERS))
    full_name: Mapped[str] = mapped_column(String(MAX_USER_FIELD_CHARACTERS), default="")
    password_hash: Mapped[str] = mapped_column(String(MAX_USER_FIELD_CHARACTERS))
    # Логин СРО нужен, чтобы понимать, чьей сессией писать. Пароль — никогда.
    sro_login: Mapped[str] = mapped_column(String(MAX_USER_FIELD_CHARACTERS), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Регистр и лишние пробелы в логине не должны плодить двойников.
    __table_args__ = (Index("ix_user_login_lower", func.lower(login), unique=True),)


class LoginSession(Base):
    """One revocable application login session; bearer tokens are never stored."""

    __tablename__ = "login_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    token_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()

    __table_args__ = (
        UniqueConstraint("token_sha256", name="uq_login_session_token_sha256"),
        Index("ix_login_session_user_expires", "user_id", "expires_at"),
    )


class Document(Base):
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(MAX_INTAKE_FILENAME_CHARACTERS))
    stored_path: Mapped[str] = mapped_column(String(1024))
    content_hash: Mapped[str] = mapped_column(String(64), index=True, unique=True)
    source: Mapped[IntakeSource] = mapped_column(Enum(IntakeSource, name="intake_source"))
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    uploaded_by: Mapped[User | None] = relationship()
    tasks: Mapped[list["Task"]] = relationship(back_populates="document")


class IntakeOperation(Base):
    """Durable idempotency boundary for one browser-submitted intake request."""

    __tablename__ = "intake_operation"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    source_metadata_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship()
    tasks: Mapped[list["Task"]] = relationship(back_populates="intake_operation")

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_intake_operation_user_key"),
        CheckConstraint(
            "source_metadata_sha256 IS NULL OR "
            "source_metadata_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_intake_operation_source_metadata_sha256",
        ),
    )


class IntakeItem(Base):
    """One source occurrence within an accepted operation, even for shared bytes."""

    __tablename__ = "intake_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[int] = mapped_column(ForeignKey("intake_operation.id"))
    item_index: Mapped[int] = mapped_column(Integer)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    filename: Mapped[str] = mapped_column(String(MAX_INTAKE_FILENAME_CHARACTERS))
    source: Mapped[IntakeSource] = mapped_column(Enum(IntakeSource, name="intake_source"))
    received_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    operation: Mapped[IntakeOperation] = relationship()
    document: Mapped[Document] = relationship()
    received_by: Mapped[User | None] = relationship()
    task: Mapped["Task | None"] = relationship(
        back_populates="intake_item", uselist=False, viewonly=True
    )

    __table_args__ = (
        CheckConstraint("item_index >= 0", name="ck_intake_item_nonnegative_index"),
        UniqueConstraint("operation_id", "item_index", name="uq_intake_item_operation_index"),
        UniqueConstraint(
            "id", "operation_id", "document_id", name="uq_intake_item_context"
        ),
    )


class Task(Base):
    """Задание: «сделать из этого документа вот такую запись в СРО»."""

    __tablename__ = "task"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    scenario: Mapped[str] = mapped_column(String(64))
    profile: Mapped[str] = mapped_column(String(64))
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), default=TaskStatus.new, index=True
    )

    # Распознанное: {attr: {value, confidence, source, warning}}.
    recognized: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Итог после правок оператора: {attr: "значение"}.
    values: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Дублируется из полей ради журнала дел и быстрого поиска.
    case_number: Mapped[str] = mapped_column(String(64), default="", index=True)

    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    intake_operation_id: Mapped[int | None] = mapped_column(ForeignKey("intake_operation.id"))
    intake_item_id: Mapped[int | None] = mapped_column(Integer, unique=True)

    record_url: Mapped[str] = mapped_column(String(1024), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    dry_run: Mapped[bool] = mapped_column(default=True)

    # Снимки заполненной формы и ответ оператора на них: "close" или "save".
    screenshots: Mapped[list] = mapped_column(JSONB, default=list)
    browser_decision: Mapped[str] = mapped_column(String(16), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="tasks")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])
    intake_operation: Mapped[IntakeOperation | None] = relationship(back_populates="tasks")
    intake_item: Mapped[IntakeItem | None] = relationship(
        back_populates="task", viewonly=True
    )

    __table_args__ = (
        Index("ix_task_status_created", "status", "created_at"),
        Index("ix_task_intake_operation_id", "intake_operation_id"),
        ForeignKeyConstraint(
            ["intake_item_id", "intake_operation_id", "document_id"],
            ["intake_item.id", "intake_item.operation_id", "intake_item.document_id"],
            name="fk_task_intake_item_context",
        ),
    )


class TaskStatusHistory(Base):
    """Immutable database-recorded history of task lifecycle changes."""

    __tablename__ = "task_status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("task.id"))
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    __table_args__ = (
        CheckConstraint(
            "from_status <> to_status", name="ck_task_status_history_changed"
        ),
        Index("ix_task_status_history_task_id_id", "task_id", "id"),
    )


class TaskLease(Base):
    """Fenced ownership for retryable local worker operations."""

    __tablename__ = "task_lease"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), primary_key=True
    )
    owner_token: Mapped[str] = mapped_column(String(36))
    job_kind: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class JobOutbox(Base):
    """Pending at-least-once delivery intents for safe internal jobs only."""

    __tablename__ = "job_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("task.id", ondelete="CASCADE"))
    job_kind: Mapped[str] = mapped_column(String(32))
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error_type: Mapped[str | None] = mapped_column(String(64))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Historical `kad` rows may still exist. New intents of that kind are rejected.
    __table_args__ = (
        CheckConstraint(
            "job_kind IN ('ocr', 'kad', 'local_validation')",
            name="ck_job_outbox_safe_job_kind",
        ),
        CheckConstraint(
            "(job_kind = 'local_validation' AND actor_id IS NOT NULL) OR "
            "(job_kind IN ('ocr', 'kad') AND actor_id IS NULL)",
            name="ck_job_outbox_actor_scope",
        ),
        Index("ix_job_outbox_available_id", "available_at", "id"),
    )


class Event(Base):
    """Аудит: кто что сделал. Сканов и распознанного текста здесь нет."""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    kind: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User | None] = relationship()


TASK_STATUS_RU: dict[TaskStatus, str] = {
    TaskStatus.new: "принят",
    TaskStatus.recognizing: "распознаётся",
    TaskStatus.needs_review: "на проверке",
    TaskStatus.approved: "подтверждён",
    TaskStatus.writing: "пишется в СРО",
    TaskStatus.awaiting_preview: "форма заполнена, ждёт вашего решения",
    TaskStatus.done: "готово",
    TaskStatus.failed: "ошибка",
    TaskStatus.duplicate: "найден дубль",
    TaskStatus.rejected: "отклонён",
}

__all__ = [
    "Document",
    "Event",
    "IntakeSource",
    "JobOutbox",
    "TASK_STATUS_RU",
    "Task",
    "TaskLease",
    "TaskStatus",
    "User",
]
