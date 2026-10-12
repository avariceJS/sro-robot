#!/usr/bin/env python3
"""Verify stale inactive-account sessions are backfilled and stay revoked."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.postgres_safety import assert_disposable_postgres


assert_disposable_postgres()
target_url = make_url(os.environ.get("SRO_DEACTIVATION_DATABASE_URL", ""))
source_url = make_url(os.environ["DATABASE_URL"])
if (
    target_url.get_backend_name() != "postgresql"
    or target_url.host != source_url.host
    or target_url.port != source_url.port
    or target_url.database != "deactivation_session_target"
):
    raise SystemExit("deactivation session drill target is not the named disposable database")

target_env = os.environ.copy()
target_env["DATABASE_URL"] = target_url.render_as_string(hide_password=False)
target_env["MIGRATION_DATABASE_URL"] = target_env["DATABASE_URL"]
upgrade_source = (
    "from alembic import command; from alembic.config import Config; "
    "from sro_robot.config import ROOT; command.upgrade(Config(str(ROOT / 'alembic.ini')), "
    "REVISION)"
)


def upgrade(revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", upgrade_source.replace("REVISION", repr(revision))],
        env=target_env,
        check=False,
        timeout=60,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"deactivation session migration failed: exit={result.returncode}")


upgrade("d92f41b7a6c3")
engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.begin() as connection:
        user_id = connection.execute(
            text(
                'INSERT INTO public."user" '
                "(login, full_name, password_hash, sro_login, is_active) "
                "VALUES ('synthetic-inactive-session', 'Synthetic', 'synthetic-only', "
                "'synthetic', false) RETURNING id"
            )
        ).scalar_one()
        active_digest = "a" * 64
        expired_digest = "b" * 64
        connection.execute(
            text(
                "INSERT INTO public.login_session "
                "(user_id, token_sha256, expires_at, revoked_at) "
                "VALUES (:user_id, :active_digest, :active_expiry, NULL), "
                "(:user_id, :expired_digest, :expired_expiry, NULL)"
            ),
            {
                "user_id": user_id,
                "active_digest": active_digest,
                "active_expiry": datetime.now(timezone.utc) + timedelta(days=1),
                "expired_digest": expired_digest,
                "expired_expiry": datetime.now(timezone.utc) - timedelta(days=1),
            },
        )
finally:
    engine.dispose()

upgrade("head")
engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.begin() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM public.alembic_version")
        ).scalar_one()
        active_revoked = connection.execute(
            text("SELECT revoked_at IS NOT NULL FROM public.login_session WHERE token_sha256=:d"),
            {"d": active_digest},
        ).scalar_one()
        expired_revoked = connection.execute(
            text("SELECT revoked_at IS NOT NULL FROM public.login_session WHERE token_sha256=:d"),
            {"d": expired_digest},
        ).scalar_one()
        if revision != "e3c1d5a7b904" or not active_revoked or expired_revoked:
            raise SystemExit("inactive-account session backfill did not preserve intended row state")

        # Recreate the stale row condition after migration to verify a later
        # false-to-true state transition cannot revive it.
        connection.execute(
            text("UPDATE public.login_session SET revoked_at=NULL WHERE token_sha256=:d"),
            {"d": active_digest},
        )
        connection.execute(
            text('UPDATE public."user" SET is_active=true WHERE id=:id'),
            {"id": user_id},
        )
        reactivation_revoked = connection.execute(
            text("SELECT revoked_at IS NOT NULL FROM public.login_session WHERE token_sha256=:d"),
            {"d": active_digest},
        ).scalar_one()
        if not reactivation_revoked:
            raise SystemExit("account reactivation restored a stale browser session")
finally:
    engine.dispose()

print("inactive_account_session_backfill=passed")
print("reactivation_session_replay=blocked")
print("expired_session_backfill=unchanged")
