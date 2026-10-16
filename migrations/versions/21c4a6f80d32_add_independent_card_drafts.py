"""Allow independently created and resumable debtor-card drafts.

Revision ID: 21c4a6f80d32
Revises: a6c2d8e41b90
"""

from alembic import op


revision = "21c4a6f80d32"
down_revision = "a6c2d8e41b90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_debtor_card_draft_preparation_state", "debtor_card_draft", type_="check")
    op.create_check_constraint(
        "ck_debtor_card_draft_preparation_state",
        "debtor_card_draft",
        "preparation_state IN ('draft', 'prepared', 'reviewed')",
    )
    op.alter_column("debtor_card_draft", "source_task_id", nullable=True)


def downgrade() -> None:
    raise RuntimeError("Independent card drafts are durable work; downgrade would lose lifecycle state")
