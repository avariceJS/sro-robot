"""enforce the task lifecycle graph in PostgreSQL

Revision ID: 3b6a9f2d41c8
Revises: f28c7a913bd4
Create Date: 2026-10-11
"""

from __future__ import annotations

from alembic import op


revision = "3b6a9f2d41c8"
down_revision = "f28c7a913bd4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sro_enforce_task_status_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
                RETURN NEW;
            END IF;

            IF (CASE OLD.status::text
                WHEN 'new' THEN NEW.status::text = ANY (ARRAY['recognizing', 'failed', 'rejected'])
                WHEN 'recognizing' THEN NEW.status::text = ANY (ARRAY['needs_review', 'failed', 'duplicate'])
                WHEN 'needs_review' THEN NEW.status::text = ANY (ARRAY['approved', 'rejected'])
                WHEN 'approved' THEN NEW.status::text = ANY (ARRAY['needs_review', 'rejected', 'writing', 'done', 'failed'])
                WHEN 'writing' THEN NEW.status::text = ANY (ARRAY['done', 'awaiting_preview'])
                ELSE FALSE
            END) IS NOT TRUE THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'task status transition is not allowed';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER task_status_transition_guard
        BEFORE UPDATE OF status ON public.task
        FOR EACH ROW EXECUTE FUNCTION public.sro_enforce_task_status_transition()
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Task lifecycle protection must not be removed automatically; "
        "use a reviewed forward migration after preserving workflow state."
    )
