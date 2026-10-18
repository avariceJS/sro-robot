"""Prevent duplicate company and sole-proprietor registration IDs.

Revision ID: b82f04c193ad
Revises: f58c2d41a693
"""

from alembic import op
import sqlalchemy as sa


revision = "b82f04c193ad"
down_revision = "f58c2d41a693"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("debtor_card_draft", sa.Column("ogrn", sa.String(length=13), nullable=True))
    op.add_column("debtor_card_draft", sa.Column("ogrnip", sa.String(length=15), nullable=True))
    op.execute(sa.text(
        "UPDATE debtor_card_draft SET ogrn = CASE "
        "WHEN entity_kind = 'legal_entity' THEN NULLIF(CASE "
        "WHEN values -> 'ogrn' ->> 'value' ~ '^[0-9 -]+$' "
        "THEN regexp_replace(values -> 'ogrn' ->> 'value', '[ -]', '', 'g') "
        "ELSE values -> 'ogrn' ->> 'value' END, '') END"
    ))
    op.execute(sa.text(
        "UPDATE debtor_card_draft SET ogrnip = CASE "
        "WHEN entity_kind = 'sole_proprietor' THEN NULLIF(CASE "
        "WHEN values -> 'ogrnip' ->> 'value' ~ '^[0-9 -]+$' "
        "THEN regexp_replace(values -> 'ogrnip' ->> 'value', '[ -]', '', 'g') "
        "ELSE values -> 'ogrnip' ->> 'value' END, '') END"
    ))
    op.create_check_constraint(
        "ck_debtor_card_draft_registry_id_kind",
        "debtor_card_draft",
        "(ogrn IS NULL OR entity_kind = 'legal_entity') AND "
        "(ogrnip IS NULL OR entity_kind = 'sole_proprietor')",
    )
    op.create_index(
        "uq_debtor_card_draft_legal_entity_ogrn",
        "debtor_card_draft",
        ["ogrn"],
        unique=True,
        postgresql_where=sa.text("entity_kind = 'legal_entity' AND ogrn IS NOT NULL"),
    )
    op.create_index(
        "uq_debtor_card_draft_sole_proprietor_ogrnip",
        "debtor_card_draft",
        ["ogrnip"],
        unique=True,
        postgresql_where=sa.text("entity_kind = 'sole_proprietor' AND ogrnip IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_debtor_card_draft_sole_proprietor_ogrnip",
        table_name="debtor_card_draft",
    )
    op.drop_index(
        "uq_debtor_card_draft_legal_entity_ogrn",
        table_name="debtor_card_draft",
    )
    op.drop_constraint(
        "ck_debtor_card_draft_registry_id_kind",
        "debtor_card_draft",
        type_="check",
    )
    op.drop_column("debtor_card_draft", "ogrnip")
    op.drop_column("debtor_card_draft", "ogrn")
