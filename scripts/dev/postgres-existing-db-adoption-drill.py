#!/usr/bin/env python3
"""Restore the configured loopback development DB into a disposable clone and rehearse adoption.

The source is opened read-only and is never stamped or migrated. The custom-format
dump and service credential file live in a private temporary directory and are
removed on exit. Output contains only aggregate pass/fail evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alembic.autogenerate.api import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.db.base import Base, engine
from app.db import models as _models  # Register the modeled schema.
from app.cli import compare_postgresql_enums, compare_postgresql_index_uniqueness
from app.cli import compare_postgresql_objects
from sro_robot.config import ROOT

BASELINE_REVISION = "bc518c7514d3"
EXPECTED_HEAD = "f58c2d41a693"
SOURCE_TABLES = ("user", "document", "task", "event")
EXPECTED_SOURCE_TABLES = set(SOURCE_TABLES)
SOURCE_COLUMNS = {
    "user": (
        "id", "login", "full_name", "password_hash", "sro_login", "is_active", "created_at",
    ),
    "document": (
        "id", "filename", "stored_path", "content_hash", "source", "uploaded_by_id", "created_at",
    ),
    "task": (
        "id", "document_id", "scenario", "profile", "status", "recognized", "values",
        "case_number", "assignee_id", "approved_by_id", "record_url", "error", "dry_run",
        "screenshots", "browser_decision", "created_at", "updated_at",
    ),
    "event": ("id", "task_id", "user_id", "kind", "message", "payload", "created_at"),
}


def _handle_shutdown_signal(signum: int, frame) -> None:
    """Turn normal termination signals into exceptions so `finally` cleanup runs."""
    raise KeyboardInterrupt


def _service_value(value: object) -> str:
    """Format one libpq service-file value without exposing it to argv/logs."""
    raw = str(value or "")
    if "\n" in raw or "\r" in raw:
        raise ValueError("PostgreSQL service values must be single-line")
    if any(character.isspace() or character in "#\\\"'" for character in raw):
        return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return raw


def _run(arguments: list[str], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return result.stdout


def _source_preflight() -> tuple[
    int,
    dict[str, tuple[int, str]],
    tuple[int, str],
]:
    url = engine.url
    if url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("source database host is not loopback")
    if engine.dialect.name != "postgresql":
        raise RuntimeError("source database is not PostgreSQL")

    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        revision = context.get_current_revision()
        tables = set(inspect(connection).get_table_names(schema="public")) - {"alembic_version"}
        if (
            revision is not None
            or inspect(connection).has_table("alembic_version", schema="public")
            or tables != EXPECTED_SOURCE_TABLES
        ):
            raise RuntimeError("source is not the expected unversioned application schema")
        if "task_lease" in tables:
            raise RuntimeError("source unexpectedly contains task lease state")

        metadata_differences = list(compare_metadata(context, Base.metadata))
        enum_differences = compare_postgresql_enums(connection)
        object_differences = compare_postgresql_objects(connection)
        index_differences = compare_postgresql_index_uniqueness(connection)
        expected_legacy_audit_drift = [
            "append-only event trigger function missing or ambiguous",
            "append-only event trigger set missing or ambiguous",
        ]
        if (
            len(metadata_differences) != 8
            or enum_differences
            or object_differences != expected_legacy_audit_drift
            or len(index_differences) != 1
            or "document.ix_document_content_hash" not in index_differences[0]
        ):
            raise RuntimeError("source schema drift differs from the reviewed legacy profile")

        hash_index = next(
            (
                item
                for item in inspect(connection).get_indexes("document", schema="public")
                if item.get("name") == "ix_document_content_hash"
            ),
            None,
        )
        if hash_index is None or hash_index.get("unique"):
            raise RuntimeError("source content-hash index differs from the expected legacy profile")
        duplicate_excess = connection.execute(text("""
            SELECT coalesce(sum(row_count - 1), 0)
            FROM (
                SELECT count(*) AS row_count
                FROM public.document
                GROUP BY content_hash
                HAVING count(*) > 1
            ) AS duplicate_hashes
        """)).scalar_one()
        if duplicate_excess:
            raise RuntimeError("source contains duplicate document hashes; adoption needs review")
        signature = _row_signature(connection)
        sequence_signature = _sequence_signature(connection)
    return int(duplicate_excess), signature, sequence_signature


def _row_signature(connection) -> dict[str, tuple[int, str]]:
    preparer = connection.dialect.identifier_preparer
    signature: dict[str, tuple[int, str]] = {}
    for table_name in SOURCE_TABLES:
        table = preparer.quote(table_name)
        digest = hashlib.sha256()
        row_count = 0
        pairs = ", ".join(
            f"'{column}', row_data.{preparer.quote(column)}"
            for column in SOURCE_COLUMNS[table_name]
        )
        statement = (
            f"SELECT jsonb_build_object({pairs})::text "
            f"FROM public.{table} AS row_data ORDER BY row_data.id"
        )
        rows = connection.execution_options(stream_results=True).exec_driver_sql(statement)
        for (row_json,) in rows:
            payload = str(row_json).encode("utf-8")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
            row_count += 1
        signature[table_name] = (row_count, digest.hexdigest())
    return signature


def _sequence_signature(connection) -> tuple[int, str]:
    """Hash sequence definitions and exact next-value state without printing names."""
    sequences = connection.execute(text("""
        SELECT relation.relname,
               seq.seqstart::text,
               seq.seqincrement::text,
               seq.seqmin::text,
               seq.seqmax::text,
               seq.seqcache::text,
               seq.seqcycle
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_sequence AS seq ON seq.seqrelid = relation.oid
        JOIN pg_depend AS dependency
          ON dependency.classid = 'pg_class'::regclass
         AND dependency.objid = relation.oid
         AND dependency.refclassid = 'pg_class'::regclass
         AND dependency.deptype IN ('a', 'i')
        JOIN pg_class AS owner_table ON owner_table.oid = dependency.refobjid
        WHERE namespace.nspname = 'public'
          AND relation.relkind = 'S'
          AND owner_table.relname = ANY(:source_tables)
        ORDER BY relation.relname
    """), {"source_tables": list(SOURCE_TABLES)}).all()
    preparer = connection.dialect.identifier_preparer
    digest = hashlib.sha256()
    for sequence in sequences:
        name, *definition = sequence
        quoted_name = preparer.quote(name)
        last_value, is_called = connection.exec_driver_sql(
            f"SELECT last_value::text, is_called FROM public.{quoted_name}"
        ).one()
        payload = json.dumps(
            [name, *definition, last_value, is_called],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return len(sequences), digest.hexdigest()


def _write_service_file(path: Path) -> None:
    url = engine.url
    fields = {
        "host": url.host,
        "port": url.port or 5432,
        "dbname": url.database,
        "user": url.username,
        "password": url.password,
        "connect_timeout": 5,
    }
    for option in ("sslmode", "sslrootcert", "sslcert", "sslkey"):
        if option in url.query:
            fields[option] = url.query[option]
    path.write_text(
        "[sro_source]\n" + "".join(f"{key}={_service_value(value)}\n" for key, value in fields.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _create_database_signature(url: str) -> dict[str, tuple[int, str]]:
    clone_engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    try:
        with clone_engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            return _row_signature(connection)
    finally:
        clone_engine.dispose()


def _create_sequence_signature(url: str) -> tuple[int, str]:
    clone_engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    try:
        with clone_engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            return _sequence_signature(connection)
    finally:
        clone_engine.dispose()


def main() -> int:
    if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
        raise RuntimeError("pg_dump and pg_restore must be installed locally")

    _, source_signature, source_sequence_signature = _source_preflight()
    container_name = f"sro-existing-adoption-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory(prefix="sro-existing-db-adoption-") as temporary:
        private_dir = Path(temporary)
        private_dir.chmod(0o700)
        service_file = private_dir / "pg_service.conf"
        dump_path = private_dir / "source.dump"
        _write_service_file(service_file)
        child_env = {**os.environ, "PGSERVICEFILE": str(service_file)}
        container_started = False
        try:
            _run([
                "pg_dump", "--no-owner", "--no-privileges", "--format=custom",
                f"--file={dump_path}", "service=sro_source",
            ], env=child_env)
            dump_path.chmod(0o600)
            # Set before starting Docker so an interrupt during `docker run` also
            # attempts to remove a container that may already have been created.
            container_started = True
            _run([
                "docker", "run", "-d", "--name", container_name,
                "-p", "127.0.0.1::5432", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea",
            ])
            ready = False
            for _ in range(30):
                probe = subprocess.run(
                    ["docker", "exec", container_name, "pg_isready", "-U", "postgres"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if probe.returncode == 0:
                    ready = True
                    break
                time.sleep(1)
            if not ready:
                raise RuntimeError("disposable PostgreSQL clone did not become ready")

            published = _run(["docker", "port", container_name, "5432/tcp"]).strip().splitlines()[0]
            host_port = published.rsplit(":", 1)[1]
            _run(["createdb", "--host=127.0.0.1", f"--port={host_port}", "--username=postgres", "restore_target"])
            _run([
                "pg_restore", "--host=127.0.0.1", f"--port={host_port}",
                "--username=postgres", "--dbname=restore_target", "--exit-on-error",
                "--no-owner", "--no-privileges", str(dump_path),
            ])

            clone_url = f"postgresql+psycopg://postgres@127.0.0.1:{host_port}/restore_target"
            restored_signature = _create_database_signature(clone_url)
            if source_signature != restored_signature:
                raise RuntimeError("restored clone rows differ from the source dump snapshot")
            restored_sequence_signature = _create_sequence_signature(clone_url)
            if source_sequence_signature != restored_sequence_signature:
                raise RuntimeError("restored clone sequence state differs from the source dump snapshot")

            clone_env = {
                **os.environ,
                "DATABASE_URL": clone_url,
                "APP_DEBUG": "1",
                "SRO_UPLOAD_DIR": str(private_dir / "uploads"),
            }
            stamp_code = (
                "from alembic import command; from alembic.config import Config; "
                "from sro_robot.config import ROOT; "
                f"command.stamp(Config(str(ROOT / 'alembic.ini')), '{BASELINE_REVISION}')"
            )
            _run([sys.executable, "-c", stamp_code], env=clone_env)
            _run([sys.executable, "-m", "app.cli", "init-db"], env=clone_env)
            _run([sys.executable, "-m", "app.cli", "schema-check"], env=clone_env)

            migrated_signature = _create_database_signature(clone_url)
            if source_signature != migrated_signature:
                raise RuntimeError("migration changed existing rows in the restored clone")
            migrated_sequence_signature = _create_sequence_signature(clone_url)
            if source_sequence_signature != migrated_sequence_signature:
                raise RuntimeError("migration changed sequence state in the restored clone")
            with create_engine(clone_url).connect() as connection:
                revision = MigrationContext.configure(connection).get_current_revision()
                if revision != EXPECTED_HEAD:
                    raise RuntimeError("clone migration did not reach expected head")
                unique_index = next(
                    (
                        item
                        for item in inspect(connection).get_indexes("document", schema="public")
                        if item.get("name") == "ix_document_content_hash"
                    ),
                    None,
                )
                if unique_index is None or not unique_index.get("unique"):
                    raise RuntimeError("clone content-hash index is not unique after migration")
                lease_rows = connection.execute(text("SELECT count(*) FROM public.task_lease")).scalar_one()
                if lease_rows:
                    raise RuntimeError("migration created unexpected lease rows")

            _, source_after, source_sequence_after = _source_preflight()
            if source_signature != source_after:
                raise RuntimeError("source rows changed during the read-only clone rehearsal")
            if source_sequence_signature != source_sequence_after:
                raise RuntimeError("source sequence state changed during the read-only clone rehearsal")

            print("source_read_only_preflight=passed")
            print("source_dump_restored_row_signature=matched")
            print("source_dump_restored_sequence_signature=matched")
            print("existing_rows_preserved_through_baseline_and_migrations=true")
            print("sequence_state_preserved_through_migrations=true")
            print(f"clone_revision={EXPECTED_HEAD}")
            print("clone_document_hash_index_unique=true")
            print("source_rows_unchanged=true")
            return 0
        finally:
            if container_started:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Existing-DB adoption rehearsal interrupted; disposable resources cleaned", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Existing-DB adoption rehearsal failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(2)
