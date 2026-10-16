"""Persist replay keys for independent assignment creation requests.

Revision ID: 6d8f31a4c920
Revises: 21c4a6f80d32
"""

from alembic import op
import sqlalchemy as sa


revision = "6d8f31a4c920"
down_revision = "21c4a6f80d32"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assignment_create_request",
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("debtor_card_id", sa.Integer(), nullable=False),
        sa.Column("assignment_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_assignment_create_request_user_id_user"),
        sa.ForeignKeyConstraint(["debtor_card_id"], ["debtor_card_draft.id"], name="fk_assignment_create_request_debtor_card_id_debtor_card_draft"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignment_draft.id"], name="fk_assignment_create_request_assignment_id_assignment_draft"),
        sa.PrimaryKeyConstraint("request_hash", name="pk_assignment_create_request"),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_assignment_create_request_hash_length"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM assignment_create_request)")).scalar_one():
        raise RuntimeError(
            "Assignment idempotency receipts may contain completed operations; "
            "use a reviewed forward migration instead of deleting retry history."
        )
    op.drop_table("assignment_create_request")
