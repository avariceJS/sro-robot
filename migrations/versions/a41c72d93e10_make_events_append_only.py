"""make audit events append-only

Revision ID: a41c72d93e10
Revises: d84f3a210c6b
Create Date: 2026-10-11
"""

from __future__ import annotations

from alembic import op

revision = "a41c72d93e10"
down_revision = "d84f3a210c6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sro_reject_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'event rows are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER event_append_only
        BEFORE UPDATE OR DELETE ON public.event
        FOR EACH ROW EXECUTE FUNCTION public.sro_reject_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER event_no_truncate
        BEFORE TRUNCATE ON public.event
        FOR EACH STATEMENT EXECUTE FUNCTION public.sro_reject_event_mutation()
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Audit history cannot be made mutable automatically. Correct the issue with a "
        "reviewed forward migration after preserving an immutable copy."
    )
