"""bind new same-key intake replays to exact ordered source filenames

Revision ID: b7e1c40d62a9
Revises: 9ad3c7e10f62
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b7e1c40d62a9"
down_revision = "9ad3c7e10f62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL preserves retry compatibility for operations created before this
    # metadata contract existed; new admissions persist a versioned digest.
    op.add_column(
        "intake_operation",
        sa.Column("source_metadata_sha256", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Intake replay metadata must not be removed automatically; "
        "use a reviewed forward migration after reconciling accepted operations."
    )
