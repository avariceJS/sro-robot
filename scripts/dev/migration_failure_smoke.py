#!/usr/bin/env python3
"""Verify PostgreSQL rolls back a partially executed additive migration."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from app.db.base import engine
from sro_robot.config import ROOT

BASELINE = "bc518c7514d3"
HEAD = "b7e1c40d62a9"
LEASE_INDEX = "ix_task_lease_lease_until"
SYNTHETIC_FAILURE = "synthetic failure after lease table creation"


def current_revision() -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def lease_table_exists() -> bool:
    with engine.connect() as connection:
        return inspect(connection).has_table("task_lease")


def fail_after_lease_table(self, index_name, *args, **kwargs):
    if index_name == LEASE_INDEX:
        raise RuntimeError(SYNTHETIC_FAILURE)
    return original_create_index(self, index_name, *args, **kwargs)


config = Config(str(ROOT / "alembic.ini"))
command.upgrade(config, BASELINE)
if current_revision() != BASELINE or lease_table_exists():
    raise SystemExit("baseline setup produced an unexpected schema")

original_create_index = Operations.create_index
try:
    with patch.object(Operations, "create_index", fail_after_lease_table):
        command.upgrade(config, "head")
except RuntimeError as exc:
    if str(exc) != SYNTHETIC_FAILURE:
        raise
else:
    raise SystemExit("injected migration failure did not occur")

if current_revision() != BASELINE or lease_table_exists():
    raise SystemExit("failed migration left a partial table or advanced revision")

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
if current_revision() != HEAD or not lease_table_exists():
    raise SystemExit("successful retry did not reach the expected migration head")

print("PostgreSQL migration failure rollback and locked retry passed")
