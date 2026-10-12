"""constrain intake source metadata digest format

Revision ID: f61c2a9d04e8
Revises: b7e1c40d62a9
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op


revision = "f61c2a9d04e8"
down_revision = "b7e1c40d62a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_intake_operation_source_metadata_sha256",
        "intake_operation",
        "source_metadata_sha256 IS NULL OR "
        "source_metadata_sha256 ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Intake replay metadata validation must not be removed automatically; "
        "use a reviewed forward migration after reconciling accepted operations."
    )
