"""drop the unused public intake source and its document column

Revision ID: c3a8e1d04b57
Revises: b62d941a7f30
Create Date: 2026-10-13
"""

from __future__ import annotations

from alembic import op


revision = "c3a8e1d04b57"
down_revision = "b62d941a7f30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM document WHERE source::text = 'fedresurs') THEN
            RAISE EXCEPTION 'documents still use the retired intake source';
          END IF;
          IF EXISTS (SELECT 1 FROM intake_item WHERE source::text = 'fedresurs') THEN
            RAISE EXCEPTION 'intake items still use the retired intake source';
          END IF;
          IF EXISTS (SELECT 1 FROM job_outbox WHERE job_kind = 'fedresurs') THEN
            RAISE EXCEPTION 'outbox rows still use the retired job kind';
          END IF;
        END $$
        """
    )
    op.drop_column("document", "source_metadata")
    op.execute("ALTER TABLE document ALTER COLUMN source TYPE text USING source::text")
    op.execute("ALTER TABLE intake_item ALTER COLUMN source TYPE text USING source::text")
    op.execute("DROP TYPE intake_source")
    op.execute(
        "CREATE TYPE intake_source AS ENUM "
        "('upload', 'batch', 'mailbox', 'folder', 'kad')"
    )
    op.execute(
        "ALTER TABLE document ALTER COLUMN source TYPE intake_source "
        "USING source::intake_source"
    )
    op.execute(
        "ALTER TABLE intake_item ALTER COLUMN source TYPE intake_source "
        "USING source::intake_source"
    )
    op.drop_constraint("ck_job_outbox_safe_job_kind", "job_outbox", type_="check")
    op.create_check_constraint(
        "ck_job_outbox_safe_job_kind",
        "job_outbox",
        "job_kind IN ('ocr', 'kad', 'local_validation')",
    )
    op.drop_constraint("ck_job_outbox_actor_scope", "job_outbox", type_="check")
    op.create_check_constraint(
        "ck_job_outbox_actor_scope",
        "job_outbox",
        "(job_kind = 'local_validation' AND actor_id IS NOT NULL) OR "
        "(job_kind IN ('ocr', 'kad') AND actor_id IS NULL)",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Retired public-source acquisition schema must not be restored automatically"
    )
