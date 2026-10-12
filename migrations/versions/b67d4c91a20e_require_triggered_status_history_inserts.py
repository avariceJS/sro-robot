"""require task status history rows to come from the transition trigger

Revision ID: b67d4c91a20e
Revises: 1fc64da92b1e
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op


revision = "b67d4c91a20e"
down_revision = "1fc64da92b1e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sro_require_triggered_status_history_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF pg_catalog.pg_trigger_depth() < 2 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'task status history rows require a status transition';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER task_status_history_require_transition
        BEFORE INSERT ON public.task_status_history
        FOR EACH ROW EXECUTE FUNCTION public.sro_require_triggered_status_history_insert()
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Task status history insert integrity must not be removed automatically; "
        "use a reviewed forward migration after preserving audit history."
    )
