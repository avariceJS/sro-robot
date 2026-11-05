"""Persist closed form previews and source-bound applicant assignments.

Revision ID: 20261105_mixed
Revises: 20261029_preview
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261105_mixed"
down_revision = "20261029_preview"
branch_labels = None
depends_on = None


def _task_transition_function(*, mixed: bool) -> str:
    review_targets = (
        "NEW.status::text = ANY (ARRAY['approved', 'rejected']) OR "
        "(NEW.status::text = 'writing' AND NEW.preview_mode = 'durable_closed' "
        "AND NEW.dry_run = true AND NEW.approved_by_id IS NULL "
        "AND ((NEW.scenario = 'person_card' AND NEW.recognized->'_mixed_route_v1'->>'reason' = 'explicit_person_debtor') "
        "OR (NEW.scenario = 'entity_review' AND NEW.recognized->'_mixed_route_v1'->>'reason' = 'explicit_entity_debtor')))"
        if mixed else "NEW.status::text = ANY (ARRAY['approved', 'rejected'])"
    )
    return """
        CREATE OR REPLACE FUNCTION public.sro_enforce_task_status_transition()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.status IS NOT DISTINCT FROM OLD.status THEN RETURN NEW; END IF;
            IF (CASE OLD.status::text
                WHEN 'new' THEN NEW.status::text = ANY (ARRAY['recognizing', 'failed', 'rejected'])
                WHEN 'recognizing' THEN NEW.status::text = ANY (ARRAY['needs_review', 'failed', 'duplicate'])
                WHEN 'needs_review' THEN REVIEW_TARGETS
                WHEN 'approved' THEN NEW.status::text = ANY (ARRAY['needs_review', 'rejected', 'writing', 'done', 'failed'])
                WHEN 'writing' THEN NEW.status::text = ANY (ARRAY['done', 'awaiting_preview'])
                ELSE FALSE
            END) IS NOT TRUE THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'task status transition is not allowed';
            END IF;
            RETURN NEW;
        END;
        $$
    """.replace("REVIEW_TARGETS", review_targets)


def upgrade() -> None:
    op.add_column("task", sa.Column("preview_mode", sa.String(32), server_default="live_session", nullable=False))
    op.add_column("task", sa.Column("preview_purpose", sa.String(32), server_default="legacy", nullable=False))
    op.add_column("task", sa.Column("preview_values_sha256", sa.String(64), nullable=True))
    op.add_column("task", sa.Column("preview_schema_version", sa.Integer(), nullable=True))
    op.add_column("task", sa.Column("preview_screenshot_sha256", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.add_column("task", sa.Column("preview_destination", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("task", sa.Column("preview_save_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("task", sa.Column("local_finalization_state", sa.String(20), server_default="not_applicable", nullable=False))
    op.create_check_constraint("ck_task_preview_mode", "task", "preview_mode IN ('live_session', 'durable_closed')")
    op.create_check_constraint("ck_task_preview_purpose", "task", "(preview_mode = 'live_session' AND preview_purpose = 'legacy') OR (preview_mode = 'durable_closed' AND preview_purpose = 'new_record_review')")
    op.create_check_constraint("ck_task_preview_screenshot_hashes", "task", "jsonb_typeof(preview_screenshot_sha256) = 'array'")
    op.create_check_constraint("ck_task_preview_destination", "task", "jsonb_typeof(preview_destination) = 'object'")
    op.create_check_constraint("ck_task_local_finalization_state", "task", "local_finalization_state IN ('not_applicable', 'pending', 'succeeded', 'failed')")
    op.create_check_constraint("ck_task_preview_snapshot", "task", "preview_mode = 'live_session' OR (status <> 'awaiting_preview' AND preview_schema_version IS NULL AND preview_values_sha256 IS NULL) OR (preview_schema_version = 2 AND length(preview_values_sha256) = 64)")
    op.execute(_task_transition_function(mixed=True))
    op.create_table(
        "mixed_batch_prepare_intent",
        sa.Column("batch_id", sa.String(32), primary_key=True),
        sa.Column("file_index", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("task.id"), nullable=True),
        sa.Column("status", sa.String(24), server_default="pending_ocr", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.ForeignKeyConstraint(["batch_id", "file_index"], ["upload_transport_batch_item.batch_id", "upload_transport_batch_item.file_index"], name="fk_mixed_prepare_source_item"),
        sa.CheckConstraint("status IN ('pending_ocr', 'admitted', 'withheld')", name="ck_mixed_prepare_status"),
        sa.UniqueConstraint("task_id", name="uq_mixed_prepare_task"),
    )
    op.create_table(
        "task_local_finalization_attempt",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("task.id"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.CheckConstraint("outcome IN ('succeeded', 'failed')", name="ck_task_local_finalization_attempt_outcome"),
    )
    op.create_index("ix_task_local_finalization_attempt_task", "task_local_finalization_attempt", ["task_id", "id"])
    op.execute("""
        CREATE FUNCTION public.sro_reject_local_finalization_attempt_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'local finalization attempts are append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER task_local_finalization_attempt_append_only
        BEFORE UPDATE OR DELETE ON public.task_local_finalization_attempt FOR EACH ROW
        EXECUTE FUNCTION public.sro_reject_local_finalization_attempt_mutation()
    """)
    op.execute("""
        CREATE TRIGGER task_local_finalization_attempt_no_truncate
        BEFORE TRUNCATE ON public.task_local_finalization_attempt FOR EACH STATEMENT
        EXECUTE FUNCTION public.sro_reject_local_finalization_attempt_mutation()
    """)
    op.alter_column("assignment_draft", "debtor_card_id", nullable=True)
    op.add_column("assignment_draft", sa.Column("source_role", sa.String(16), server_default="debtor", nullable=False))
    op.create_check_constraint("ck_assignment_draft_source_role_card", "assignment_draft", "(source_role = 'debtor' AND debtor_card_id IS NOT NULL) OR (source_role = 'applicant' AND source_task_id IS NOT NULL)")
    op.execute("DROP TRIGGER assignment_draft_record_revision ON public.assignment_draft")
    op.execute("""
        CREATE OR REPLACE FUNCTION public.sro_record_assignment_draft_revision()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            INSERT INTO public.assignment_draft_revision
                (assignment_draft_id, revision, status, snapshot, changed_by_id)
            VALUES
                (NEW.id, NEW.version, NEW.status,
                 jsonb_build_object(
                    'debtor_card_id', NEW.debtor_card_id,
                    'source_role', NEW.source_role,
                    'source_task_id', NEW.source_task_id,
                    'case_number', NEW.case_number,
                    'values', NEW.values
                 ), NEW.updated_by_id);
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER assignment_draft_record_revision
        AFTER INSERT OR UPDATE OF debtor_card_id, source_role, source_task_id, case_number, status, values, version, updated_by_id
        ON public.assignment_draft FOR EACH ROW
        EXECUTE FUNCTION public.sro_record_assignment_draft_revision()
    """)


def downgrade() -> None:
    # Applicant drafts cannot be mapped to a debtor card without inventing one.
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM assignment_draft WHERE source_role = 'applicant')")).scalar_one():
        raise RuntimeError("Cannot downgrade while applicant assignment drafts exist")
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM task WHERE preview_mode = 'durable_closed')")).scalar_one():
        raise RuntimeError("Cannot downgrade while durable form previews exist")
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM mixed_batch_prepare_intent)")).scalar_one():
        raise RuntimeError("Cannot downgrade while mixed batch prepare intents exist")
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM task_local_finalization_attempt)")).scalar_one():
        raise RuntimeError("Cannot downgrade while local finalization audit attempts exist")
    op.execute("DROP TRIGGER task_local_finalization_attempt_no_truncate ON public.task_local_finalization_attempt")
    op.execute("DROP TRIGGER task_local_finalization_attempt_append_only ON public.task_local_finalization_attempt")
    op.execute("DROP FUNCTION public.sro_reject_local_finalization_attempt_mutation()")
    op.drop_index("ix_task_local_finalization_attempt_task", table_name="task_local_finalization_attempt")
    op.drop_table("task_local_finalization_attempt")
    op.drop_table("mixed_batch_prepare_intent")
    op.execute(_task_transition_function(mixed=False))
    op.execute("DROP TRIGGER assignment_draft_record_revision ON public.assignment_draft")
    op.execute("""
        CREATE OR REPLACE FUNCTION public.sro_record_assignment_draft_revision()
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
    """)
    op.execute("""
        CREATE TRIGGER assignment_draft_record_revision
        AFTER INSERT OR UPDATE OF debtor_card_id, case_number, status, values, version, updated_by_id
        ON public.assignment_draft FOR EACH ROW
        EXECUTE FUNCTION public.sro_record_assignment_draft_revision()
    """)
    op.drop_constraint("ck_assignment_draft_source_role_card", "assignment_draft", type_="check")
    op.drop_column("assignment_draft", "source_role")
    op.alter_column("assignment_draft", "debtor_card_id", nullable=False)
    op.drop_constraint("ck_task_preview_snapshot", "task", type_="check")
    op.drop_constraint("ck_task_preview_screenshot_hashes", "task", type_="check")
    op.drop_constraint("ck_task_preview_destination", "task", type_="check")
    op.drop_constraint("ck_task_local_finalization_state", "task", type_="check")
    op.drop_constraint("ck_task_preview_mode", "task", type_="check")
    op.drop_constraint("ck_task_preview_purpose", "task", type_="check")
    op.drop_column("task", "preview_save_enabled")
    op.drop_column("task", "local_finalization_state")
    op.drop_column("task", "preview_schema_version")
    op.drop_column("task", "preview_screenshot_sha256")
    op.drop_column("task", "preview_destination")
    op.drop_column("task", "preview_values_sha256")
    op.drop_column("task", "preview_mode")
    op.drop_column("task", "preview_purpose")
