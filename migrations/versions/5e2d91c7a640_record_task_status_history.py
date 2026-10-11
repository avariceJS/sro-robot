"""record immutable task status changes

Revision ID: 5e2d91c7a640
Revises: 3b6a9f2d41c8
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "5e2d91c7a640"
down_revision = "3b6a9f2d41c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_status_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("from_status <> to_status", name="ck_task_status_history_changed"),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_task_status_history_task_id_id",
        "task_status_history",
        ["task_id", "id"],
    )
    op.execute(
        """
        CREATE FUNCTION public.sro_record_task_status_change()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            INSERT INTO public.task_status_history (task_id, from_status, to_status)
            VALUES (NEW.id, OLD.status::text, NEW.status::text);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER task_status_history_record_change
        AFTER UPDATE OF status ON public.task
        FOR EACH ROW
        WHEN (OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION public.sro_record_task_status_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.sro_reject_task_status_history_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'task status history rows are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER task_status_history_append_only
        BEFORE UPDATE OR DELETE ON public.task_status_history
        FOR EACH ROW EXECUTE FUNCTION public.sro_reject_task_status_history_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER task_status_history_no_truncate
        BEFORE TRUNCATE ON public.task_status_history
        FOR EACH STATEMENT EXECUTE FUNCTION public.sro_reject_task_status_history_mutation()
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Task status history cannot be removed automatically; preserve the immutable history "
        "and use a reviewed forward migration."
    )
