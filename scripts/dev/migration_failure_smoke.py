#!/usr/bin/env python3
"""Verify PostgreSQL rolls back a partially executed additive migration."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from app.db.base import engine
from sro_robot.config import ROOT

BASELINE = "bc518c7514d3"
HEAD = "ea912b30c761"
EVENT_FUNCTION_MARKER = "/* sro-task-event-context */"
SYNTHETIC_FAILURE = "synthetic failure after task-context validator replacement"


def current_revision() -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def lease_table_exists() -> bool:
    with engine.connect() as connection:
        return inspect(connection).has_table("task_lease")


def event_contract_function_exists() -> bool:
    with engine.connect() as connection:
        return bool(
            connection.exec_driver_sql(
                "SELECT EXISTS (SELECT 1 FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' AND p.proname='sro_validate_event_insert')"
            ).scalar_one()
        )


def fail_after_event_function(self, statement, *args, **kwargs):
    result = original_execute(self, statement, *args, **kwargs)
    if EVENT_FUNCTION_MARKER in str(statement):
        raise RuntimeError(SYNTHETIC_FAILURE)
    return result


config = Config(str(ROOT / "alembic.ini"))
command.upgrade(config, BASELINE)
if current_revision() != BASELINE or lease_table_exists():
    raise SystemExit("baseline setup produced an unexpected schema")

original_execute = Connection.exec_driver_sql
try:
    with patch.object(Connection, "exec_driver_sql", fail_after_event_function):
        command.upgrade(config, "head")
except RuntimeError as exc:
    if str(exc) != SYNTHETIC_FAILURE:
        raise
else:
    raise SystemExit("injected migration failure did not occur")

if current_revision() != BASELINE or lease_table_exists() or event_contract_function_exists():
    raise SystemExit("failed migration left partial DDL or advanced revision")

retry = subprocess.run(
    [sys.executable, "-m", "app.cli", "init-db"],
    capture_output=True,
    text=True,
    timeout=20,
    check=False,
    env=os.environ.copy(),
)
if retry.returncode != 0 or "Traceback" in retry.stderr:
    raise SystemExit(
        f"migration retry failed after rollback (exit={retry.returncode}): "
        f"stdout={retry.stdout[-4000:]} stderr={retry.stderr[-4000:]}"
    )
if current_revision() != HEAD or not lease_table_exists() or not event_contract_function_exists():
    raise SystemExit("successful retry did not reach the expected migration head")

print("PostgreSQL migration failure rollback and locked retry passed")
