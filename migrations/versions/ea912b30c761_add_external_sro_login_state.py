"""Add encrypted per-login external SRO state."""

from alembic import op
import sqlalchemy as sa


revision = "ea912b30c761"
down_revision = "d6a3e18b0421"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "login_session",
        sa.Column("external_state_ciphertext", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "login_session",
        sa.Column("external_state_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("login_session", "external_state_expires_at")
    op.drop_column("login_session", "external_state_ciphertext")
