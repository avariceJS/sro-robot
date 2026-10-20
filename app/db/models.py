"""Database tables for the workflow platform.

The core stores scenario-defined fields as JSONB. New workflows may still need
schema migrations for their identity, reservation, provenance or lifecycle needs.
SRO credentials and browser passwords must never be stored here.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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
    """Local staff identity linked to the exact verified SRO login."""

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(MAX_USER_FIELD_CHARACTERS))
    full_name: Mapped[str] = mapped_column(String(MAX_USER_FIELD_CHARACTERS), default="")
    password_hash: Mapped[str] = mapped_column(String(MAX_USER_FIELD_CHARACTERS))
    # Exact SRO login identifies the employee; external passwords are never stored.
    sro_login: Mapped[str] = mapped_column(String(MAX_USER_FIELD_CHARACTERS), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Регистр и лишние пробелы в логине не должны плодить двойников.
    __table_args__ = (Index("ix_user_login_lower", func.lower(login), unique=True),)


class LoginSession(Base):
    """One revocable app session; bearer token and SRO state are never stored in clear."""

    __tablename__ = "login_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    token_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_state_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    external_state_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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


class KadSourceRun(Base):
    """One explicit OCR run for a specific selected KAD PDF occurrence."""

    __tablename__ = "kad_source_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_identity_sha256: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer)
    operation_id: Mapped[int] = mapped_column(ForeignKey("intake_operation.id"), unique=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("task.id"), unique=True)
    parent_run_id: Mapped[int | None] = mapped_column(Integer)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    __table_args__ = (
        CheckConstraint(
            "source_identity_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_kad_source_run_identity_sha256",
        ),
        CheckConstraint("generation >= 1", name="ck_kad_source_run_generation_positive"),
        CheckConstraint(
            "(generation = 1 AND parent_run_id IS NULL) OR "
            "(generation > 1 AND parent_run_id IS NOT NULL)",
            name="ck_kad_source_run_parent_generation",
        ),
        UniqueConstraint(
            "source_identity_sha256", "generation", name="uq_kad_source_run_generation"
        ),
        UniqueConstraint(
            "id", "source_identity_sha256", name="uq_kad_source_run_parent_identity"
        ),
        ForeignKeyConstraint(
            ["parent_run_id", "source_identity_sha256"],
            ["kad_source_run.id", "kad_source_run.source_identity_sha256"],
            name="fk_kad_source_run_parent_identity",
        ),
    )


class KadImportRequestReceipt(Base):
    """Cross-employee request receipt for a canonical KAD source run."""

    __tablename__ = "kad_import_request_receipt"

    request_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    operation_id: Mapped[int | None] = mapped_column(ForeignKey("intake_operation.id"))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    source_metadata_sha256: Mapped[str] = mapped_column(String(64))
    source_metadata: Mapped[dict] = mapped_column(JSONB)
    reprocess: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    __table_args__ = (
        CheckConstraint("length(request_hash) = 64", name="ck_kad_import_receipt_request_hash"),
        CheckConstraint("length(manifest_sha256) = 64", name="ck_kad_import_receipt_manifest_hash"),
        CheckConstraint(
            "length(source_metadata_sha256) = 64",
            name="ck_kad_import_receipt_source_metadata_hash",
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


class DebtorCardDraft(Base):
    """Internal, reviewed debtor identity used by independent office work."""

    __tablename__ = "debtor_card_draft"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_kind: Mapped[str] = mapped_column(String(32), default="person")
    display_name: Mapped[str] = mapped_column(String(512))
    inn: Mapped[str | None] = mapped_column(String(12), nullable=True)
    ogrn: Mapped[str | None] = mapped_column(String(13), nullable=True)
    ogrnip: Mapped[str | None] = mapped_column(String(15), nullable=True)
    preparation_state: Mapped[str] = mapped_column(String(16), default="reviewed", server_default="reviewed")
    values: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id"), nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    updated_by_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp()
    )

    source_task: Mapped[Task | None] = relationship()
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])
    updated_by: Mapped[User] = relationship(foreign_keys=[updated_by_id])

    __table_args__ = (
        CheckConstraint(
            "entity_kind IN ('person', 'legal_entity', 'sole_proprietor')",
            name="ck_debtor_card_draft_entity_kind",
        ),
        CheckConstraint(
            "inn IS NULL OR "
            "(entity_kind = 'person' AND length(inn) = 12) OR "
            "(entity_kind = 'legal_entity' AND length(inn) = 10) OR "
            "(entity_kind = 'sole_proprietor' AND length(inn) = 12)",
            name="ck_debtor_card_draft_inn_length",
        ),
        CheckConstraint(
            "(ogrn IS NULL OR entity_kind = 'legal_entity') AND "
            "(ogrnip IS NULL OR entity_kind = 'sole_proprietor')",
            name="ck_debtor_card_draft_registry_id_kind",
        ),
        CheckConstraint("version >= 1", name="ck_debtor_card_draft_version"),
        CheckConstraint("preparation_state IN ('draft', 'prepared', 'reviewed')", name="ck_debtor_card_draft_preparation_state"),
        UniqueConstraint("source_task_id", name="uq_debtor_card_draft_source_task"),
        Index(
            "uq_debtor_card_draft_kind_inn",
            "entity_kind",
            "inn",
            unique=True,
            postgresql_where=text("inn IS NOT NULL"),
        ),
        Index("uq_debtor_card_draft_legal_entity_ogrn", "ogrn", unique=True,
              postgresql_where=text("entity_kind = 'legal_entity' AND ogrn IS NOT NULL")),
        Index("uq_debtor_card_draft_sole_proprietor_ogrnip", "ogrnip", unique=True,
              postgresql_where=text("entity_kind = 'sole_proprietor' AND ogrnip IS NOT NULL")),
        Index("ix_debtor_card_draft_display_name", "display_name"),
    )


class DebtorCardDraftRevision(Base):
    """Attributed internal-card snapshots; automatic preparation stays explicit."""

    __tablename__ = "debtor_card_draft_revision"
    id: Mapped[int] = mapped_column(primary_key=True)
    debtor_card_id: Mapped[int] = mapped_column(ForeignKey("debtor_card_draft.id"))
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSONB)
    changed_by_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    __table_args__ = (
        UniqueConstraint("debtor_card_id", "revision", name="uq_debtor_card_draft_revision_number"),
        CheckConstraint("revision >= 1", name="ck_debtor_card_draft_revision_positive"),
    )


class AssignmentDraft(Base):
    """Resumable SRO Robot assignment draft; it is never an SRO record."""

    __tablename__ = "assignment_draft"

    id: Mapped[int] = mapped_column(primary_key=True)
    debtor_card_id: Mapped[int] = mapped_column(ForeignKey("debtor_card_draft.id"))
    # One source task may seed at most one assignment. Independent assignments
    # from different acts remain valid even when debtor/case numbers match.
    source_task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id"), nullable=True)
    case_number: Mapped[str] = mapped_column(String(64), default="", server_default="")
    status: Mapped[str] = mapped_column(String(32), default="draft", server_default="draft")
    values: Mapped[dict] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    updated_by_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp()
    )

    debtor_card: Mapped[DebtorCardDraft] = relationship()
    source_task: Mapped[Task | None] = relationship(foreign_keys=[source_task_id])
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])
    updated_by: Mapped[User] = relationship(foreign_keys=[updated_by_id])
    revisions: Mapped[list["AssignmentDraftRevision"]] = relationship(
        back_populates="assignment", order_by="AssignmentDraftRevision.revision"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'awaiting_information', 'ready_for_review')",
            name="ck_assignment_draft_status",
        ),
        CheckConstraint("version >= 1", name="ck_assignment_draft_version"),
        UniqueConstraint("source_task_id", name="uq_assignment_draft_source_task"),
        Index("ix_assignment_draft_status_updated", "status", "updated_at"),
        Index("ix_assignment_draft_debtor_card_id_id", "debtor_card_id", "id"),
    )


class AssignmentCreateRequest(Base):
    """Durable request-key receipt for replay-safe employee assignment creation."""

    __tablename__ = "assignment_create_request"

    request_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    debtor_card_id: Mapped[int] = mapped_column(ForeignKey("debtor_card_draft.id"))
    assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignment_draft.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class DebtorCardCreateRequest(Base):
    """Durable request-key receipt for replay-safe manual card creation."""

    __tablename__ = "debtor_card_create_request"

    request_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    debtor_card_id: Mapped[int | None] = mapped_column(
        ForeignKey("debtor_card_draft.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())

    __table_args__ = (
        CheckConstraint("length(request_hash) = 64", name="ck_debtor_card_create_request_hash_length"),
        CheckConstraint("length(payload_hash) = 64", name="ck_debtor_card_create_request_payload_hash_length"),
    )


class AssignmentDraftRevision(Base):
    """Append-only snapshots of internal assignment edits and status changes."""

    __tablename__ = "assignment_draft_revision"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_draft_id: Mapped[int] = mapped_column(ForeignKey("assignment_draft.id"))
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    changed_by_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    assignment: Mapped[AssignmentDraft] = relationship(back_populates="revisions")
    changed_by: Mapped[User] = relationship()

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_assignment_draft_revision_positive"),
        CheckConstraint(
            "status IN ('draft', 'awaiting_information', 'ready_for_review')",
            name="ck_assignment_draft_revision_status",
        ),
        UniqueConstraint(
            "assignment_draft_id", "revision", name="uq_assignment_draft_revision_number"
        ),
        Index("ix_assignment_draft_revision_parent", "assignment_draft_id", "revision"),
    )


class CasePackage(Base):
    """An employee-curated internal workspace; labels never establish identity."""

    __tablename__ = "case_package"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    case_number: Mapped[str] = mapped_column(String(64), default="", server_default="")
    created_by_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    request_key_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    created_by: Mapped[User] = relationship()
    membership_events: Mapped[list["CasePackageTaskEvent"]] = relationship(
        order_by="CasePackageTaskEvent.id"
    )

    __table_args__ = (
        CheckConstraint("length(title) BETWEEN 1 AND 255", name="ck_case_package_title_length"),
        CheckConstraint(
            "case_number = '' OR length(case_number) <= 64",
            name="ck_case_package_case_number_length",
        ),
        CheckConstraint("request_key_sha256 ~ '^[0-9a-f]{64}$'", name="ck_case_package_request_key_sha256"),
        CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_case_package_payload_sha256"),
        Index("ix_case_package_created", "created_at", "id"),
    )


class CasePackageTaskEvent(Base):
    """Append-only, actor-attributed addition/removal history for package tasks."""

    __tablename__ = "case_package_task_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_package_id: Mapped[int] = mapped_column(ForeignKey("case_package.id"))
    task_id: Mapped[int] = mapped_column(ForeignKey("task.id"))
    action: Mapped[str] = mapped_column(String(8))
    actor_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    task: Mapped[Task] = relationship()
    actor: Mapped[User] = relationship()

    __table_args__ = (
        CheckConstraint("action IN ('added', 'removed')", name="ck_case_package_task_event_action"),
        Index("ix_case_package_task_event_membership", "case_package_id", "task_id", "id"),
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
    "AssignmentDraft",
    "AssignmentDraftRevision",
    "DebtorCardDraft",
    "DebtorCardDraftRevision",
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
