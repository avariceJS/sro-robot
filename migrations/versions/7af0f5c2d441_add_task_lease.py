"""add durable task lease for local worker claims

Revision ID: 7af0f5c2d441
Revises: bc518c7514d3
Create Date: 2026-10-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "7af0f5c2d441"
down_revision = "bc518c7514d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_lease",
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("owner_token", sa.String(length=36), nullable=False),
        sa.Column("job_kind", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index("ix_task_lease_lease_until", "task_lease", ["lease_until"])


def downgrade() -> None:
    raise RuntimeError(
        "Task lease history cannot be dropped automatically. Stop workers and perform an "
        "explicit forward recovery after reconciling all in-flight attempts."
    )
