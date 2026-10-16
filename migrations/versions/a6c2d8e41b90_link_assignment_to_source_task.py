"""link internal assignments to their source document task

Revision ID: a6c2d8e41b90
Revises: f4b7c2d091a6
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa


revision = "a6c2d8e41b90"
down_revision = "f4b7c2d091a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assignment_draft",
        sa.Column("source_task_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_assignment_draft_source_task",
        "assignment_draft",
        "task",
        ["source_task_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_assignment_draft_source_task",
        "assignment_draft",
        ["source_task_id"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.sro_record_assignment_draft_revision()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            INSERT INTO public.assignment_draft_revision
                (assignment_draft_id, revision, status, snapshot, changed_by_id)
            VALUES
                (NEW.id, NEW.version, NEW.status,
                 jsonb_build_object(
                    'debtor_card_id', NEW.debtor_card_id,
                    'source_task_id', NEW.source_task_id,
                    'case_number', NEW.case_number,
                    'values', NEW.values
                 ), NEW.updated_by_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER assignment_draft_record_revision ON public.assignment_draft")
    op.execute(
        """
        CREATE TRIGGER assignment_draft_record_revision
        AFTER INSERT OR UPDATE OF debtor_card_id, source_task_id, case_number, status,
        "values", version, updated_by_id ON public.assignment_draft
        FOR EACH ROW EXECUTE FUNCTION public.sro_record_assignment_draft_revision()
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Internal assignment provenance is forward-only; preserve drafts and upgrade with a reviewed migration."
    )
