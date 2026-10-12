"""preserve each accepted source occurrence on its task

Revision ID: 9ad3c7e10f62
Revises: c4a781b6d902
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "9ad3c7e10f62"
down_revision = "c4a781b6d902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intake_item",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Integer(), nullable=False),
        sa.Column("item_index", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column(
            "source",
            postgresql.ENUM(name="intake_source", create_type=False),
            nullable=False,
        ),
        sa.Column("received_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("item_index >= 0", name="ck_intake_item_nonnegative_index"),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.ForeignKeyConstraint(["operation_id"], ["intake_operation.id"]),
        sa.ForeignKeyConstraint(["received_by_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id", "item_index", name="uq_intake_item_operation_index"
        ),
        sa.UniqueConstraint(
            "id", "operation_id", "document_id", name="uq_intake_item_context"
        ),
    )
    op.add_column("task", sa.Column("intake_item_id", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_task_intake_item_id", "task", ["intake_item_id"])
    op.create_foreign_key(
        "fk_task_intake_item_context",
        "task",
        "intake_item",
        ["intake_item_id", "intake_operation_id", "document_id"],
        ["id", "operation_id", "document_id"],
    )

    # Preserve the best provenance available for already accepted operations.
    # New admissions retain each original filename and source independently.
    op.execute(
        sa.text(
            "WITH ranked AS MATERIALIZED ("
            " SELECT t.id AS task_id, t.intake_operation_id AS operation_id,"
            " row_number() OVER (PARTITION BY t.intake_operation_id ORDER BY t.id) - 1"
            " AS item_index, t.document_id, d.filename, d.source, io.user_id, t.created_at"
            " FROM task AS t"
            " JOIN document AS d ON d.id = t.document_id"
            " JOIN intake_operation AS io ON io.id = t.intake_operation_id"
            " WHERE t.intake_operation_id IS NOT NULL"
            "), inserted AS ("
            " INSERT INTO intake_item"
            " (operation_id, item_index, document_id, filename, source, received_by_id, created_at)"
            " SELECT operation_id, item_index, document_id, filename, source, user_id, created_at"
            " FROM ranked RETURNING id, operation_id, item_index"
            ")"
            " UPDATE task AS t SET intake_item_id = i.id"
            " FROM ranked AS r JOIN inserted AS i"
            " ON i.operation_id = r.operation_id AND i.item_index = r.item_index"
            " WHERE t.id = r.task_id"
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "Intake source provenance must not be removed by downgrade; "
        "use a reviewed forward migration after preserving accepted work history."
    )
