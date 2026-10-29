#!/usr/bin/env python3
"""Verify reprocessing migration refuses managed-function drift transactionally."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.postgres_safety import assert_disposable_postgres
from migrations.versions.d74b20c6a913_add_shared_upload_transport_batches import (
    _REQUIRE_COMPLETE_FUNCTION as prior_function_sql,
)
replace_function_sql = prior_function_sql.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


assert_disposable_postgres()
target_url = make_url(os.environ.get("SRO_BATCH_REPROCESS_DATABASE_URL", ""))
source_url = make_url(os.environ["DATABASE_URL"])
if (
    target_url.get_backend_name() != "postgresql"
    or target_url.host != source_url.host
    or target_url.port != source_url.port
    or target_url.database != "batch_reprocess_migration_target"
):
    raise SystemExit("reprocessing migration target is not the named disposable database")

target_env = os.environ.copy()
target_env["DATABASE_URL"] = target_url.render_as_string(hide_password=False)
target_env["MIGRATION_DATABASE_URL"] = target_env["DATABASE_URL"]
def upgrade(revision: str) -> subprocess.CompletedProcess[str]:
    upgrade_code = (
        "from alembic import command; from alembic.config import Config; "
        f"from sro_robot.config import ROOT; command.upgrade(Config(str(ROOT / 'alembic.ini')), {revision!r})"
    )
    return subprocess.run(
        [sys.executable, "-c", upgrade_code],
        env=target_env,
        check=False,
        timeout=60,
        capture_output=True,
        text=True,
    )


result = upgrade("d74b20c6a913")
if result.returncode != 0:
    raise SystemExit("could not prepare the upload transport migration baseline")

engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.begin() as connection:
        connection.exec_driver_sql(replace_function_sql.replace("BEGIN", "-- synthetic drift probe\nBEGIN", 1))
finally:
    engine.dispose()

result = upgrade("head")
output = result.stdout + result.stderr
if result.returncode == 0 or "Refusing to replace a drifted upload manifest guard" not in output:
    raise SystemExit("migration did not refuse a drifted upload manifest guard")

engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        new_columns = connection.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='upload_transport_batch' "
            "AND column_name IN ('reprocess_parent_id', 'reprocess_parent_manifest_sha256', 'reprocess_request_key_sha256')"
        )).scalar_one()
        if revision != "d74b20c6a913" or new_columns != 0:
            raise SystemExit("refused migration changed schema revision or added lineage columns")
    with engine.begin() as connection:
        connection.exec_driver_sql(replace_function_sql)
finally:
    engine.dispose()

result = upgrade("head")
if result.returncode != 0:
    raise SystemExit("migration retry after restoring the managed guard failed")

engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        new_columns = connection.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='upload_transport_batch' "
            "AND column_name IN ('reprocess_parent_id', 'reprocess_parent_manifest_sha256', 'reprocess_request_key_sha256')"
        )).scalar_one()
        if revision != "20261029_preview" or new_columns != 3:
            raise SystemExit("migration retry did not publish all reprocessing columns")
finally:
    engine.dispose()

print("drifted_guard_migration=refused_without_partial_ddl")
print("restored_guard_migration_retry=passed")
