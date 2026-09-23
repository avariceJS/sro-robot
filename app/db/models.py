"""Таблицы платформы.

Ядро не знает, какие именно поля у карточки: распознанное хранится как JSONB,
а состав полей объявляет сценарий. Поэтому карточка организации и назначение
лягут в те же таблицы без миграции.

Паролей СРО здесь нет и не будет: пароль нужен только для создания браузерной
сессии (решение в memory-bank/70-decisions.md).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
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


class User(Base):
    """Сотрудник.

    Логин — в том же виде, что и в СРО: «Иванова М.И.». Почты у сотрудников
    может не быть вовсе, поэтому проверки формата почты здесь нет.
    """

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    # Логин СРО нужен, чтобы понимать, чьей сессией писать. Пароль — никогда.
    sro_login: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Регистр и лишние пробелы в логине не должны плодить двойников.
    __table_args__ = (Index("ix_user_login_lower", func.lower(login), unique=True),)


class Document(Base):
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(1024))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[IntakeSource] = mapped_column(Enum(IntakeSource, name="intake_source"))
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    uploaded_by: Mapped[User | None] = relationship()
    tasks: Mapped[list["Task"]] = relationship(back_populates="document")


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

    __table_args__ = (Index("ix_task_status_created", "status", "created_at"),)


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
    "TASK_STATUS_RU",
    "Task",
    "TaskStatus",
    "User",
]
