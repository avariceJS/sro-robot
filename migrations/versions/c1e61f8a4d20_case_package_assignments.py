"""Track explicit assignment membership in case workspaces.

Revision ID: c1e61f8a4d20
Revises: a5f9c1d84b27
Create Date: 2026-10-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c1e61f8a4d20"
down_revision = "a5f9c1d84b27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_package_assignment_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_package_id", sa.Integer(), nullable=False),
        sa.Column("assignment_draft_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"), nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('added', 'removed')",
            name="ck_case_package_assignment_event_action",
        ),
        sa.ForeignKeyConstraint(["case_package_id"], ["case_package.id"]),
        sa.ForeignKeyConstraint(["assignment_draft_id"], ["assignment_draft.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["user.id"]),
    )
    op.create_index(
        "ix_case_package_assignment_event_membership",
        "case_package_assignment_event",
        ["case_package_id", "assignment_draft_id", "id"],
    )
    op.execute("""
        CREATE FUNCTION public.sro_reject_case_package_assignment_event_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'case package assignment events are append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER case_package_assignment_event_append_only
        BEFORE UPDATE OR DELETE ON public.case_package_assignment_event FOR EACH ROW
        EXECUTE FUNCTION public.sro_reject_case_package_assignment_event_mutation()
    """)
    op.execute("""
        CREATE TRIGGER case_package_assignment_event_no_truncate
        BEFORE TRUNCATE ON public.case_package_assignment_event FOR EACH STATEMENT
        EXECUTE FUNCTION public.sro_reject_case_package_assignment_event_mutation()
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Case package assignment history must be preserved; "
        "use the reviewed checkpoint rollback path"
    )
