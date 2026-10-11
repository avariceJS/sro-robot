"""add safe internal job dispatch outbox

Revision ID: f28c7a913bd4
Revises: c73b8e4a19d2
Create Date: 2026-10-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f28c7a913bd4"
down_revision = "c73b8e4a19d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("job_kind", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_type", sa.String(length=64), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "job_kind IN ('ocr', 'kad', 'local_validation')",
            name="ck_job_outbox_safe_job_kind",
        ),
        sa.CheckConstraint(
            "(job_kind = 'local_validation' AND actor_id IS NOT NULL) OR "
            "(job_kind IN ('ocr', 'kad') AND actor_id IS NULL)",
            name="ck_job_outbox_actor_scope",
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_job_outbox_dedupe_key"),
    )
    op.create_index("ix_job_outbox_available_id", "job_outbox", ["available_at", "id"])


def downgrade() -> None:
    raise RuntimeError(
        "Safe job dispatch history must not be removed by downgrade; "
        "use a reviewed forward migration after resolving pending jobs."
    )
