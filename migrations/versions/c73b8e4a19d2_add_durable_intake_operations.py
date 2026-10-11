"""add durable intake operation identity

Revision ID: c73b8e4a19d2
Revises: a41c72d93e10
Create Date: 2026-10-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c73b8e4a19d2"
down_revision = "a41c72d93e10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intake_operation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_intake_operation_user_key"),
    )
    op.add_column("task", sa.Column("intake_operation_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_task_intake_operation_id_intake_operation",
        "task",
        "intake_operation",
        ["intake_operation_id"],
        ["id"],
    )
    op.create_index("ix_task_intake_operation_id", "task", ["intake_operation_id"])


def downgrade() -> None:
    raise RuntimeError(
        "Durable intake idempotency must not be removed by downgrade; "
        "use a reviewed forward migration after preserving operation history."
    )
