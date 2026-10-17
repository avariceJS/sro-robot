"""support internal legal entity and sole proprietor debtor cards

Revision ID: 8b2d71c4e906
Revises: 6d8f31a4c920
Create Date: 2026-10-17
"""

from __future__ import annotations

from alembic import op


revision = "8b2d71c4e906"
down_revision = "6d8f31a4c920"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_debtor_card_draft_entity_kind", "debtor_card_draft", type_="check")
    op.create_check_constraint(
        "ck_debtor_card_draft_entity_kind",
        "debtor_card_draft",
        "entity_kind IN ('person', 'legal_entity', 'sole_proprietor')",
    )
    op.create_check_constraint(
        "ck_debtor_card_draft_inn_length",
        "debtor_card_draft",
        "inn IS NULL OR "
        "(entity_kind = 'person' AND length(inn) = 12) OR "
        "(entity_kind = 'legal_entity' AND length(inn) = 10) OR "
        "(entity_kind = 'sole_proprietor' AND length(inn) = 12)",
    )
    op.execute("DROP TRIGGER debtor_card_record_revision ON public.debtor_card_draft")
    op.execute(
        """CREATE OR REPLACE FUNCTION public.sro_record_debtor_card_revision()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            INSERT INTO public.debtor_card_draft_revision
                (debtor_card_id, revision, snapshot, changed_by_id)
            VALUES (NEW.id, NEW.version,
                jsonb_build_object(
                    'entity_kind', NEW.entity_kind,
                    'display_name', NEW.display_name,
                    'inn', NEW.inn,
                    'values', NEW.values,
                    'preparation_state', NEW.preparation_state
                ), NEW.updated_by_id);
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER debtor_card_record_revision
        AFTER INSERT OR UPDATE OF entity_kind, display_name, inn, values,
            preparation_state, version, updated_by_id
        ON public.debtor_card_draft FOR EACH ROW
        EXECUTE FUNCTION public.sro_record_debtor_card_revision()"""
    )


def downgrade() -> None:
    raise RuntimeError("Debtor entity cards are staff data; use a reviewed forward migration.")
