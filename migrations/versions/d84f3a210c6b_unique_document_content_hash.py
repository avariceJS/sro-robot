"""enforce unique stored document content hashes

Revision ID: d84f3a210c6b
Revises: 7af0f5c2d441
Create Date: 2026-10-11
"""

from __future__ import annotations

from alembic import op

revision = "d84f3a210c6b"
down_revision = "7af0f5c2d441"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A duplicate legacy hash aborts the transaction without removing data.
    op.drop_index("ix_document_content_hash", table_name="document")
    op.create_index(
        "ix_document_content_hash",
        "document",
        ["content_hash"],
        unique=True,
    )


def downgrade() -> None:
    raise RuntimeError(
        "Document content identity must remain unique; restore the previous schema only "
        "through a reviewed forward migration."
    )
