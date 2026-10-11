"""add revocable per-login sessions

Revision ID: 8cd3a71f204e
Revises: 5e2d91c7a640
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "8cd3a71f204e"
down_revision = "5e2d91c7a640"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_sha256", name="uq_login_session_token_sha256"),
    )
    op.create_index(
        "ix_login_session_user_expires",
        "login_session",
        ["user_id", "expires_at"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "Login session revocation records must not be removed automatically; "
        "use a reviewed forward migration after preserving session state."
    )
