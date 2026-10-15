"""add internal debtor cards and resumable assignment drafts

Revision ID: d6a3e18b0421
Revises: b62d941a7f30
Create Date: 2026-10-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d6a3e18b0421"
down_revision = "c3a8e1d04b57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "debtor_card_draft",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_kind", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_task_id", sa.Integer(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False
        ),
        sa.CheckConstraint("entity_kind IN ('person')", name="ck_debtor_card_draft_entity_kind"),
        sa.CheckConstraint("version >= 1", name="ck_debtor_card_draft_version"),
        sa.ForeignKeyConstraint(["source_task_id"], ["task.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["updated_by_id"], ["user.id"]),
        sa.UniqueConstraint("source_task_id", name="uq_debtor_card_draft_source_task"),
    )
    op.create_index(
        "uq_debtor_card_draft_inn",
        "debtor_card_draft",
        ["inn"],
        unique=True,
        postgresql_where=sa.text("inn IS NOT NULL"),
    )
    op.create_index("ix_debtor_card_draft_display_name", "debtor_card_draft", ["display_name"])

    op.create_table(
        "assignment_draft",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("debtor_card_id", sa.Integer(), nullable=False),
        sa.Column("case_number", sa.String(length=64), server_default="", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="draft", nullable=False),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'awaiting_information', 'ready_for_review')",
            name="ck_assignment_draft_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_assignment_draft_version"),
        sa.ForeignKeyConstraint(["debtor_card_id"], ["debtor_card_draft.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["updated_by_id"], ["user.id"]),
    )
    op.create_index(
        "ix_assignment_draft_status_updated",
        "assignment_draft",
        ["status", "updated_at"],
    )

    op.create_table(
        "assignment_draft_revision",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assignment_draft_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("changed_by_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False
        ),
        sa.CheckConstraint("revision >= 1", name="ck_assignment_draft_revision_positive"),
        sa.CheckConstraint(
            "status IN ('draft', 'awaiting_information', 'ready_for_review')",
            name="ck_assignment_draft_revision_status",
        ),
        sa.ForeignKeyConstraint(["assignment_draft_id"], ["assignment_draft.id"]),
        sa.ForeignKeyConstraint(["changed_by_id"], ["user.id"]),
        sa.UniqueConstraint(
            "assignment_draft_id", "revision", name="uq_assignment_draft_revision_number"
        ),
    )
    op.create_index(
        "ix_assignment_draft_revision_parent",
        "assignment_draft_revision",
        ["assignment_draft_id", "revision"],
    )

    op.execute(
        """
        CREATE FUNCTION public.sro_record_assignment_draft_revision()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            INSERT INTO public.assignment_draft_revision
                (assignment_draft_id, revision, status, snapshot, changed_by_id)
            VALUES
                (NEW.id, NEW.version, NEW.status,
                 jsonb_build_object(
                    'debtor_card_id', NEW.debtor_card_id,
                    'case_number', NEW.case_number,
                    'values', NEW.values
                 ), NEW.updated_by_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER assignment_draft_record_revision
        AFTER INSERT OR UPDATE OF debtor_card_id, case_number, status, values, version, updated_by_id
        ON public.assignment_draft FOR EACH ROW
        EXECUTE FUNCTION public.sro_record_assignment_draft_revision()
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.sro_reject_assignment_draft_revision_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'assignment draft revisions are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER assignment_draft_revision_append_only
        BEFORE UPDATE OR DELETE ON public.assignment_draft_revision FOR EACH ROW
        EXECUTE FUNCTION public.sro_reject_assignment_draft_revision_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER assignment_draft_revision_no_truncate
        BEFORE TRUNCATE ON public.assignment_draft_revision FOR EACH STATEMENT
        EXECUTE FUNCTION public.sro_reject_assignment_draft_revision_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.sro_require_assignment_draft_revision_trigger()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF pg_catalog.pg_trigger_depth() < 2 THEN
                RAISE EXCEPTION USING ERRCODE = '55000',
                    MESSAGE = 'assignment revisions require a draft write';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER assignment_draft_revision_require_trigger
        BEFORE INSERT ON public.assignment_draft_revision FOR EACH ROW
        EXECUTE FUNCTION public.sro_require_assignment_draft_revision_trigger()
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Internal debtor and assignment history must not be dropped automatically; "
        "use a reviewed forward migration after preserving staff drafts."
    )
