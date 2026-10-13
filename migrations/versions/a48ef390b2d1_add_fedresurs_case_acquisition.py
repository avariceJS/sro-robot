"""add fedresurs as a read-only case acquisition source

Revision ID: a48ef390b2d1
Revises: f91a2d6b83c4
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op


revision = "a48ef390b2d1"
down_revision = "f91a2d6b83c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE intake_source ADD VALUE IF NOT EXISTS 'fedresurs'")
    op.drop_constraint("ck_job_outbox_safe_job_kind", "job_outbox", type_="check")
    op.create_check_constraint(
        "ck_job_outbox_safe_job_kind",
        "job_outbox",
        "job_kind IN ('ocr', 'kad', 'fedresurs', 'local_validation')",
    )
    op.drop_constraint("ck_job_outbox_actor_scope", "job_outbox", type_="check")
    op.create_check_constraint(
        "ck_job_outbox_actor_scope",
        "job_outbox",
        "(job_kind = 'local_validation' AND actor_id IS NOT NULL) OR "
        "(job_kind IN ('ocr', 'kad', 'fedresurs') AND actor_id IS NULL)",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Fedresurs source provenance and accepted tasks must not be removed automatically"
    )
