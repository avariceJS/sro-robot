"""add immutable OCR retry lineage

Revision ID: 74b9d2e1c6a0
Revises: e9c7b2a4106d
Create Date: 2026-10-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "74b9d2e1c6a0"
down_revision = "e9c7b2a4106d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_task_id_document", "task", ["id", "document_id"])
    op.create_unique_constraint(
        "uq_task_status_history_id_task", "task_status_history", ["id", "task_id"]
    )
    op.create_table(
        "ocr_retry_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parent_task_id", sa.Integer(), nullable=False),
        sa.Column("failure_history_id", sa.Integer(), nullable=False),
        sa.Column("child_task_id", sa.Integer(), nullable=False),
        sa.Column("source_intake_item_id", sa.Integer(), nullable=False),
        sa.Column("source_operation_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("requested_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False),
        sa.UniqueConstraint("parent_task_id", name="uq_ocr_retry_run_parent"),
        sa.UniqueConstraint("failure_history_id", name="uq_ocr_retry_run_failure"),
        sa.UniqueConstraint("child_task_id", name="uq_ocr_retry_run_child"),
        sa.CheckConstraint("parent_task_id <> child_task_id", name="ck_ocr_retry_distinct_tasks"),
        sa.ForeignKeyConstraint(
            ["parent_task_id", "document_id"], ["task.id", "task.document_id"],
            name="fk_ocr_retry_parent_document",
        ),
        sa.ForeignKeyConstraint(
            ["failure_history_id", "parent_task_id"],
            ["task_status_history.id", "task_status_history.task_id"],
            name="fk_ocr_retry_failure_parent",
        ),
        sa.ForeignKeyConstraint(
            ["child_task_id", "document_id"], ["task.id", "task.document_id"],
            name="fk_ocr_retry_child_document",
        ),
        sa.ForeignKeyConstraint(
            ["source_intake_item_id", "source_operation_id", "document_id"],
            ["intake_item.id", "intake_item.operation_id", "intake_item.document_id"],
            name="fk_ocr_retry_source_receipt",
        ),
        sa.ForeignKeyConstraint(["requested_by_id"], ["user.id"], name="fk_ocr_retry_actor"),
    )
    op.execute("""
    CREATE FUNCTION public.sro_validate_ocr_retry_insert()
    RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $ocr_retry_guard$
    DECLARE
        parent_row record;
        child_row record;
        history_count integer;
        source_item_id integer;
        source_operation_id integer;
        source_document_id integer;
        failure_row record;
    BEGIN
        SELECT id, document_id, status, intake_item_id, intake_operation_id, recognized
          INTO parent_row FROM public.task WHERE id = NEW.parent_task_id FOR UPDATE;
        SELECT id, document_id, status, intake_item_id, intake_operation_id,
               recognized, "values", case_number, approved_by_id, record_url,
               error, dry_run, screenshots, browser_decision
          INTO child_row FROM public.task WHERE id = NEW.child_task_id FOR UPDATE;
        SELECT id, task_id, from_status, to_status INTO failure_row
          FROM public.task_status_history
          WHERE task_id = NEW.parent_task_id ORDER BY id DESC LIMIT 1;
        SELECT count(*) INTO history_count FROM public.task_status_history
          WHERE task_id = NEW.parent_task_id;

        IF parent_row.id IS NULL OR child_row.id IS NULL
           OR parent_row.status <> 'failed' OR failure_row.id <> NEW.failure_history_id
           OR failure_row.from_status <> 'recognizing' OR failure_row.to_status <> 'failed'
           OR history_count <> 2 THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'OCR retry requires the exact initial OCR failure';
        END IF;
        IF parent_row.document_id <> NEW.document_id OR child_row.document_id <> NEW.document_id
           OR child_row.status <> 'new' OR child_row.intake_item_id IS NOT NULL
           OR child_row.intake_operation_id IS NOT NULL OR child_row.recognized <> '{}'::jsonb
           OR child_row.values <> '{}'::jsonb OR child_row.case_number <> ''
           OR child_row.approved_by_id IS NOT NULL OR child_row.record_url <> ''
           OR child_row.error <> '' OR child_row.dry_run IS NOT TRUE
           OR child_row.screenshots <> '[]'::jsonb OR child_row.browser_decision <> '' THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'OCR retry child must start without inherited review or execution state';
        END IF;
        IF parent_row.recognized ? '_kad_source_v1'
           OR EXISTS (SELECT 1 FROM public.kad_source_run
                      WHERE task_id = NEW.parent_task_id OR operation_id = parent_row.intake_operation_id)
           OR EXISTS (SELECT 1 FROM public.kad_import_request_receipt
                      WHERE operation_id = parent_row.intake_operation_id)
           OR EXISTS (
                SELECT 1 FROM public.event
                WHERE task_id = NEW.parent_task_id
                  AND kind IN ('created_from_kad', 'kad_lookup_started', 'kad_document_received',
                               'recognized', 'fields_saved', 'approved', 'approval_invalidated',
                               'local_validation_requested', 'writing_started',
                               'local_validation_completed', 'preview_started', 'decision_close',
                               'preview_timeout', 'live_save_blocked', 'not_approved', 'done')
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'KAD and SRO tasks cannot use OCR retry';
        END IF;

        IF parent_row.intake_item_id IS NOT NULL THEN
            source_item_id := parent_row.intake_item_id;
            source_operation_id := parent_row.intake_operation_id;
            source_document_id := parent_row.document_id;
        ELSE
            SELECT retry.source_intake_item_id, retry.source_operation_id, retry.document_id
              INTO source_item_id, source_operation_id, source_document_id
              FROM public.ocr_retry_run AS retry WHERE retry.child_task_id = NEW.parent_task_id;
        END IF;
        IF source_item_id IS DISTINCT FROM NEW.source_intake_item_id
           OR source_operation_id IS DISTINCT FROM NEW.source_operation_id
           OR source_document_id IS DISTINCT FROM NEW.document_id THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'OCR retry source receipt does not match its parent lineage';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.intake_item item
            JOIN public.document document ON document.id = item.document_id
            WHERE item.id = NEW.source_intake_item_id
              AND item.operation_id = NEW.source_operation_id
              AND item.document_id = NEW.document_id
              AND item.source IN ('upload', 'batch')
              AND document.source IN ('upload', 'batch')
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'OCR retry requires an upload or batch receipt';
        END IF;
        RETURN NEW;
    END;
    $ocr_retry_guard$;
    CREATE TRIGGER ocr_retry_run_validate_insert
    BEFORE INSERT ON public.ocr_retry_run FOR EACH ROW
    EXECUTE FUNCTION public.sro_validate_ocr_retry_insert();

    CREATE FUNCTION public.sro_reject_ocr_retry_mutation()
    RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $ocr_retry_immutable$
    BEGIN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'OCR retry lineage is append-only';
    END;
    $ocr_retry_immutable$;
    CREATE TRIGGER ocr_retry_run_append_only
    BEFORE UPDATE OR DELETE ON public.ocr_retry_run FOR EACH ROW
    EXECUTE FUNCTION public.sro_reject_ocr_retry_mutation();
    CREATE TRIGGER ocr_retry_run_no_truncate
    BEFORE TRUNCATE ON public.ocr_retry_run FOR EACH STATEMENT
    EXECUTE FUNCTION public.sro_reject_ocr_retry_mutation();
    """)


def downgrade() -> None:
    raise RuntimeError("OCR retry lineage is durable workflow history and cannot be removed automatically")
