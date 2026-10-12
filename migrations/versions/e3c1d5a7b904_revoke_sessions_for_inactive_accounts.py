"""close stale login sessions for already inactive accounts

Revision ID: e3c1d5a7b904
Revises: d92f41b7a6c3
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op


revision = "e3c1d5a7b904"
down_revision = "d92f41b7a6c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE public.login_session AS session
        SET revoked_at = pg_catalog.clock_timestamp()
        FROM public."user" AS account
        WHERE account.id = session.user_id
          AND account.is_active IS FALSE
          AND session.revoked_at IS NULL
          AND session.expires_at > pg_catalog.clock_timestamp()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.sro_revoke_sessions_on_user_deactivation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF OLD.is_active IS DISTINCT FROM NEW.is_active THEN
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


def downgrade() -> None:
    raise RuntimeError(
        "Inactive-account session revocation must not be removed automatically; "
        "use a reviewed forward migration after reconciling session state."
    )
