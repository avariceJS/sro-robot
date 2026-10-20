"""Add employee-curated case workspaces with immutable task membership history.

Revision ID: d12f6a84c3e1
Revises: c9e4a17b2d60
Create Date: 2026-10-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d12f6a84c3e1"
down_revision = "c9e4a17b2d60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_package",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("case_number", sa.String(length=64), server_default="", nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("request_key_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 255", name="ck_case_package_title_length"),
        sa.CheckConstraint("case_number = '' OR length(case_number) <= 64", name="ck_case_package_case_number_length"),
        sa.CheckConstraint("request_key_sha256 ~ '^[0-9a-f]{64}$'", name="ck_case_package_request_key_sha256"),
        sa.CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_case_package_payload_sha256"),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"]),
        sa.UniqueConstraint("request_key_sha256", name="uq_case_package_request_key_sha256"),
    )
    op.create_index("ix_case_package_created", "case_package", ["created_at", "id"])
    op.create_table(
        "case_package_task_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_package_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.CheckConstraint("action IN ('added', 'removed')", name="ck_case_package_task_event_action"),
        sa.ForeignKeyConstraint(["case_package_id"], ["case_package.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["user.id"]),
    )
    op.create_index(
        "ix_case_package_task_event_membership",
        "case_package_task_event",
        ["case_package_id", "task_id", "id"],
    )
    op.execute("""
        CREATE FUNCTION public.sro_reject_case_package_task_event_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'case package task events are append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER case_package_task_event_append_only
        BEFORE UPDATE OR DELETE ON public.case_package_task_event FOR EACH ROW
        EXECUTE FUNCTION public.sro_reject_case_package_task_event_mutation()
    """)
    op.execute("""
        CREATE TRIGGER case_package_task_event_no_truncate
        BEFORE TRUNCATE ON public.case_package_task_event FOR EACH STATEMENT
        EXECUTE FUNCTION public.sro_reject_case_package_task_event_mutation()
    """)
    op.execute("""
        CREATE FUNCTION public.sro_reject_case_package_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'case package identity is immutable';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER case_package_identity_immutable
        BEFORE UPDATE OR DELETE ON public.case_package FOR EACH ROW
        EXECUTE FUNCTION public.sro_reject_case_package_mutation()
    """)
    op.execute("""
        CREATE TRIGGER case_package_no_truncate
        BEFORE TRUNCATE ON public.case_package FOR EACH STATEMENT
        EXECUTE FUNCTION public.sro_reject_case_package_mutation()
    """)


def downgrade() -> None:
    raise RuntimeError("Case package membership history must be preserved; use the reviewed checkpoint rollback path")
