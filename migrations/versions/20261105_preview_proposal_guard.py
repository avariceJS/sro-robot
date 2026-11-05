"""Freeze a preview proposal once admitted to external preparation.

Revision ID: 20261105_proposal_guard
Revises: 20261105_proposal
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261105_proposal_guard"
down_revision = "20261105_proposal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.sro_guard_preview_proposal()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF OLD.preview_mode = 'durable_closed'
               AND OLD.preview_proposal_sha256 IS NOT NULL
               AND (NEW.preview_proposal_sha256 IS DISTINCT FROM OLD.preview_proposal_sha256
                    OR NEW.preview_source_occurrence IS DISTINCT FROM OLD.preview_source_occurrence) THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'admitted preview proposal is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER task_preview_proposal_guard
        BEFORE UPDATE OF preview_proposal_sha256, preview_source_occurrence ON public.task
        FOR EACH ROW EXECUTE FUNCTION public.sro_guard_preview_proposal()
    """)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM task WHERE preview_proposal_sha256 IS NOT NULL)"
    )).scalar_one():
        raise RuntimeError("Cannot remove admitted preview proposal custody")
    op.execute("DROP TRIGGER task_preview_proposal_guard ON public.task")
    op.execute("DROP FUNCTION public.sro_guard_preview_proposal()")
