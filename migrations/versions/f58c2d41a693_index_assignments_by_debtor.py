"""Index assignment history by debtor card.

Revision ID: f58c2d41a693
Revises: a7c4e91f6302
"""

from alembic import op


revision = "f58c2d41a693"
down_revision = "a7c4e91f6302"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_assignment_draft_debtor_card_id_id",
        "assignment_draft",
        ["debtor_card_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_assignment_draft_debtor_card_id_id", table_name="assignment_draft")
