#!/usr/bin/env python3
"""Rehearse an exact SRO release against a private, network-isolated DB clone.

Run only as root on the VPS after manually placing the reviewed git-archive tar
and Linux candidate venv on the VPS. This script never reads .env, stops writers,
sets maintenance, connects a candidate process to production, restores production,
or prints row/document/credential values. Source access is limited to pg_dump
and read-only catalog/row signatures through the live database container.

The clone is a single disposable PostgreSQL container on --network none, reached
from the host only through a private Unix socket directory. Migration and runtime
URLs use distinct synthetic clone roles and that socket; production TLS policy
allows absolute Unix-socket paths and therefore does not need to be weakened.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import uuid

PROD_CONTAINER = "deploy-db-1"
BACKUP_ROOT = Path("/var/backups/sro-robot")
EXPECTED_SOURCE_REVISION = "ea912b30c761"  # origin/main at the approved production commit
EXPECTED_TARGET_REVISION = "20261029_preview"  # exact reviewed develop candidate head
CURRENT_SCHEMA_REVISION = "20261021_reproc"  # production schema used for no-migration rehearsals
EXPECTED_REVISION_PAIRS = {
    "migration": (EXPECTED_SOURCE_REVISION, EXPECTED_TARGET_REVISION),
    "same-schema": (CURRENT_SCHEMA_REVISION, CURRENT_SCHEMA_REVISION),
}
RUNTIME_ROLE = "sro_rehearsal_runtime"
MIGRATION_ROLE = "sro_rehearsal_migrator"
IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
QUERY_TIMEOUT_SECONDS = 900
DUMP_TIMEOUT_SECONDS = 1800
MAX_DUMP_BYTES = 50 * 1024**3
MIN_DUMP_BYTES = 256 * 1024**2
FIXED_DISK_RESERVE_BYTES = 1024**3

STAGE = "arguments"
CLONE_NAME: str | None = None
TEMP: Path | None = None
PRESERVE_TEMP = False
CANDIDATE_COMMIT: str | None = None
CHECKPOINT_SHA256: str | None = None
FAILURE_WORKSPACE: Path | None = None
SUCCESS_SUMMARY: str | None = None
CLEANUP_ERROR: str | None = None


class RehearsalError(RuntimeError):
    pass


def run(args: list[str], *, stdin=None, stdout=None, env=None, cwd=None,
        timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    try:
        options = {
            "stdout": stdout if stdout is not None else subprocess.PIPE,
            "stderr": subprocess.DEVNULL, "env": env, "cwd": cwd,
            "timeout": timeout, "check": False,
        }
        if isinstance(stdin, (bytes, bytearray)):
            options["input"] = stdin
        elif stdin is not None:
            options["stdin"] = stdin
        result = subprocess.run(args, **options)
    except Exception as exc:
        raise RehearsalError(type(exc).__name__) from None
    if check and result.returncode:
        raise RehearsalError("CommandFailed")
    return result


def docker_sql_args(container: str, db: str, *, clone: bool) -> list[str]:
    if clone:
        return ["docker", "exec", "-i", container, "psql", "-X", "-Atq",
                "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", db]
    return ["docker", "exec", "-i", container, "sh", "-ec",
            'exec psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"',
            "psql", db]


def sql_capture(container: str, db: str, sql: str, *, clone: bool) -> str:
    if not clone:
        sql = (
            "SET default_transaction_read_only = on;\n"
            f"SET statement_timeout = '{QUERY_TIMEOUT_SECONDS}s';\n"
            "SET lock_timeout = '5s';\n" + sql
        )
    result = run(docker_sql_args(container, db, clone=clone), stdin=sql.encode(), timeout=300)
    return result.stdout.decode("utf-8").strip()


def sql_lines(container: str, db: str, sql: str, *, clone: bool):
    """Yield bounded COPY output internally; raw row JSON never reaches logs."""
    if not clone:
        sql = (
            "SET default_transaction_read_only = on;\n"
            f"SET statement_timeout = '{QUERY_TIMEOUT_SECONDS}s';\n"
            "SET lock_timeout = '5s';\n" + sql
        )
    proc = None
    selector = None
    try:
        proc = subprocess.Popen(
            docker_sql_args(container, db, clone=clone), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(sql.encode())
        proc.stdin.close()
        deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS + 30
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        pending = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise RehearsalError("SignatureQueryTimeout")
            chunk = os.read(proc.stdout.fileno(), 1024 * 1024)
            if not chunk:
                break
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    break
                yield bytes(pending[:newline]).rstrip(b"\r")
                del pending[: newline + 1]
        if pending:
            yield bytes(pending).rstrip(b"\r")
        proc.stdout.close()
        if proc.wait(timeout=5):
            raise RehearsalError("SignatureQueryFailed")
    except RehearsalError:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        raise
    except Exception as exc:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        raise RehearsalError(type(exc).__name__) from None
    finally:
        if selector is not None:
            selector.close()
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
            if proc.stdout is not None and not proc.stdout.closed:
                proc.stdout.close()


def source_identity() -> tuple[str, str, str]:
    result = run([
        "docker", "exec", PROD_CONTAINER, "sh", "-ec",
        'printf "%s\\n%s\\n" "${POSTGRES_DB:-postgres}" "${POSTGRES_USER:-postgres}"',
    ])
    values = result.stdout.decode("ascii").splitlines()
    if len(values) != 2:
        raise RehearsalError("SourceIdentityUnavailable")
    db, user = values
    if not IDENT.fullmatch(db) or not IDENT.fullmatch(user):
        raise RehearsalError("UnexpectedSourceIdentityFormat")
    image = run(["docker", "inspect", "--format", "{{.Image}}", PROD_CONTAINER]).stdout.decode().strip()
    health = run(["docker", "inspect", "--format", "{{.State.Health.Status}}", PROD_CONTAINER]).stdout.decode().strip()
    if not image or health != "healthy":
        raise RehearsalError("SourceDatabaseNotHealthy")
    return db, user, image


def source_revision(db: str) -> str:
    value = sql_capture(PROD_CONTAINER, db,
                        "SELECT version_num FROM public.alembic_version ORDER BY version_num;",
                        clone=False)
    if not value or "\n" in value:
        raise RehearsalError("UnexpectedSourceRevisionCardinality")
    return value


def preflight_storage(db: str) -> tuple[int, int, int]:
    """Bound checkpoint growth and require room for dump plus restored clone."""
    raw_size = sql_capture(
        PROD_CONTAINER, db,
        "SELECT pg_database_size(current_database())::bigint;", clone=False,
    )
    if not raw_size.isdecimal():
        raise RehearsalError("SourceDatabaseSizeUnavailable")
    database_bytes = int(raw_size)
    if database_bytes <= 0:
        raise RehearsalError("SourceDatabaseSizeInvalid")
    dump_limit = max(MIN_DUMP_BYTES, database_bytes * 2 + 64 * 1024**2)
    if dump_limit > MAX_DUMP_BYTES:
        raise RehearsalError("SourceDatabaseExceedsBoundedDumpLimit")

    docker_root = run([
        "docker", "info", "--format", "{{.DockerRootDir}}",
    ], timeout=30).stdout.decode("utf-8").strip()
    root_path = Path(docker_root)
    if not docker_root or not root_path.is_dir():
        raise RehearsalError("DockerStoragePathUnavailable")
    backup_free = shutil.disk_usage(BACKUP_ROOT).free
    docker_free = shutil.disk_usage(root_path).free
    available = min(backup_free, docker_free)
    required = dump_limit + int(database_bytes * 1.5) + FIXED_DISK_RESERVE_BYTES
    if available < required:
        raise RehearsalError("InsufficientRehearsalDiskBudget")
    return database_bytes, dump_limit, required


def run_to_file_bounded(args: list[str], output, *, max_bytes: int, timeout: int) -> int:
    """Stream a subprocess artifact to disk under a hard byte and time limit."""
    proc = None
    selector = None
    written = 0
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise RehearsalError("SourceDumpTimeout")
            chunk = os.read(proc.stdout.fileno(), 1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise RehearsalError("SourceDumpSizeLimitExceeded")
            output.write(chunk)
        output.flush()
        status = proc.wait(timeout=5)
        if status:
            raise RehearsalError("SourceDumpFailed")
        return written
    except RehearsalError:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        raise
    except Exception as exc:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        raise RehearsalError(type(exc).__name__) from None
    finally:
        if selector is not None:
            selector.close()
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()


def identifiers(value: list[str]) -> list[str]:
    if not value or any(not IDENT.fullmatch(item) for item in value):
        raise RehearsalError("UnsupportedSchemaIdentifier")
    return value


def quote_ident(value: str) -> str:
    if not IDENT.fullmatch(value):
        raise RehearsalError("UnsupportedSchemaIdentifier")
    return '"' + value + '"'


def table_columns(container: str, db: str, *, clone: bool) -> dict[str, list[str]]:
    raw = sql_capture(container, db, """
        SELECT coalesce(json_agg(table_name ORDER BY table_name), '[]'::json)::text
        FROM information_schema.tables
        WHERE table_schema='public' AND table_type='BASE TABLE';
    """, clone=clone)
    names = identifiers(json.loads(raw))
    output: dict[str, list[str]] = {}
    for name in names:
        cols = sql_capture(container, db, f"""
            SELECT coalesce(json_agg(column_name ORDER BY ordinal_position), '[]'::json)::text
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='{name}';
        """, clone=clone)
        output[name] = identifiers(json.loads(cols))
    return output


def row_table_signature(container: str, db: str, table: str, columns: list[str], *, clone: bool) -> tuple[int, str]:
    if table == "alembic_version":
        raise RehearsalError("RevisionTableIsNotApplicationData")
    quoted = quote_ident(table)
    fields = ", ".join(
        "'" + col + "', row_data." + quote_ident(col) for col in columns
    )
    projection = f"jsonb_build_object({fields})::text"
    query = f"COPY (SELECT {projection} FROM public.{quoted} AS row_data) TO STDOUT;"
    sum_accumulator = 0
    xor_accumulator = 0
    count = 0
    for line in sql_lines(container, db, query, clone=clone):
        row_digest = hashlib.sha256(line).digest()
        row_value = int.from_bytes(row_digest, "big")
        sum_accumulator = (sum_accumulator + row_value) % (1 << 256)
        xor_accumulator ^= row_value
        count += 1
    digest = hashlib.sha256(
        count.to_bytes(8, "big")
        + sum_accumulator.to_bytes(32, "big")
        + xor_accumulator.to_bytes(32, "big")
    )
    return count, digest.hexdigest()


def all_row_signatures(container: str, db: str, schema: dict[str, list[str]], *, clone: bool):
    return {
        table: row_table_signature(container, db, table, cols, clone=clone)
        for table, cols in sorted(schema.items()) if table != "alembic_version"
    }


def signature_summary(signatures: dict[str, tuple[int, str]]) -> tuple[int, str]:
    digest = hashlib.sha256()
    rows = 0
    for table, (count, table_hash) in sorted(signatures.items()):
        rows += count
        digest.update(table.encode() + b"\0" + str(count).encode() + b"\0" + table_hash.encode() + b"\n")
    return rows, digest.hexdigest()


def sequence_snapshot(container: str, db: str, *, clone: bool):
    raw = sql_capture(container, db, """
        SELECT coalesce(json_agg(seq.relname ORDER BY seq.relname), '[]'::json)::text
        FROM pg_class AS seq
        JOIN pg_namespace AS ns ON ns.oid=seq.relnamespace
        WHERE ns.nspname='public' AND seq.relkind='S';
    """, clone=clone)
    names = identifiers(json.loads(raw)) if json.loads(raw) else []
    output = {}
    for name in names:
        value = sql_capture(container, db,
                            f"SELECT last_value::text || ':' || is_called::text FROM public.{quote_ident(name)};",
                            clone=clone)
        output[name] = value
    return output


def aggregate_sequence_signature(values: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(values.items()):
        digest.update(name.encode() + b"\0" + value.encode() + b"\n")
    return digest.hexdigest()


def index_snapshot(container: str, db: str, *, clone: bool) -> dict[str, tuple[str, bool, bool, bool]]:
    """Return exact public index definitions and readiness flags."""
    raw = sql_capture(container, db, """
        SELECT coalesce(json_agg(json_build_array(
            index_rel.relname, table_rel.relname, pg_get_indexdef(index_rel.oid),
            index_info.indisunique, index_info.indisvalid, index_info.indisready
        ) ORDER BY index_rel.relname), '[]'::json)::text
        FROM pg_index AS index_info
        JOIN pg_class AS index_rel ON index_rel.oid=index_info.indexrelid
        JOIN pg_class AS table_rel ON table_rel.oid=index_info.indrelid
        JOIN pg_namespace AS schema_ns ON schema_ns.oid=index_rel.relnamespace
        WHERE schema_ns.nspname='public';
    """, clone=clone)
    values = json.loads(raw)
    return {
        str(name): (str(definition), bool(unique), bool(valid), bool(ready))
        for name, _table, definition, unique, valid, ready in values
    }


def validate_revision_pair(mode: str, source_revision: str | None,
                           target_revision: str | None) -> None:
    if mode not in EXPECTED_REVISION_PAIRS:
        raise RehearsalError("InvalidRehearsalMode")
    if not source_revision or not target_revision:
        raise RehearsalError("ExpectedRevisionPinsRequired")
    if EXPECTED_REVISION_PAIRS[mode] != (source_revision, target_revision):
        raise RehearsalError("UnexpectedMigrationPin")


def assert_same_schema_state(source_schema, candidate_schema, source_rows,
                             candidate_rows, source_sequences, candidate_sequences,
                             source_indexes, candidate_indexes) -> None:
    if candidate_schema != source_schema:
        raise RehearsalError("SameSchemaTableOrColumnMismatch")
    if candidate_indexes != source_indexes:
        raise RehearsalError("SameSchemaIndexMismatch")
    if candidate_rows != source_rows:
        raise RehearsalError("SameSchemaRowSignatureMismatch")
    if candidate_sequences != source_sequences:
        raise RehearsalError("SameSchemaSequenceSignatureMismatch")


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise RehearsalError("UnsafeArchivePath")
            if member.issym() or member.islnk():
                link = PurePosixPath(member.linkname)
                target = link if link.is_absolute() else name.parent / link
                normalized = PurePosixPath(os.path.normpath(str(target)))
                if normalized.is_absolute() or ".." in normalized.parts:
                    raise RehearsalError("UnsafeArchiveLink")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise RehearsalError("UnsupportedArchiveMember")
        tar.extractall(destination, members=members)


def candidate_root_from_archive(archive: Path, archive_sha: str, tmp: Path) -> Path:
    if archive.is_symlink() or not archive.is_file():
        raise RehearsalError("CandidateArchiveUnavailable")
    metadata = archive.stat()
    if metadata.st_uid != 0 or metadata.st_mode & 0o077:
        raise RehearsalError("CandidateArchivePermissionsUnsafe")
    if hash_file(archive) != archive_sha:
        raise RehearsalError("CandidateArchiveHashMismatch")
    root = tmp / "candidate-source"
    root.mkdir(mode=0o700)
    safe_extract(archive, root)
    if (root / ".env").exists():
        raise RehearsalError("PrivateEnvMustNotBeInCandidateArchive")
    if not (root / "app" / "cli.py").is_file() or not (root / "migrations" / "env.py").is_file():
        raise RehearsalError("CandidateArchiveMissingRequiredSource")
    return root


def check_venv(python: Path, candidate_root: Path) -> None:
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RehearsalError("CandidatePythonUnavailable")
    requirements = candidate_root / "requirements.txt"
    # venv/bin/python is commonly a symlink to the base interpreter. Resolve
    # sys.prefix from the invoked interpreter instead of inferring it from the
    # executable's resolved filesystem location.
    result = run([
        str(python), "-c",
        "import os,sys; print(os.path.abspath(sys.prefix)); print(os.path.abspath(sys.base_prefix))",
    ], timeout=30)
    prefixes = result.stdout.decode("utf-8").splitlines()
    if len(prefixes) != 2 or not prefixes[0] or not prefixes[1] or prefixes[0] == prefixes[1]:
        raise RehearsalError("CandidateInterpreterIsNotVenv")
    prefix = Path(prefixes[0])
    expected_python = prefix / "bin" / "python"
    if python.parent.resolve() != expected_python.parent.resolve() or not expected_python.is_file():
        raise RehearsalError("CandidatePythonOutsideVenv")
    marker = prefix / ".requirements-sha256"
    if not requirements.is_file() or not marker.is_file():
        raise RehearsalError("CandidateEnvironmentMarkerMissing")
    recorded = marker.read_text(encoding="ascii").strip()
    if recorded != hash_file(requirements):
        raise RehearsalError("CandidateEnvironmentRequirementsMismatch")
    # pip check output is intentionally suppressed by run(); only success or
    # a bounded error code can escape this helper.
    run([str(python), "-m", "pip", "check"], timeout=120)


def read_revision(container: str, db: str, *, clone: bool) -> str:
    value = sql_capture(container, db,
                        "SELECT version_num FROM public.alembic_version ORDER BY version_num;",
                        clone=clone)
    if not value or "\n" in value:
        raise RehearsalError("UnexpectedAlembicRevisionCardinality")
    return value


def clone_urls(db: str, socket_dir: Path) -> tuple[str, str]:
    # libpq expects the socket directory as host and appends .s.PGSQL.<port>.
    query = urllib.parse.urlencode({"host": str(socket_dir), "port": "5432"})
    database = urllib.parse.quote(db, safe="")
    runtime = f"postgresql+psycopg://{RUNTIME_ROLE}@/{database}?{query}"
    migration = f"postgresql+psycopg://{MIGRATION_ROLE}@/{database}?{query}"
    return runtime, migration


def cli_env(tmp: Path, runtime_url: str, migration_url: str) -> dict[str, str]:
    empty_env = tmp / "empty-app.env"
    if not empty_env.exists():
        fd = os.open(empty_env, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    upload_dir = tmp / "empty-upload-store"
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "APP_DEBUG": "0",
        "APP_SECRET_KEY": "rehearsal-only-" + uuid.uuid4().hex + uuid.uuid4().hex,
        "DATABASE_URL": runtime_url,
        "MIGRATION_DATABASE_URL": migration_url,
        "REDIS_URL": "unix:///run/sro-robot-migration-rehearsal/no-redis.sock",
        "SRO_ROBOT_ENV_FILE": str(empty_env),
        "SRO_UPLOAD_DIR": str(upload_dir),
        "PYTHONUNBUFFERED": "1",
    }


def run_cli(python: Path, source: Path, tmp: Path, env: dict[str, str], command: str) -> None:
    run([str(python), "-m", "app.cli", command], env=env, cwd=source, timeout=300)


def initialize_candidate(mode: str, python: Path, source: Path, tmp: Path,
                        env: dict[str, str]) -> None:
    if mode not in EXPECTED_REVISION_PAIRS:
        raise RehearsalError("InvalidRehearsalMode")
    # Activation calls init-db in both cases. For same-schema this must prove
    # that the candidate's normal startup migration path is a no-op.
    run_cli(python, source, tmp, env, "init-db")


def create_clone(image: str, db: str, dump: Path, socket_dir: Path) -> str:
    global CLONE_NAME
    name = "sro-migration-rehearsal-" + uuid.uuid4().hex[:12]
    # Register before docker run: even a partial/failed startup must be removed
    # by the outer finally cleanup.
    CLONE_NAME = name
    run([
        "docker", "run", "--detach", "--name", name, "--network", "none",
        "--mount", f"type=bind,src={socket_dir},dst=/var/run/postgresql",
        "--env", "POSTGRES_USER=postgres", "--env", "POSTGRES_DB=postgres",
        "--env", "POSTGRES_HOST_AUTH_METHOD=trust", image,
        "-c", "unix_socket_directories=/var/run/postgresql",
        "-c", "unix_socket_permissions=0777",
        "-c", "listen_addresses=",
    ], timeout=120)
    for _ in range(60):
        ready = run([
            "docker", "exec", name, "pg_isready", "-h", "/var/run/postgresql",
            "-p", "5432", "-U", "postgres", "-d", "postgres",
        ], timeout=5, check=False)
        if ready.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RehearsalError("CloneDatabaseDidNotStart")
    with dump.open("rb") as source:
        run([
            "docker", "exec", "-i", name, "pg_restore", "--create", "--no-owner",
            "--no-privileges", "--exit-on-error", "-U", "postgres", "-d", "postgres",
        ], stdin=source, timeout=1800)
    return name


def configure_clone_roles(clone: str, db: str) -> None:
    qdb = quote_ident(db)
    roles = sql_capture(clone, "postgres", f"""
      SELECT coalesce(bool_and(rolcanlogin AND NOT rolsuper AND NOT rolcreaterole
        AND NOT rolcreatedb AND NOT rolreplication AND NOT rolbypassrls), false)::text
      FROM pg_roles WHERE rolname IN ('{RUNTIME_ROLE}', '{MIGRATION_ROLE}');
    """, clone=True)
    role_count = int(sql_capture(clone, "postgres", f"""
      SELECT count(*) FROM pg_roles WHERE rolname IN ('{RUNTIME_ROLE}', '{MIGRATION_ROLE}');
    """, clone=True))
    if role_count == 0:
        sql_capture(clone, "postgres", f"""
          CREATE ROLE {RUNTIME_ROLE} LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;
          CREATE ROLE {MIGRATION_ROLE} LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;
        """, clone=True)
    elif role_count != 2 or roles != "true":
        raise RehearsalError("DisposableRoleInvariantFailed")
    sql_capture(clone, "postgres", f"ALTER DATABASE {qdb} OWNER TO {MIGRATION_ROLE};", clone=True)
    ownership_and_grants = f"""
      DO $rehearsal$
      DECLARE object_record record; object_kind text;
              routine_record record; routine_kind text;
      BEGIN
        FOR object_record IN
          SELECT relation.relkind, namespace.nspname, relation.relname
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace
          WHERE namespace.nspname='public' AND relation.relkind IN ('r','p','v','m','S')
            AND (relation.relkind <> 'S' OR NOT EXISTS (
              SELECT 1 FROM pg_depend AS dependency
              WHERE dependency.classid='pg_class'::regclass
                AND dependency.objid=relation.oid
                AND dependency.refclassid='pg_class'::regclass
                AND dependency.deptype IN ('a','i')
            ))
          ORDER BY relation.relkind, relation.relname
        LOOP
          object_kind := CASE object_record.relkind
            WHEN 'S' THEN 'SEQUENCE'
            WHEN 'v' THEN 'VIEW'
            WHEN 'm' THEN 'MATERIALIZED VIEW'
            ELSE 'TABLE'
          END;
          EXECUTE format('ALTER %s %I.%I OWNER TO %I', object_kind,
            object_record.nspname, object_record.relname, '{MIGRATION_ROLE}');
        END LOOP;
        FOR routine_record IN
          SELECT namespace.nspname, routine.proname,
                 pg_get_function_identity_arguments(routine.oid) AS identity_arguments,
                 routine.prokind
          FROM pg_proc AS routine
          JOIN pg_namespace AS namespace ON namespace.oid=routine.pronamespace
          WHERE namespace.nspname='public' AND routine.prokind IN ('f','p')
          ORDER BY routine.prokind, routine.proname, identity_arguments
        LOOP
          routine_kind := CASE routine_record.prokind
            WHEN 'p' THEN 'PROCEDURE'
            ELSE 'FUNCTION'
          END;
          EXECUTE format('ALTER %s %I.%I(%s) OWNER TO %I', routine_kind,
            routine_record.nspname, routine_record.proname,
            routine_record.identity_arguments, '{MIGRATION_ROLE}');
        END LOOP;
      END
      $rehearsal$;
      ALTER SCHEMA public OWNER TO {MIGRATION_ROLE};
      GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {MIGRATION_ROLE};
      GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {MIGRATION_ROLE};
      GRANT CONNECT ON DATABASE {qdb} TO {RUNTIME_ROLE};
      GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE};
      GRANT SELECT ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE};
      GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE};
    """
    sql_capture(clone, db, ownership_and_grants, clone=True)


def candidate_index_invariants(clone: str, db: str) -> None:
    index_flags = json.loads(sql_capture(clone, db, """
      SELECT coalesce(json_object_agg(index_rel.relname, index_info.indisunique), '{}'::json)::text
      FROM pg_index AS index_info
      JOIN pg_class AS index_rel ON index_rel.oid=index_info.indexrelid
      JOIN pg_namespace AS schema_ns ON schema_ns.oid=index_rel.relnamespace
      WHERE schema_ns.nspname='public';
    """, clone=True))
    expected_unique = {
        "uq_debtor_card_draft_kind_inn",
        "uq_debtor_card_draft_legal_entity_ogrn",
        "uq_debtor_card_draft_sole_proprietor_ogrnip",
        "uq_upload_transport_batch_reprocess_key",
    }
    expected_nonunique = {"ix_assignment_draft_debtor_card_id_id"}
    if any(index_flags.get(name) is not True for name in expected_unique):
        raise RehearsalError("CandidateUniqueIndexInvariantFailed")
    if any(index_flags.get(name) is not False for name in expected_nonunique):
        raise RehearsalError("CandidateIndexInvariantFailed")


def target_invariants(clone: str, db: str) -> tuple[int, int]:
    required_tables = [
        "assignment_create_request", "debtor_card_create_request", "ocr_retry_run",
        "kad_source_run", "kad_import_request_receipt", "case_package",
        "case_package_task_event", "case_package_assignment_event",
        "upload_transport_batch", "upload_transport_batch_item",
        "upload_transport_chunk_receipt", "debtor_card_draft_revision",
    ]
    existing = set(identifiers(json.loads(sql_capture(
        clone, db,
        "SELECT coalesce(json_agg(table_name ORDER BY table_name), '[]'::json)::text "
        "FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';",
        clone=True,
    ))))
    if not set(required_tables).issubset(existing):
        raise RehearsalError("CandidateExpectedTablesMissing")

    empty_tables = [name for name in required_tables if name != "debtor_card_draft_revision"]
    for table in empty_tables:
        count = int(sql_capture(clone, db, f"SELECT count(*) FROM public.{quote_ident(table)};", clone=True))
        if count:
            raise RehearsalError("UnexpectedNewWorkflowRows")

    card_count = int(sql_capture(clone, db, "SELECT count(*) FROM public.debtor_card_draft;", clone=True))
    revision_count = int(sql_capture(clone, db, "SELECT count(*) FROM public.debtor_card_draft_revision;", clone=True))
    mismatch = int(sql_capture(clone, db, """
      SELECT count(*) FROM public.debtor_card_draft AS card
      LEFT JOIN public.debtor_card_draft_revision AS revision
        ON revision.debtor_card_id=card.id AND revision.revision=card.version
      WHERE revision.id IS NULL
         OR revision.changed_by_id IS DISTINCT FROM card.updated_by_id
         OR revision.snapshot IS DISTINCT FROM jsonb_build_object(
              'display_name', card.display_name,
              'inn', card.inn,
              'values', card.values,
              'preparation_state', 'reviewed',
              'origin', 'migration_baseline'
            );
    """, clone=True))
    if revision_count != card_count or mismatch:
        raise RehearsalError("CardBaselineRevisionProjectionMismatch")

    # Columns span different tables; check them separately and compare the
    # registry-ID backfill to the migration's exact normalization expression.
    preparation_bad = int(sql_capture(clone, db, """
      SELECT count(*) FROM public.debtor_card_draft
      WHERE preparation_state IS DISTINCT FROM 'reviewed';
    """, clone=True))
    source_task_bad = int(sql_capture(clone, db, """
      SELECT count(*) FROM public.assignment_draft WHERE source_task_id IS NOT NULL;
    """, clone=True))
    reprocess_bad = int(sql_capture(clone, db, """
      SELECT count(*) FROM public.upload_transport_batch
      WHERE reprocess_parent_id IS NOT NULL
         OR reprocess_parent_manifest_sha256 IS NOT NULL
         OR reprocess_request_key_sha256 IS NOT NULL;
    """, clone=True))
    registry_bad = int(sql_capture(clone, db, """
      SELECT count(*) FROM public.debtor_card_draft
      WHERE ogrn IS DISTINCT FROM CASE
          WHEN entity_kind = 'legal_entity' THEN NULLIF(CASE
            WHEN values -> 'ogrn' ->> 'value' ~ '^[0-9 -]+$'
            THEN regexp_replace(values -> 'ogrn' ->> 'value', '[ -]', '', 'g')
            ELSE values -> 'ogrn' ->> 'value' END, '') END
         OR ogrnip IS DISTINCT FROM CASE
          WHEN entity_kind = 'sole_proprietor' THEN NULLIF(CASE
            WHEN values -> 'ogrnip' ->> 'value' ~ '^[0-9 -]+$'
            THEN regexp_replace(values -> 'ogrnip' ->> 'value', '[ -]', '', 'g')
            ELSE values -> 'ogrnip' ->> 'value' END, '') END;
    """, clone=True))
    if preparation_bad or source_task_bad or reprocess_bad or registry_bad:
        raise RehearsalError("CandidateBackfillInvariantFailed")

    candidate_index_invariants(clone, db)
    return card_count, revision_count


def validate_candidate_mode(mode: str, clone: str, db: str, source_schema,
                            baseline_rows, baseline_sequences, baseline_indexes):
    candidate_schema = table_columns(clone, db, clone=True)
    if mode == "same-schema":
        candidate_rows = all_row_signatures(clone, db, candidate_schema, clone=True)
        candidate_sequences = sequence_snapshot(clone, db, clone=True)
        candidate_indexes = index_snapshot(clone, db, clone=True)
        assert_same_schema_state(
            source_schema, candidate_schema, baseline_rows, candidate_rows,
            baseline_sequences, candidate_sequences, baseline_indexes, candidate_indexes,
        )
        candidate_index_invariants(clone, db)
        return None, None
    if mode != "migration":
        raise RehearsalError("InvalidRehearsalMode")

    for table, columns in source_schema.items():
        if table not in candidate_schema or not set(columns).issubset(candidate_schema[table]):
            raise RehearsalError("ExistingTableOrColumnMissingAfterMigration")
    candidate_rows = all_row_signatures(clone, db, source_schema, clone=True)
    if candidate_rows != baseline_rows:
        raise RehearsalError("ExistingRowProjectionChangedByMigration")
    old_sequence_names = set(baseline_sequences)
    post_sequences = sequence_snapshot(clone, db, clone=True)
    if any(post_sequences.get(name) != baseline_sequences[name] for name in old_sequence_names):
        raise RehearsalError("ExistingSequenceStateChangedByMigration")
    return target_invariants(clone, db)


def restore_clone(dump: Path, clone: str) -> None:
    with dump.open("rb") as source:
        run([
            "docker", "exec", "-i", clone, "pg_restore", "--clean", "--if-exists",
            "--create", "--no-owner", "--no-privileges", "--exit-on-error",
            "-U", "postgres", "-d", "postgres",
        ], stdin=source, timeout=1800)


def main() -> int:
    global STAGE, CLONE_NAME, TEMP, CANDIDATE_COMMIT, CHECKPOINT_SHA256
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--old-release", required=True, type=Path,
                        help="current old production release; used only for clone schema-check after rollback")
    parser.add_argument("--old-commit", required=True)
    parser.add_argument("--mode", choices=tuple(EXPECTED_REVISION_PAIRS), required=True,
                        help="migration validates the approved upgrade pair; same-schema validates the current no-migration pair")
    parser.add_argument("--expected-source-revision", required=True)
    parser.add_argument("--expected-target-revision", required=True)
    args = parser.parse_args()
    CANDIDATE_COMMIT = args.candidate_commit
    if os.geteuid() != 0:
        raise RehearsalError("MustRunAsRoot")
    if not COMMIT.fullmatch(args.candidate_commit) or not COMMIT.fullmatch(args.old_commit):
        raise RehearsalError("InvalidCommitPin")
    if not SHA256.fullmatch(args.archive_sha256):
        raise RehearsalError("InvalidArchiveHashPin")
    validate_revision_pair(args.mode, args.expected_source_revision, args.expected_target_revision)

    STAGE = "private_temp"
    if (not BACKUP_ROOT.is_dir() or BACKUP_ROOT.is_symlink()
            or BACKUP_ROOT.stat().st_uid != 0 or (BACKUP_ROOT.stat().st_mode & 0o077)):
        raise RehearsalError("PrivateBackupRootUnavailable")
    TEMP = Path(tempfile.mkdtemp(prefix=".migration-rehearsal-", dir=BACKUP_ROOT))
    os.chmod(TEMP, 0o700)
    socket_dir = TEMP / "socket"
    socket_dir.mkdir(mode=0o777)
    os.chmod(socket_dir, 0o777)

    STAGE = "candidate_source"
    candidate_root = candidate_root_from_archive(args.archive, args.archive_sha256, TEMP)
    check_venv(args.candidate_python, candidate_root)
    old_release = args.old_release.resolve(strict=True)
    if not old_release.is_dir() or not (old_release / ".release-commit").is_file():
        raise RehearsalError("OldReleaseUnavailable")
    if (old_release / ".release-commit").read_text(encoding="ascii").strip() != args.old_commit:
        raise RehearsalError("OldReleaseCommitMismatch")
    old_python = old_release / ".venv" / "bin" / "python"
    if not old_python.is_file() or not os.access(old_python, os.X_OK):
        raise RehearsalError("OldReleasePythonUnavailable")

    STAGE = "source_preflight"
    db, _db_user, image = source_identity()
    source_rev = source_revision(db)
    if source_rev != args.expected_source_revision:
        raise RehearsalError("ProductionSchemaRevisionMismatch")

    STAGE = "source_disk_preflight"
    database_bytes, dump_limit, required_free = preflight_storage(db)

    STAGE = "source_dump"
    dump = TEMP / "source-checkpoint.dump"
    fd = os.open(dump, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as output:
        dump_bytes = run_to_file_bounded([
            "docker", "exec", PROD_CONTAINER, "sh", "-ec",
            'exec pg_dump --format=custom --create --no-owner --no-privileges '
            '--lock-wait-timeout=5s -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
        ], output, max_bytes=dump_limit, timeout=DUMP_TIMEOUT_SECONDS)
        os.fsync(output.fileno())
    if not dump_bytes:
        raise RehearsalError("SourceDumpEmpty")
    dump_hash = hash_file(dump)
    CHECKPOINT_SHA256 = dump_hash

    STAGE = "clone_restore"
    CLONE_NAME = create_clone(image, db, dump, socket_dir)
    clone_rev = read_revision(CLONE_NAME, db, clone=True)
    if clone_rev != source_rev:
        raise RehearsalError("RestoredCloneRevisionMismatch")
    source_schema = table_columns(PROD_CONTAINER, db, clone=False)
    clone_schema = table_columns(CLONE_NAME, db, clone=True)
    if source_schema != clone_schema:
        raise RehearsalError("RestoredCloneSchemaMismatch")
    baseline_rows = all_row_signatures(CLONE_NAME, db, clone_schema, clone=True)
    baseline_sequences = sequence_snapshot(CLONE_NAME, db, clone=True)
    baseline_indexes = index_snapshot(CLONE_NAME, db, clone=True)

    STAGE = "source_signature_readback"
    source_rows = all_row_signatures(PROD_CONTAINER, db, source_schema, clone=False)
    source_sequences = sequence_snapshot(PROD_CONTAINER, db, clone=False)
    source_indexes = index_snapshot(PROD_CONTAINER, db, clone=False)
    if (source_rows != baseline_rows or source_sequences != baseline_sequences
            or source_indexes != baseline_indexes):
        raise RehearsalError("DumpDoesNotMatchReadOnlySourceSignature")

    STAGE = "clone_roles"
    configure_clone_roles(CLONE_NAME, db)
    runtime_url, migration_url = clone_urls(db, socket_dir)
    env = cli_env(TEMP, runtime_url, migration_url)

    STAGE = "candidate_migration" if args.mode == "migration" else "candidate_same_schema_init_db"
    initialize_candidate(args.mode, args.candidate_python, candidate_root, TEMP, env)
    if read_revision(CLONE_NAME, db, clone=True) != args.expected_target_revision:
        raise RehearsalError("CandidateDidNotReachPinnedRevision")

    STAGE = "candidate_schema_check"
    run_cli(args.candidate_python, candidate_root, TEMP, env, "schema-check")
    card_count, revision_count = validate_candidate_mode(
        args.mode, CLONE_NAME, db, source_schema, baseline_rows,
        baseline_sequences, baseline_indexes,
    )

    STAGE = "source_unchanged_readback"
    if source_revision(db) != source_rev:
        raise RehearsalError("SourceRevisionChangedDuringRehearsal")
    source_rows_after = all_row_signatures(PROD_CONTAINER, db, source_schema, clone=False)
    source_sequences_after = sequence_snapshot(PROD_CONTAINER, db, clone=False)
    source_indexes_after = index_snapshot(PROD_CONTAINER, db, clone=False)
    if (source_rows_after != source_rows or source_sequences_after != source_sequences
            or source_indexes_after != source_indexes):
        raise RehearsalError("SourceChangedDuringRehearsal")

    STAGE = "clone_rollback_restore"
    restore_clone(dump, CLONE_NAME)
    if read_revision(CLONE_NAME, db, clone=True) != source_rev:
        raise RehearsalError("RollbackRevisionMismatch")
    rolled_schema = table_columns(CLONE_NAME, db, clone=True)
    if rolled_schema != source_schema:
        raise RehearsalError("RollbackSchemaMismatch")
    rolled_rows = all_row_signatures(CLONE_NAME, db, rolled_schema, clone=True)
    rolled_sequences = sequence_snapshot(CLONE_NAME, db, clone=True)
    rolled_indexes = index_snapshot(CLONE_NAME, db, clone=True)
    if (rolled_rows != baseline_rows or rolled_sequences != baseline_sequences
            or rolled_indexes != baseline_indexes):
        raise RehearsalError("RollbackDataSignatureMismatch")

    STAGE = "rollback_old_release_schema_check"
    configure_clone_roles(CLONE_NAME, db)
    rollback_runtime_url, rollback_migration_url = clone_urls(db, socket_dir)
    rollback_env = cli_env(TEMP, rollback_runtime_url, rollback_migration_url)
    run_cli(old_python, old_release, TEMP, rollback_env, "schema-check")

    rows_after, hash_after = signature_summary(rolled_rows)
    global SUCCESS_SUMMARY
    SUCCESS_SUMMARY = (
        "rehearsal=passed"
        f" candidate={args.candidate_commit}"
        f" source_revision={source_rev} target_revision={args.expected_target_revision}"
        f" old_tables_preserved={sum(table != 'alembic_version' for table in source_schema)}"
        f" old_rows_preserved={rows_after}"
        f" old_row_projection_sha256={hash_after}"
        f" old_sequences_preserved={len(baseline_sequences)}"
        f" source_unchanged=true"
        f" candidate_schema_check=passed"
        + (f" baseline_card_revisions={revision_count}/{card_count}"
           if revision_count is not None else " same_schema_exact_signatures=passed")
        + " rollback_restore=passed rollback_old_schema_check=passed"
        f" source_database_bytes={database_bytes}"
        f" dump_bytes={dump_bytes} dump_limit_bytes={dump_limit}"
        f" minimum_free_bytes={required_free}"
        f" checkpoint_sha256={dump_hash}"
    )
    return 0


def cleanup() -> None:
    global CLONE_NAME, TEMP, PRESERVE_TEMP, CLEANUP_ERROR
    errors = []
    if CLONE_NAME:
        name = CLONE_NAME
        try:
            subprocess.run(["docker", "rm", "-f", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=60)
            readback = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=30,
            )
            if readback.returncode or name in readback.stdout.decode("utf-8").splitlines():
                errors.append("CloneRemovalUnverified")
            else:
                CLONE_NAME = None
        except Exception:
            errors.append("CloneRemovalUnverified")
    if TEMP and TEMP.exists():
        if PRESERVE_TEMP or errors:
            os.chmod(TEMP, 0o700)
            PRESERVE_TEMP = True
        else:
            try:
                shutil.rmtree(TEMP)
                if TEMP.exists():
                    errors.append("WorkspaceRemovalUnverified")
                else:
                    TEMP = None
            except Exception:
                errors.append("WorkspaceRemovalUnverified")
    if errors:
        CLEANUP_ERROR = errors[0]
        raise RehearsalError(errors[0])


def preserve_failure(exc: Exception) -> None:
    """Retain private diagnostics after an incomplete run; never include data."""
    global PRESERVE_TEMP, FAILURE_WORKSPACE
    if not TEMP or not TEMP.exists():
        return
    os.chmod(TEMP, 0o700)
    error_name = exc.args[0] if isinstance(exc, RehearsalError) and exc.args else type(exc).__name__
    report = {
        "status": "failed",
        "stage": STAGE,
        "error": str(error_name)[:100],
        "candidate_commit": CANDIDATE_COMMIT,
        "checkpoint_sha256": CHECKPOINT_SHA256,
        "cleanup_error": CLEANUP_ERROR,
    }
    report_path = TEMP / "failure.json"
    if report_path.exists():
        PRESERVE_TEMP = True
        FAILURE_WORKSPACE = TEMP
        return
    fd = os.open(report_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(report, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    PRESERVE_TEMP = True
    FAILURE_WORKSPACE = TEMP


def signal_to_failure(signum, _frame):
    raise RehearsalError("Interrupted")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal_to_failure)
    signal.signal(signal.SIGINT, signal_to_failure)
    exit_code = 0
    failure_name = None
    try:
        exit_code = main()
    except Exception as exc:
        error_name = exc.args[0] if isinstance(exc, RehearsalError) and exc.args else type(exc).__name__
        failure_name = error_name
        if TEMP and TEMP.exists():
            PRESERVE_TEMP = True
        exit_code = 1
    try:
        cleanup()
    except Exception as exc:
        error_name = exc.args[0] if isinstance(exc, RehearsalError) and exc.args else type(exc).__name__
        failure_name = failure_name or error_name
        exit_code = 1
    if exit_code:
        try:
            preserve_failure(RehearsalError(failure_name or "RehearsalFailed"))
        except Exception:
            pass
        print(f"rehearsal=failed stage={STAGE} error={failure_name or 'RehearsalFailed'}", file=sys.stderr)
    elif SUCCESS_SUMMARY is not None:
        print(SUCCESS_SUMMARY)
    if FAILURE_WORKSPACE is not None:
        # cleanup intentionally leaves retained failure artifacts in place.
        print(f"rehearsal_failure_workspace={FAILURE_WORKSPACE}", file=sys.stderr)
    raise SystemExit(exit_code)
