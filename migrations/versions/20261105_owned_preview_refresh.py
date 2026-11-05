"""Persist a one-shot same-GUID preview refresh request and claim.

Revision ID: 20261105_refresh
Revises: 20261105_proposal_guard
"""
from __future__ import annotations

import hashlib
import sqlalchemy as sa
from alembic import op


revision = "20261105_refresh"
down_revision = "20261105_proposal_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task", sa.Column("preview_refresh_state", sa.String(16), server_default="idle", nullable=False))
    op.add_column("task", sa.Column("preview_refresh_actor_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True))
    op.add_column("task", sa.Column("preview_refresh_request_id", sa.String(128), nullable=True))
    op.add_column("task", sa.Column("preview_refresh_previous_token", sa.String(64), nullable=True))
    op.add_column("task", sa.Column("preview_refresh_values_sha256", sa.String(64), nullable=True))
    op.add_column("task", sa.Column("preview_refresh_generation", sa.Integer(), server_default="0", nullable=False))
    op.create_check_constraint("ck_task_preview_refresh_state", "task",
                               "preview_refresh_state IN ('idle', 'requested', 'started', 'unknown') AND preview_refresh_generation >= 0")
    op.create_check_constraint("ck_task_preview_refresh_provenance", "task",
                               "(preview_refresh_state = 'idle' AND (preview_refresh_request_id IS NULL OR preview_refresh_actor_id IS NOT NULL)) "
                               "OR (preview_refresh_state <> 'idle' AND preview_mode = 'durable_closed' AND owned_add_state = 'bound' "
                               "AND preview_refresh_actor_id IS NOT NULL AND preview_refresh_request_id IS NOT NULL "
                               "AND preview_refresh_previous_token ~ '^[0-9a-f]{64}$' "
                               "AND preview_refresh_values_sha256 ~ '^[0-9a-f]{64}$')")
    op.execute("""
        CREATE FUNCTION public.sro_guard_owned_preview_refresh()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.preview_refresh_state <> 'idle' OR NEW.preview_refresh_generation <> 0 THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid initial refresh state';
                END IF;
            ELSIF OLD.preview_refresh_state = 'unknown' THEN
                IF NEW.preview_refresh_state IS DISTINCT FROM OLD.preview_refresh_state
                   OR NEW.preview_refresh_generation IS DISTINCT FROM OLD.preview_refresh_generation
                   OR NEW.preview_refresh_actor_id IS DISTINCT FROM OLD.preview_refresh_actor_id
                   OR NEW.preview_refresh_request_id IS DISTINCT FROM OLD.preview_refresh_request_id
                   OR NEW.preview_refresh_previous_token IS DISTINCT FROM OLD.preview_refresh_previous_token
                   OR NEW.preview_refresh_values_sha256 IS DISTINCT FROM OLD.preview_refresh_values_sha256 THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'uncertain refresh is held';
                END IF;
            ELSIF OLD.preview_refresh_state = 'idle' THEN
                IF NEW.preview_refresh_state NOT IN ('idle', 'requested')
                   OR NEW.preview_refresh_generation IS DISTINCT FROM OLD.preview_refresh_generation THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid refresh request transition';
                END IF;
            ELSIF OLD.preview_refresh_state = 'requested' THEN
                IF NEW.preview_refresh_state NOT IN ('requested', 'started')
                   OR NEW.preview_refresh_generation IS DISTINCT FROM OLD.preview_refresh_generation
                   OR NEW.preview_refresh_actor_id IS DISTINCT FROM OLD.preview_refresh_actor_id
                   OR NEW.preview_refresh_request_id IS DISTINCT FROM OLD.preview_refresh_request_id
                   OR NEW.preview_refresh_previous_token IS DISTINCT FROM OLD.preview_refresh_previous_token
                   OR NEW.preview_refresh_values_sha256 IS DISTINCT FROM OLD.preview_refresh_values_sha256 THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid refresh claim transition';
                END IF;
            ELSIF OLD.preview_refresh_state = 'started' THEN
                IF NEW.preview_refresh_state NOT IN ('started', 'idle', 'unknown')
                   OR NEW.preview_refresh_actor_id IS DISTINCT FROM OLD.preview_refresh_actor_id
                   OR NEW.preview_refresh_request_id IS DISTINCT FROM OLD.preview_refresh_request_id
                   OR NEW.preview_refresh_previous_token IS DISTINCT FROM OLD.preview_refresh_previous_token
                   OR NEW.preview_refresh_values_sha256 IS DISTINCT FROM OLD.preview_refresh_values_sha256
                   OR (NEW.preview_refresh_state = 'idle' AND NEW.preview_refresh_generation <> OLD.preview_refresh_generation + 1)
                   OR (NEW.preview_refresh_state <> 'idle' AND NEW.preview_refresh_generation IS DISTINCT FROM OLD.preview_refresh_generation) THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid refresh completion transition';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER task_owned_preview_refresh_guard
        BEFORE INSERT OR UPDATE OF preview_refresh_state, preview_refresh_actor_id,
            preview_refresh_request_id, preview_refresh_previous_token,
            preview_refresh_values_sha256, preview_refresh_generation ON public.task
        FOR EACH ROW EXECUTE FUNCTION public.sro_guard_owned_preview_refresh()
    """)
    connection = op.get_bind()
    source = connection.execute(sa.text(
        "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname='public' AND p.proname='sro_enforce_task_status_transition' "
        "AND p.pronargs=0 AND p.prorettype='trigger'::regtype"
    )).scalar_one()
    if hashlib.sha256(" ".join(source.split()).encode()).hexdigest() != (
        "106c9bad2871c56a9a45479458f7a1cacbe55517ff790f16250d7c7dedaea3f3"
    ):
        raise RuntimeError("Task transition guard differs before review proposal admission")
    replacements = (
        ("AND NEW.approved_by_id IS NULL",
         "AND NEW.approved_by_id IS NULL AND NEW.preview_proposal_sha256 ~ '^[0-9a-f]{64}$' "
         "AND NEW.preview_source_occurrence <> '{}'::jsonb"),
        ("NEW.recognized->'_mixed_route_v1'->>'reason' = 'explicit_person_debtor'",
         "NEW.recognized->'_mixed_route_v1'->>'reason' IN ('explicit_person_debtor', 'review_person_candidate')"),
        ("NEW.recognized->'_mixed_route_v1'->>'reason' = 'explicit_entity_debtor'",
         "NEW.recognized->'_mixed_route_v1'->>'reason' IN ('explicit_entity_debtor', 'review_entity_candidate')"),
    )
    for old, new in replacements:
        if source.count(old) != 1:
            raise RuntimeError("Task transition guard insertion point is ambiguous")
        source = source.replace(old, new, 1)
    if hashlib.sha256(" ".join(source.split()).encode()).hexdigest() != (
        "6066eae60d1caaf12277dcd160098982442d4b254435ba497f28eb9533329728"
    ):
        raise RuntimeError("Review proposal transition guard differs from pinned source")
    connection.exec_driver_sql(
        "CREATE OR REPLACE FUNCTION public.sro_enforce_task_status_transition() "
        "RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog "
        "AS $sro_review_candidate_transition$\n" + source + "\n$sro_review_candidate_transition$"
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM task WHERE preview_refresh_request_id IS NOT NULL) ")).scalar_one():
        raise RuntimeError("Cannot remove owned preview refresh receipts")
    if connection.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM task WHERE preview_mode='durable_closed' "
        "AND recognized->'_mixed_route_v1'->>'reason' IN ('review_person_candidate', 'review_entity_candidate'))"
    )).scalar_one():
        raise RuntimeError("Cannot remove review candidate transition while admitted tasks exist")
    # Restore the prior exact transition source before dropping refresh columns.
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path
    previous = Path(__file__).with_name("20261105_mixed_preview_assignments.py")
    spec = spec_from_file_location("sro_mixed_transition_downgrade", previous)
    module = module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    op.execute(module._task_transition_function(mixed=True))
    op.execute("DROP TRIGGER task_owned_preview_refresh_guard ON public.task")
    op.execute("DROP FUNCTION public.sro_guard_owned_preview_refresh()")
    op.drop_constraint("ck_task_preview_refresh_provenance", "task", type_="check")
    op.drop_constraint("ck_task_preview_refresh_state", "task", type_="check")
    for column in ("preview_refresh_generation", "preview_refresh_values_sha256", "preview_refresh_previous_token",
                   "preview_refresh_request_id", "preview_refresh_actor_id", "preview_refresh_state"):
        op.drop_column("task", column)
