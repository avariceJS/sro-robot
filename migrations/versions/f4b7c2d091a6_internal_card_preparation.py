"""Distinguish automatic internal preparation from employee review.

Existing cards remain reviewed. No external operations or task approvals change.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f4b7c2d091a6"
down_revision = "ea912b30c761"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("debtor_card_draft", sa.Column(
        "preparation_state", sa.String(16), nullable=False, server_default="reviewed",
    ))
    op.create_check_constraint("ck_debtor_card_draft_preparation_state", "debtor_card_draft",
                               "preparation_state IN ('prepared', 'reviewed')")
    op.create_table(
        "debtor_card_draft_revision",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("debtor_card_id", sa.Integer(), sa.ForeignKey("debtor_card_draft.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("changed_by_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint("debtor_card_id", "revision", name="uq_debtor_card_draft_revision_number"),
        sa.CheckConstraint("revision >= 1", name="ck_debtor_card_draft_revision_positive"),
    )
    # Baseline snapshots are explicitly the migration-time current version;
    # there is no invented reconstruction of earlier employee edits.
    op.execute("""INSERT INTO public.debtor_card_draft_revision
        (debtor_card_id, revision, snapshot, changed_by_id)
        SELECT id, version, jsonb_build_object('display_name', display_name,
            'inn', inn, 'values', values, 'preparation_state', preparation_state,
            'origin', 'migration_baseline'), updated_by_id FROM public.debtor_card_draft""")
    op.execute("""CREATE FUNCTION public.sro_record_debtor_card_revision()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN INSERT INTO public.debtor_card_draft_revision
        (debtor_card_id, revision, snapshot, changed_by_id)
        VALUES (NEW.id, NEW.version, jsonb_build_object(
            'display_name', NEW.display_name, 'inn', NEW.inn, 'values', NEW.values,
            'preparation_state', NEW.preparation_state), NEW.updated_by_id);
        RETURN NEW; END; $$""")
    op.execute("""CREATE TRIGGER debtor_card_record_revision
        AFTER INSERT OR UPDATE OF display_name, inn, values, preparation_state, version, updated_by_id
        ON public.debtor_card_draft FOR EACH ROW
        EXECUTE FUNCTION public.sro_record_debtor_card_revision()""")
    op.execute("""CREATE TRIGGER debtor_card_revision_append_only BEFORE UPDATE OR DELETE
        ON public.debtor_card_draft_revision FOR EACH ROW
        EXECUTE FUNCTION public.sro_reject_assignment_draft_revision_mutation()""")
    op.execute("""CREATE TRIGGER debtor_card_revision_no_truncate BEFORE TRUNCATE
        ON public.debtor_card_draft_revision FOR EACH STATEMENT
        EXECUTE FUNCTION public.sro_reject_assignment_draft_revision_mutation()""")
    op.execute("""CREATE TRIGGER debtor_card_revision_require_trigger BEFORE INSERT
        ON public.debtor_card_draft_revision FOR EACH ROW
        EXECUTE FUNCTION public.sro_require_assignment_draft_revision_trigger()""")


def downgrade() -> None:
    # A downgrade would erase the distinction and misrepresent unreviewed cards.
    raise RuntimeError("Internal preparation provenance requires a forward-only migration")
