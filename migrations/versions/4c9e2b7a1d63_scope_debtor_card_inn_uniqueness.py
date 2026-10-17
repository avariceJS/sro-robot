"""scope debtor-card INN uniqueness by entity kind

Revision ID: 4c9e2b7a1d63
Revises: 8b2d71c4e906
Create Date: 2026-10-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "4c9e2b7a1d63"
down_revision = "8b2d71c4e906"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SROArbitr distinguishes a sole proprietor from the linked person record.
    # Preserve duplicate protection within each card kind while allowing the
    # same 12-digit personal INN on the person's card and the IP card.
    op.drop_index("uq_debtor_card_draft_inn", table_name="debtor_card_draft")
    op.create_index(
        "uq_debtor_card_draft_kind_inn",
        "debtor_card_draft",
        ["entity_kind", "inn"],
        unique=True,
        postgresql_where=sa.text("inn IS NOT NULL"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Entity-specific debtor-card identity is staff data; use a reviewed forward migration."
    )
