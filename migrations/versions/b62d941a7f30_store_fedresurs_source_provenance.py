"""store public-source selection provenance for Fedresurs acquisitions

Revision ID: b62d941a7f30
Revises: a48ef390b2d1
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b62d941a7f30"
down_revision = "a48ef390b2d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column("source_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Fedresurs source provenance must not be removed automatically"
    )
