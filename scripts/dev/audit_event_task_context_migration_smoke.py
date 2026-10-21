#!/usr/bin/env python3
"""Prove audit-function drift blocks task-context migration and safely retries."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sro_robot.config import ROOT
from tests.postgres_safety import assert_disposable_postgres


assert_disposable_postgres()
source_url = make_url(os.environ["DATABASE_URL"])
target_url = make_url(os.environ.get("SRO_AUDIT_TASK_CONTEXT_DATABASE_URL", ""))
if (
    target_url.get_backend_name() != "postgresql"
    or target_url.host != source_url.host
    or target_url.port != source_url.port
    or target_url.database != "audit_task_context_target"
):
    raise SystemExit("audit task-context migration target is not the named disposable database")

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


def load_migration(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load pinned audit migration source")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prior_migration = load_migration(
    ROOT / "migrations/versions/e75a2c8d190b_require_audit_event_attribution.py",
    "sro_prior_audit_attribution_migration",
)
context_migration = load_migration(
    ROOT / "migrations/versions/f91a2d6b83c4_require_task_context_for_task_events.py",
    "sro_task_context_migration",
)
prior_head = "e75a2c8d190b"
result = upgrade(prior_head)
if result.returncode != 0:
    raise SystemExit("could not prepare task-context migration prior head")

engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.begin() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if revision != prior_head:
            raise SystemExit("migration smoke did not reach the pinned prior head")
        original_source_hash = connection.execute(
            text(
                "SELECT md5(p.prosrc) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' AND p.proname='sro_validate_event_insert'"
            )
        ).scalar_one()
        definition = connection.execute(
            text("SELECT pg_get_functiondef('public.sro_validate_event_insert()'::regprocedure)")
        ).scalar_one()
        tamper_anchor = "expected_message text;"
        if definition.count(tamper_anchor) != 1:
            raise SystemExit("audit function drift fixture anchor is ambiguous")
        tampered_definition = definition.replace(
            tamper_anchor,
            "expected_message text; -- synthetic drift marker",
            1,
        )
        connection.exec_driver_sql(tampered_definition)
        tampered_source_hash = connection.execute(
            text(
                "SELECT md5(p.prosrc) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' AND p.proname='sro_validate_event_insert'"
            )
        ).scalar_one()
        if tampered_source_hash == original_source_hash:
            raise SystemExit("synthetic event function drift was not applied")
finally:
    engine.dispose()

result = upgrade("head")
output = result.stdout + result.stderr
if (
    result.returncode == 0
    or "Audit event attribution guard changed before task-context migration" not in output
):
    raise SystemExit("task-context migration did not refuse changed audit function source")
print("audit_function_drift=refused")

engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.begin() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        current_source_hash = connection.execute(
            text(
                "SELECT md5(p.prosrc) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' AND p.proname='sro_validate_event_insert'"
            )
        ).scalar_one()
        if revision != prior_head or current_source_hash != tampered_source_hash:
            raise SystemExit("drift refusal changed the schema revision or function source")
        connection.exec_driver_sql(prior_migration._EVENT_CONTRACT_FUNCTION)
        restored_source_hash = connection.execute(
            text(
                "SELECT p.prosrc FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' AND p.proname='sro_validate_event_insert'"
            )
        ).scalar_one()
        restored_source_hash = hashlib.sha256(
            " ".join(restored_source_hash.split()).encode("utf-8")
        ).hexdigest()
        expected_restored_source = prior_migration._EVENT_CONTRACT_FUNCTION.split(
            "AS $$", 1
        )[1].split("$$", 1)[0]
        expected_restored_hash = hashlib.sha256(
            " ".join(expected_restored_source.split()).encode("utf-8")
        ).hexdigest()
        if restored_source_hash != expected_restored_hash:
            raise SystemExit("synthetic prior audit guard was not restored exactly")
finally:
    engine.dispose()

result = upgrade("head")
if result.returncode != 0:
    raise SystemExit("task-context migration retry failed after restoring expected source")
engine = create_engine(target_env["DATABASE_URL"], connect_args={"connect_timeout": 5})
try:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        source = connection.execute(
            text(
                "SELECT p.prosrc FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' AND p.proname='sro_validate_event_insert'"
            )
        ).scalar_one()
        actual_hash = hashlib.sha256(" ".join(source.split()).encode("utf-8")).hexdigest()
        if revision != "74b9d2e1c6a0":
            raise SystemExit("task-context migration retry did not reach exact head")
        if actual_hash == context_migration._PREVIOUS_EVENT_FUNCTION_SHA256:
            raise SystemExit("task-context migration retry left the prior guard function")
finally:
    engine.dispose()

print("migration_revision_preserved=passed")
print("task_context_migration_retry=passed")
