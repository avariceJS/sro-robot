#!/usr/bin/env python3
"""Exercise the source-occurrence migration backfill on a disposable clone."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.postgres_safety import assert_disposable_postgres


assert_disposable_postgres()
target_url = make_url(os.environ.get("SRO_INTAKE_BACKFILL_DATABASE_URL", ""))
source_url = make_url(os.environ["DATABASE_URL"])
if (
    target_url.get_backend_name() != "postgresql"
    or target_url.host != source_url.host
    or target_url.port != source_url.port
    or target_url.database != "intake_backfill_target"
):
    raise SystemExit("intake provenance drill target is not the named disposable database")

target_env = os.environ.copy()
target_env["DATABASE_URL"] = target_url.render_as_string(hide_password=False)
target_env["MIGRATION_DATABASE_URL"] = target_env["DATABASE_URL"]
python = sys.executable
alembic_upgrade = (
    "from alembic import command; from alembic.config import Config; "
    "from sro_robot.config import ROOT; command.upgrade(Config(str(ROOT / 'alembic.ini')), REVISION)"
)

subprocess.run(
    [python, "-c", alembic_upgrade.replace("REVISION", repr("c4a781b6d902"))],
    env=target_env,
    check=True,
    timeout=60,
    capture_output=True,
    text=True,
)

engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.begin() as connection:
        user_id = connection.execute(
            text(
                "INSERT INTO \"user\" (login, full_name, password_hash, sro_login, is_active) "
                "VALUES ('synthetic-intake-backfill', 'Synthetic', 'synthetic-only', "
                "'synthetic-intake-backfill', true) RETURNING id"
            )
        ).scalar_one()
        document_id = connection.execute(
            text(
                "INSERT INTO document "
                "(filename, stored_path, content_hash, source, uploaded_by_id) "
                "VALUES ('historical-first-name.pdf', '/tmp/synthetic-backfill.pdf', :digest, "
                "'upload', :user_id) RETURNING id"
            ),
            {"digest": "a" * 64, "user_id": user_id},
        ).scalar_one()
        operation_id = connection.execute(
            text(
                "INSERT INTO intake_operation (user_id, idempotency_key, manifest_sha256) "
                "VALUES (:user_id, :key, :manifest) RETURNING id"
            ),
            {"user_id": user_id, "key": "b" * 48, "manifest": "c" * 64},
        ).scalar_one()
        for ordinal in range(2):
            connection.execute(
                text(
                    "INSERT INTO task "
                    "(document_id, scenario, profile, status, recognized, values, case_number, "
                    "assignee_id, record_url, error, dry_run, screenshots, browser_decision, "
                    "intake_operation_id) "
                    "VALUES (:document_id, 'person', 'default', 'new', '{}'::jsonb, '{}'::jsonb, "
                    ":case_number, :user_id, '', '', true, '[]'::jsonb, '', :operation_id)"
                ),
                {
                    "document_id": document_id,
                    "case_number": f"synthetic-backfill-{ordinal}",
                    "user_id": user_id,
                    "operation_id": operation_id,
                },
            )
finally:
    engine.dispose()

subprocess.run(
    [python, "-c", alembic_upgrade.replace("REVISION", repr("head"))],
    env=target_env,
    check=True,
    timeout=60,
    capture_output=True,
    text=True,
)

engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT t.id, t.intake_item_id, i.operation_id, i.item_index, i.document_id, "
                "i.filename, i.source, i.received_by_id "
                "FROM task AS t JOIN intake_item AS i ON i.id=t.intake_item_id "
                "ORDER BY t.id"
            )
        ).all()
        if len(rows) != 2:
            raise SystemExit("intake provenance migration did not backfill both task occurrences")
        if [row.item_index for row in rows] != [0, 1]:
            raise SystemExit("intake provenance migration did not preserve per-operation order")
        if any(
            row.operation_id != operation_id
            or row.document_id != document_id
            or row.filename != "historical-first-name.pdf"
            or row.source != "upload"
            or row.received_by_id != user_id
            for row in rows
        ):
            raise SystemExit("intake provenance migration backfill did not preserve known context")
        print("intake_item_backfill_tasks=2")
        print("intake_item_backfill_order=preserved")
        print("intake_item_backfill_context=operation,document,source,uploader")
finally:
    engine.dispose()
