"""revoke login sessions at the database account boundary

Revision ID: d92f41b7a6c3
Revises: f61c2a9d04e8
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op


revision = "d92f41b7a6c3"
down_revision = "f61c2a9d04e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sro_revoke_sessions_on_user_deactivation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF OLD.is_active IS TRUE AND NEW.is_active IS FALSE THEN
                UPDATE public.login_session
                SET revoked_at = pg_catalog.clock_timestamp()
                WHERE user_id = NEW.id
                  AND revoked_at IS NULL
                  AND expires_at > pg_catalog.clock_timestamp();
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER user_revoke_login_sessions_on_deactivation
        AFTER UPDATE OF is_active ON public."user"
        FOR EACH ROW EXECUTE FUNCTION public.sro_revoke_sessions_on_user_deactivation()
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Account deactivation session revocation must not be removed automatically; "
        "use a reviewed forward migration after preserving the access-control boundary."
    )
