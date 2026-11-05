"""Bind a coherent preview proposal to one employee debtor-role attestation.

Revision ID: 20261105_proposal
Revises: 20261105_owned
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261105_proposal"
down_revision = "20261105_owned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task", sa.Column("preview_proposal_sha256", sa.String(64), nullable=True))
    op.add_column("task", sa.Column("preview_source_occurrence", postgresql.JSONB(),
                                    server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.create_check_constraint("ck_task_preview_source_occurrence_object", "task",
                               "jsonb_typeof(preview_source_occurrence) = 'object'")
    op.create_check_constraint("ck_task_preview_proposal_hash", "task",
                               "preview_proposal_sha256 IS NULL OR preview_proposal_sha256 ~ '^[0-9a-f]{64}$'")
    op.create_table(
        "task_role_attestation",
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("task.id"), primary_key=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("preview_token", sa.String(64), nullable=False),
        sa.Column("proposal_sha256", sa.String(64), nullable=False),
        sa.Column("occurrence_sha256", sa.String(64), nullable=False),
        sa.Column("values_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.CheckConstraint("role = 'debtor'", name="ck_task_role_attestation_debtor"),
        sa.CheckConstraint("preview_token ~ '^[0-9a-f]{64}$' AND proposal_sha256 ~ '^[0-9a-f]{64}$' AND occurrence_sha256 ~ '^[0-9a-f]{64}$' AND values_sha256 ~ '^[0-9a-f]{64}$'", name="ck_task_role_attestation_hashes"),
    )
    op.execute("""
        CREATE FUNCTION public.sro_reject_task_role_attestation_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'role attestations are append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER task_role_attestation_append_only
        BEFORE UPDATE OR DELETE ON public.task_role_attestation FOR EACH ROW
        EXECUTE FUNCTION public.sro_reject_task_role_attestation_mutation()
    """)
    op.execute("""
        CREATE TRIGGER task_role_attestation_no_truncate
        BEFORE TRUNCATE ON public.task_role_attestation FOR EACH STATEMENT
        EXECUTE FUNCTION public.sro_reject_task_role_attestation_mutation()
    """)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM task_role_attestation)")).scalar_one():
        raise RuntimeError("Cannot downgrade while employee role attestations exist")
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM task WHERE preview_proposal_sha256 IS NOT NULL)")).scalar_one():
        raise RuntimeError("Cannot downgrade while bound preview proposals exist")
    op.execute("DROP TRIGGER task_role_attestation_no_truncate ON public.task_role_attestation")
    op.execute("DROP TRIGGER task_role_attestation_append_only ON public.task_role_attestation")
    op.execute("DROP FUNCTION public.sro_reject_task_role_attestation_mutation()")
    op.drop_table("task_role_attestation")
    op.drop_constraint("ck_task_preview_proposal_hash", "task", type_="check")
    op.drop_constraint("ck_task_preview_source_occurrence_object", "task", type_="check")
    op.drop_column("task", "preview_source_occurrence")
    op.drop_column("task", "preview_proposal_sha256")
