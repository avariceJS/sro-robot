#!/usr/bin/env python3
"""Prove malformed persisted intake metadata blocks and safely retries migration."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.postgres_safety import assert_disposable_postgres


assert_disposable_postgres()
target_url = make_url(os.environ.get("SRO_INTAKE_DIGEST_DATABASE_URL", ""))
source_url = make_url(os.environ["DATABASE_URL"])
if (
    target_url.get_backend_name() != "postgresql"
    or target_url.host != source_url.host
    or target_url.port != source_url.port
    or target_url.database != "intake_digest_target"
):
    raise SystemExit("intake digest drill target is not the named disposable database")

target_env = os.environ.copy()
target_env["DATABASE_URL"] = target_url.render_as_string(hide_password=False)
target_env["MIGRATION_DATABASE_URL"] = target_env["DATABASE_URL"]
upgrade_source = (
    "from alembic import command; from alembic.config import Config; "
    "from sro_robot.config import ROOT; command.upgrade(Config(str(ROOT / 'alembic.ini')), "
    "REVISION)"
)


def upgrade(revision: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", upgrade_source.replace("REVISION", repr(revision))],
        env=target_env,
        check=False,
        timeout=60,
        capture_output=True,
        text=True,
    )


def migration_failure_type(result: subprocess.CompletedProcess[str]) -> str:
    output = result.stdout + result.stderr
    match = re.search(r"\b([A-Za-z]+Error)\b", output)
    return match.group(1) if match else "UnknownFailure"


prior_head = "b7e1c40d62a9"
result = upgrade(prior_head)
if result.returncode != 0:
    raise SystemExit(f"could not prepare prior-head database: {migration_failure_type(result)}")

engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.begin() as connection:
        user_id = connection.execute(
            text(
                "INSERT INTO \"user\" (login, full_name, password_hash, sro_login, is_active) "
                "VALUES ('synthetic-intake-digest', 'Synthetic', 'synthetic-only', "
                "'synthetic-intake-digest', true) RETURNING id"
            )
        ).scalar_one()
        operation_id = connection.execute(
            text(
                "INSERT INTO intake_operation "
                "(user_id, idempotency_key, manifest_sha256, source_metadata_sha256) "
                "VALUES (:user_id, :key, :manifest, 'invalid-digest') RETURNING id"
            ),
            {"user_id": user_id, "key": "d" * 48, "manifest": "e" * 64},
        ).scalar_one()
finally:
    engine.dispose()

result = upgrade("head")
if result.returncode == 0 or migration_failure_type(result) != "IntegrityError":
    raise SystemExit(
        "malformed intake metadata did not block upgrade with IntegrityError: "
        f"{migration_failure_type(result)}"
    )
print("digest_upgrade_failure=IntegrityError")

engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        row = connection.execute(
            text(
                "SELECT source_metadata_sha256 FROM intake_operation WHERE id=:operation_id"
            ),
            {"operation_id": operation_id},
        ).scalar_one()
        constraint_count = connection.execute(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conrelid='public.intake_operation'::regclass "
                "AND conname='ck_intake_operation_source_metadata_sha256'"
            )
        ).scalar_one()
        if revision != prior_head or row != "invalid-digest" or constraint_count != 0:
            raise SystemExit("failed digest migration changed revision, source row, or schema")
finally:
    engine.dispose()
print("failed_upgrade_state=preserved")

engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE intake_operation SET source_metadata_sha256=:digest WHERE id=:operation_id"
            ),
            {"digest": "f" * 64, "operation_id": operation_id},
        )
finally:
    engine.dispose()

result = upgrade("head")
if result.returncode != 0:
    raise SystemExit(f"migration retry after synthetic repair failed: {migration_failure_type(result)}")

engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        row, validated = connection.execute(
            text(
                "SELECT io.source_metadata_sha256, pc.convalidated "
                "FROM intake_operation AS io CROSS JOIN pg_constraint AS pc "
                "WHERE io.id=:operation_id "
                "AND pc.conrelid='public.intake_operation'::regclass "
                "AND pc.conname='ck_intake_operation_source_metadata_sha256'"
            ),
            {"operation_id": operation_id},
        ).one()
        if revision != "f91a2d6b83c4" or row != "f" * 64 or not validated:
            raise SystemExit("migration retry did not read back the repaired row and validated guard")
finally:
    engine.dispose()

print("digest_upgrade_retry=passed")
print("digest_constraint_readback=validated")
