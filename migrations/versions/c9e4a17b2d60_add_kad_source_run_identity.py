"""Track shared KAD source operations and explicit OCR reprocessing runs.

Revision ID: c9e4a17b2d60
Revises: b82f04c193ad
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c9e4a17b2d60"
down_revision = "b82f04c193ad"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kad_source_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_identity_sha256", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("parent_run_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["operation_id"], ["intake_operation.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"]),
        sa.ForeignKeyConstraint(
            ["parent_run_id", "source_identity_sha256"],
            ["kad_source_run.id", "kad_source_run.source_identity_sha256"],
            name="fk_kad_source_run_parent_identity",
        ),
        sa.CheckConstraint(
            "source_identity_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_kad_source_run_identity_sha256",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_kad_source_run_generation_positive"),
        sa.CheckConstraint(
            "(generation = 1 AND parent_run_id IS NULL) OR "
            "(generation > 1 AND parent_run_id IS NOT NULL)",
            name="ck_kad_source_run_parent_generation",
        ),
        sa.UniqueConstraint("operation_id", name="uq_kad_source_run_operation"),
        sa.UniqueConstraint("task_id", name="uq_kad_source_run_task"),
        sa.UniqueConstraint("source_identity_sha256", "generation", name="uq_kad_source_run_generation"),
        sa.UniqueConstraint("id", "source_identity_sha256", name="uq_kad_source_run_parent_identity"),
    )
    op.create_table(
        "kad_import_request_receipt",
        sa.Column("request_hash", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Integer(), nullable=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_metadata_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reprocess", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["operation_id"], ["intake_operation.id"]),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_kad_import_receipt_request_hash"),
        sa.CheckConstraint("length(manifest_sha256) = 64", name="ck_kad_import_receipt_manifest_hash"),
        sa.CheckConstraint(
            "length(source_metadata_sha256) = 64",
            name="ck_kad_import_receipt_source_metadata_hash",
        ),
    )


def downgrade() -> None:
    raise RuntimeError(
        "KAD source identity and reprocessing history is durable; "
        "use a reviewed forward migration."
    )
