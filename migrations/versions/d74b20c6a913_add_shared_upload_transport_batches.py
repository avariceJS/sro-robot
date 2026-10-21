"""add immutable shared upload transport manifests and chunk receipts

Revision ID: d74b20c6a913
Revises: c1e61f8a4d20
Create Date: 2026-10-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d74b20c6a913"
down_revision = "c1e61f8a4d20"
branch_labels = None
depends_on = None


_IMMUTABLE_FUNCTION = """
CREATE FUNCTION public.sro_reject_upload_transport_mutation()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $upload_transport_immutable$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'upload transport records are immutable';
END;
$upload_transport_immutable$;
"""

_VALIDATE_ITEM_FUNCTION = """
CREATE FUNCTION public.sro_validate_upload_transport_batch_item()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $upload_transport_item_validate$
DECLARE chunk_size integer; declared_files integer;
BEGIN
    SELECT files_per_chunk, file_count INTO chunk_size, declared_files
    FROM public.upload_transport_batch WHERE id = NEW.batch_id FOR KEY SHARE;
    IF NOT FOUND OR NEW.file_index >= declared_files
       OR NEW.chunk_index <> NEW.file_index / chunk_size THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'upload transport item does not match its immutable batch plan';
    END IF;
    RETURN NEW;
END;
$upload_transport_item_validate$;
"""

_REQUIRE_COMPLETE_FUNCTION = """
CREATE FUNCTION public.sro_require_upload_transport_manifest_complete()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $upload_transport_manifest_complete$
DECLARE batch_id_value text; declared_files integer; declared_bytes bigint; actual_files bigint; actual_bytes bigint;
BEGIN
    batch_id_value := coalesce(to_jsonb(NEW)->>'id', to_jsonb(NEW)->>'batch_id');
    SELECT file_count, total_bytes INTO declared_files, declared_bytes
    FROM public.upload_transport_batch WHERE id = batch_id_value;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT count(*), coalesce(sum(file_size), 0) INTO actual_files, actual_bytes
    FROM public.upload_transport_batch_item WHERE batch_id = batch_id_value;
    IF actual_files <> declared_files OR actual_bytes <> declared_bytes THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'upload transport manifest is incomplete';
    END IF;
    RETURN NULL;
END;
$upload_transport_manifest_complete$;
"""

_VALIDATE_RECEIPT_FUNCTION = """
CREATE FUNCTION public.sro_validate_upload_transport_chunk_receipt()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $upload_transport_receipt_validate$
DECLARE chunk_size integer; declared_files integer; planned_count integer; actual_count integer;
BEGIN
    SELECT files_per_chunk, file_count INTO chunk_size, declared_files
    FROM public.upload_transport_batch
    WHERE id = NEW.batch_id AND manifest_sha256 = NEW.batch_manifest_sha256
    FOR KEY SHARE;
    IF NOT FOUND OR NEW.chunk_index >= (declared_files + chunk_size - 1) / chunk_size THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'upload transport receipt has no planned chunk';
    END IF;
    planned_count := least(chunk_size, declared_files - NEW.chunk_index * chunk_size);
    SELECT count(*) INTO actual_count
    FROM public.upload_transport_batch_item item
    LEFT JOIN public.intake_item intake
      ON intake.operation_id = NEW.operation_id
     AND intake.item_index = item.file_index - NEW.chunk_index * chunk_size
    LEFT JOIN public.document document ON document.id = intake.document_id
    LEFT JOIN public.task task
      ON task.intake_operation_id = NEW.operation_id
     AND task.intake_item_id = intake.id
     AND task.document_id = intake.document_id
     AND task.assignee_id = NEW.accepted_by_id
    LEFT JOIN public.job_outbox outbox
      ON outbox.task_id = task.id
     AND outbox.job_kind = 'ocr'
    WHERE item.batch_id = NEW.batch_id
      AND item.chunk_index = NEW.chunk_index
      AND (intake.id IS NULL OR intake.filename IS DISTINCT FROM item.filename
        OR document.content_hash IS DISTINCT FROM item.content_sha256
        OR intake.source::text IS DISTINCT FROM CASE WHEN planned_count = 1 THEN 'upload' ELSE 'batch' END
        OR intake.received_by_id IS DISTINCT FROM NEW.accepted_by_id
        OR task.id IS NULL OR outbox.id IS NULL);
    IF actual_count <> 0
       OR (SELECT count(*) FROM public.intake_item WHERE operation_id = NEW.operation_id) <> planned_count
       OR (SELECT count(*) FROM public.task WHERE intake_operation_id = NEW.operation_id) <> planned_count
       OR (SELECT count(*) FROM public.job_outbox
           WHERE task_id IN (SELECT id FROM public.task WHERE intake_operation_id = NEW.operation_id)
             AND job_kind = 'ocr') <> planned_count THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'upload transport receipt does not match committed intake effects';
    END IF;
    RETURN NEW;
END;
$upload_transport_receipt_validate$;
"""


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_intake_operation_receipt_context", "intake_operation",
        ["id", "user_id", "manifest_sha256", "source_metadata_sha256"],
    )
    op.create_table(
        "upload_transport_batch",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("request_key_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("files_per_chunk", sa.Integer(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.CheckConstraint("id ~ '^[0-9a-f]{32}$'", name="ck_upload_transport_batch_id"),
        sa.CheckConstraint("request_key_sha256 ~ '^[0-9a-f]{64}$'", name="ck_upload_transport_batch_request_key_sha256"),
        sa.CheckConstraint("manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_upload_transport_batch_manifest_sha256"),
        sa.CheckConstraint("files_per_chunk BETWEEN 1 AND 100", name="ck_upload_transport_batch_chunk_size"),
        sa.CheckConstraint("file_count BETWEEN 1 AND 100", name="ck_upload_transport_batch_file_count"),
        sa.CheckConstraint("total_bytes > 0", name="ck_upload_transport_batch_total_bytes"),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("created_by_id", "request_key_sha256", name="uq_upload_transport_batch_creator_key"),
        sa.UniqueConstraint("id", "manifest_sha256", name="uq_upload_transport_batch_context"),
    )
    op.create_table(
        "upload_transport_batch_item",
        sa.Column("batch_id", sa.String(length=32), nullable=False),
        sa.Column("file_index", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.CheckConstraint("file_index BETWEEN 0 AND 99", name="ck_upload_transport_batch_item_index"),
        sa.CheckConstraint("chunk_index BETWEEN 0 AND 99", name="ck_upload_transport_batch_item_chunk_index"),
        sa.CheckConstraint("file_size > 0", name="ck_upload_transport_batch_item_file_size"),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_upload_transport_batch_item_content_sha256"),
        sa.CheckConstraint("length(filename) BETWEEN 1 AND 255", name="ck_upload_transport_batch_item_filename"),
        sa.ForeignKeyConstraint(["batch_id"], ["upload_transport_batch.id"]),
        sa.PrimaryKeyConstraint("batch_id", "file_index"),
        sa.UniqueConstraint("batch_id", "file_index", "chunk_index", name="uq_upload_transport_batch_item_context"),
    )
    op.create_index(
        "ix_upload_transport_batch_item_chunk", "upload_transport_batch_item",
        ["batch_id", "chunk_index", "file_index"], unique=False,
    )
    op.create_table(
        "upload_transport_chunk_receipt",
        sa.Column("batch_id", sa.String(length=32), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Integer(), nullable=False),
        sa.Column("accepted_by_id", sa.Integer(), nullable=False),
        sa.Column("batch_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("operation_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_metadata_sha256", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.CheckConstraint("chunk_index BETWEEN 0 AND 99", name="ck_upload_transport_chunk_index"),
        sa.CheckConstraint("source_metadata_sha256 ~ '^[0-9a-f]{64}$'", name="ck_upload_transport_chunk_source_metadata_sha256"),
        sa.ForeignKeyConstraint(["accepted_by_id"], ["user.id"]),
        sa.ForeignKeyConstraint(
            ["batch_id", "batch_manifest_sha256"],
            ["upload_transport_batch.id", "upload_transport_batch.manifest_sha256"],
            name="fk_upload_transport_chunk_batch_context",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "accepted_by_id", "operation_manifest_sha256", "source_metadata_sha256"],
            ["intake_operation.id", "intake_operation.user_id", "intake_operation.manifest_sha256", "intake_operation.source_metadata_sha256"],
            name="fk_upload_transport_chunk_operation_context",
        ),
        sa.PrimaryKeyConstraint("batch_id", "chunk_index"),
        sa.UniqueConstraint("operation_id", name="uq_upload_transport_chunk_operation"),
    )
    op.execute(_IMMUTABLE_FUNCTION)
    for table, base in (
        ("upload_transport_batch", "upload_transport_batch"),
        ("upload_transport_batch_item", "upload_transport_batch_item"),
        ("upload_transport_chunk_receipt", "upload_transport_chunk_receipt"),
    ):
        op.execute(
            f"CREATE TRIGGER {base}_immutable BEFORE UPDATE OR DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.sro_reject_upload_transport_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {base}_no_truncate BEFORE TRUNCATE ON public.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.sro_reject_upload_transport_mutation()"
        )
    op.execute(_VALIDATE_ITEM_FUNCTION)
    op.execute(
        "CREATE TRIGGER upload_transport_batch_item_validate BEFORE INSERT "
        "ON public.upload_transport_batch_item FOR EACH ROW "
        "EXECUTE FUNCTION public.sro_validate_upload_transport_batch_item()"
    )
    op.execute(_REQUIRE_COMPLETE_FUNCTION)
    for table, trigger in (
        ("upload_transport_batch", "upload_transport_batch_complete"),
        ("upload_transport_batch_item", "upload_transport_batch_item_complete"),
    ):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER {trigger} AFTER INSERT ON public.{table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION public.sro_require_upload_transport_manifest_complete()"
        )
    op.execute(_VALIDATE_RECEIPT_FUNCTION)
    op.execute(
        "CREATE TRIGGER upload_transport_chunk_receipt_validate BEFORE INSERT "
        "ON public.upload_transport_chunk_receipt FOR EACH ROW "
        "EXECUTE FUNCTION public.sro_validate_upload_transport_chunk_receipt()"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Shared upload transport receipts are durable intake evidence and must not be removed automatically."
    )
