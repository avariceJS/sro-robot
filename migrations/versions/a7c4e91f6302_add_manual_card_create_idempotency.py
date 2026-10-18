"""Persist replay keys for manual debtor-card creation requests.

Revision ID: a7c4e91f6302
Revises: 4c9e2b7a1d63
"""

from alembic import op
import sqlalchemy as sa


revision = "a7c4e91f6302"
down_revision = "4c9e2b7a1d63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "debtor_card_create_request",
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("debtor_card_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_debtor_card_create_request_user_id_user"),
        sa.ForeignKeyConstraint(["debtor_card_id"], ["debtor_card_draft.id"], name="fk_debtor_card_create_request_debtor_card_id_debtor_card_draft"),
        sa.PrimaryKeyConstraint("request_hash", name="pk_debtor_card_create_request"),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_debtor_card_create_request_hash_length"),
        sa.CheckConstraint("length(payload_hash) = 64", name="ck_debtor_card_create_request_payload_hash_length"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Manual debtor-card create idempotency is forward-only; use a reviewed forward migration."
    )
