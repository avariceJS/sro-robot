"""require an active account for database login-session inserts

Revision ID: 1fc64da92b1e
Revises: e3c1d5a7b904
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op


revision = "1fc64da92b1e"
down_revision = "e3c1d5a7b904"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.sro_guard_login_session_write()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        DECLARE
            account_is_active boolean;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.user_id IS DISTINCT FROM OLD.user_id
                    OR NEW.token_sha256 IS DISTINCT FROM OLD.token_sha256
                    OR NEW.created_at IS DISTINCT FROM OLD.created_at
                    OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
                    OR (OLD.revoked_at IS NOT NULL
                        AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'login session identity and revocation are immutable';
                END IF;
                RETURN NEW;
            END IF;

            SELECT is_active INTO account_is_active
            FROM public."user"
            WHERE id = NEW.user_id
            FOR UPDATE;

            IF NOT FOUND OR account_is_active IS NOT TRUE THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'login session requires an active account';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER login_session_write_guard
        BEFORE INSERT OR UPDATE ON public.login_session
        FOR EACH ROW EXECUTE FUNCTION public.sro_guard_login_session_write()
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Active-account login-session enforcement must not be removed automatically; "
        "use a reviewed forward migration after preserving the authentication boundary."
    )
