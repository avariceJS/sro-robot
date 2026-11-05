"""Bind one technical SRO Add attempt to its exact owned draft.

Revision ID: 20261105_owned
Revises: 20261105_mixed
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261105_owned"
down_revision = "20261105_mixed"
branch_labels = None
depends_on = None


_PROVENANCE_CHECK = (
    "(owned_add_state = 'not_started' AND owned_add_actor_id IS NULL AND owned_add_started_at IS NULL "
    "AND owned_add_bound_at IS NULL AND owned_draft_url IS NULL AND owned_principal_sha256 IS NULL "
    "AND owned_baseline_sha256 IS NULL AND owned_baseline_schema_version IS NULL) OR "
    "(owned_add_state IN ('started', 'unknown') AND owned_add_actor_id IS NOT NULL "
    "AND owned_add_started_at IS NOT NULL AND owned_add_bound_at IS NULL AND owned_draft_url IS NULL "
    "AND owned_principal_sha256 ~ '^[0-9a-f]{64}$' AND owned_baseline_sha256 IS NULL "
    "AND owned_baseline_schema_version IS NULL) OR "
    "(owned_add_state = 'bound' AND owned_add_actor_id IS NOT NULL AND owned_add_started_at IS NOT NULL "
    "AND owned_add_bound_at IS NOT NULL AND owned_draft_url IS NOT NULL "
    "AND owned_principal_sha256 ~ '^[0-9a-f]{64}$' AND owned_baseline_sha256 ~ '^[0-9a-f]{64}$' "
    "AND owned_baseline_schema_version = 1)"
)


def upgrade() -> None:
    op.add_column("task", sa.Column("owned_add_state", sa.String(16), server_default="not_started", nullable=False))
    op.add_column("task", sa.Column("owned_add_actor_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True))
    op.add_column("task", sa.Column("owned_add_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("task", sa.Column("owned_add_bound_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("task", sa.Column("owned_draft_url", sa.String(1024), nullable=True))
    op.add_column("task", sa.Column("owned_principal_sha256", sa.String(64), nullable=True))
    op.add_column("task", sa.Column("owned_baseline", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("task", sa.Column("owned_baseline_sha256", sa.String(64), nullable=True))
    op.add_column("task", sa.Column("owned_baseline_schema_version", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_task_owned_draft_url", "task", ["owned_draft_url"])
    op.create_check_constraint("ck_task_owned_add_state", "task", "owned_add_state IN ('not_started', 'started', 'bound', 'unknown')")
    op.create_check_constraint("ck_task_owned_baseline_object", "task", "jsonb_typeof(owned_baseline) = 'object'")
    op.create_check_constraint("ck_task_owned_add_provenance", "task", _PROVENANCE_CHECK)
    op.execute("""
        CREATE FUNCTION public.sro_guard_owned_draft_binding()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.owned_add_state <> 'not_started' THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'owned Add must begin after task creation';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.owned_add_state IN ('bound', 'unknown') THEN
                IF NEW.owned_add_state IS DISTINCT FROM OLD.owned_add_state
                   OR NEW.owned_add_actor_id IS DISTINCT FROM OLD.owned_add_actor_id
                   OR NEW.owned_add_started_at IS DISTINCT FROM OLD.owned_add_started_at
                   OR NEW.owned_add_bound_at IS DISTINCT FROM OLD.owned_add_bound_at
                   OR NEW.owned_draft_url IS DISTINCT FROM OLD.owned_draft_url
                   OR NEW.owned_principal_sha256 IS DISTINCT FROM OLD.owned_principal_sha256
                   OR NEW.owned_baseline IS DISTINCT FROM OLD.owned_baseline
                   OR NEW.owned_baseline_sha256 IS DISTINCT FROM OLD.owned_baseline_sha256
                   OR NEW.owned_baseline_schema_version IS DISTINCT FROM OLD.owned_baseline_schema_version THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'owned draft binding is immutable';
                END IF;
            ELSIF OLD.owned_add_state = 'not_started' THEN
                IF NEW.owned_add_state NOT IN ('not_started', 'started') THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'owned Add claim is required';
                END IF;
            ELSIF OLD.owned_add_state = 'started' THEN
                IF NEW.owned_add_state NOT IN ('started', 'bound', 'unknown')
                   OR NEW.owned_add_actor_id IS DISTINCT FROM OLD.owned_add_actor_id
                   OR NEW.owned_add_started_at IS DISTINCT FROM OLD.owned_add_started_at
                   OR NEW.owned_principal_sha256 IS DISTINCT FROM OLD.owned_principal_sha256 THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'owned Add claim cannot change';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER task_owned_draft_binding_guard
        BEFORE INSERT OR UPDATE OF owned_add_state, owned_add_actor_id, owned_add_started_at,
            owned_add_bound_at, owned_draft_url, owned_principal_sha256, owned_baseline,
            owned_baseline_sha256, owned_baseline_schema_version
        ON public.task FOR EACH ROW EXECUTE FUNCTION public.sro_guard_owned_draft_binding()
    """)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM task WHERE owned_add_state <> 'not_started')")).scalar_one():
        raise RuntimeError("Cannot downgrade with claimed or bound owned SRO drafts")
    op.execute("DROP TRIGGER task_owned_draft_binding_guard ON public.task")
    op.execute("DROP FUNCTION public.sro_guard_owned_draft_binding()")
    op.drop_constraint("ck_task_owned_add_provenance", "task", type_="check")
    op.drop_constraint("ck_task_owned_baseline_object", "task", type_="check")
    op.drop_constraint("ck_task_owned_add_state", "task", type_="check")
    op.drop_constraint("uq_task_owned_draft_url", "task", type_="unique")
    op.drop_column("task", "owned_baseline_schema_version")
    op.drop_column("task", "owned_baseline_sha256")
    op.drop_column("task", "owned_baseline")
    op.drop_column("task", "owned_principal_sha256")
    op.drop_column("task", "owned_draft_url")
    op.drop_column("task", "owned_add_bound_at")
    op.drop_column("task", "owned_add_started_at")
    op.drop_column("task", "owned_add_actor_id")
    op.drop_column("task", "owned_add_state")
