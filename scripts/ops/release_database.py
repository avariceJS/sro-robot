#!/usr/bin/env python3
"""Quiesced PostgreSQL release checkpoint, isolated restore proof and rollback.

Database rows and credentials never appear in output. Existing document/Redis
backups remain separate; release migrations never restore or replace those stores.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

CONTAINER = "deploy-db-1"
MAINTENANCE = Path("/run/sro-robot-maintenance")
BACKUPS = Path("/var/backups/sro-robot")


def run(args, *, stdin=None, stdout=None) -> bytes:
    result = subprocess.run(args, stdin=stdin, stdout=stdout or subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, timeout=300)
    if result.returncode:
        raise RuntimeError("Release database command failed")
    return result.stdout or b""


def database_name(container: str) -> str:
    value = run(["docker", "exec", container, "sh", "-ec",
                 'printf "%s" "${POSTGRES_DB:-postgres}"']).decode()
    if not value or "\x00" in value:
        raise RuntimeError("Database name unavailable")
    return value


def psql_args(container: str, database: str) -> list[str]:
    return ["docker", "exec", "-i", container, "sh", "-ec",
            'exec psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"',
            "psql", database]


def query(container: str, database: str, sql: str) -> bytes:
    result = subprocess.run(psql_args(container, database), input=sql.encode(),
                            capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError("Release database query failed")
    return result.stdout


def signature(container: str, database: str) -> dict:
    """Hash sorted row JSON without retaining or printing row values."""
    result = {}
    names = query(container, database,
                  "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;")
    for name in names.decode().splitlines():
        if not re.fullmatch(r"[a-z_][a-z_0-9]*", name):
            raise RuntimeError("Unexpected public table name")
        sql = f'COPY (SELECT row_to_json(t)::text FROM public."{name}" t ORDER BY row_to_json(t)::text) TO STDOUT;'
        process = subprocess.Popen(psql_args(container, database), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        process.stdin.write(sql.encode())
        process.stdin.close()
        digest = hashlib.sha256()
        while block := process.stdout.read(1024 * 1024):
            digest.update(block)
        process.stdout.close()
        if process.wait(timeout=300):
            raise RuntimeError("Release table signature failed")
        count = int(query(container, database, f'SELECT count(*) FROM public."{name}";'))
        result[name] = {"rows": count, "sha256": digest.hexdigest()}
    sequences = query(container, database,
                      "SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename;")
    sequence_values = {}
    for name in sequences.decode().splitlines():
        if not re.fullmatch(r"[a-z_][a-z_0-9]*", name):
            raise RuntimeError("Unexpected public sequence name")
        sequence_values[name] = query(container, database,
                                     f'SELECT last_value,is_called FROM public."{name}";').decode().strip()
    return {"tables": result, "sequences": sequence_values}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def verify_dump(container: str, dump: Path, expected: dict) -> None:
    name = "sro-release-proof-" + uuid.uuid4().hex[:12]
    image = run(["docker", "inspect", "--format", "{{.Image}}", container]).decode().strip()
    database = database_name(container)
    try:
        run(["docker", "run", "--rm", "-d", "--network", "none", "--name", name,
             "-e", "POSTGRES_USER=postgres", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", image])
        for _ in range(40):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Restore proof database did not start")
        with dump.open("rb") as source:
            run(["docker", "exec", "-i", name, "pg_restore", "--create", "--no-owner",
                 "--no-privileges", "--exit-on-error", "-U", "postgres", "-d", "postgres"], stdin=source)
        if signature(name, database) != expected:
            raise RuntimeError("Restored rows or sequences differ from checkpoint")
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def snapshot(container: str, directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    dump = directory / "rollback.dump"
    database = database_name(container)
    before = signature(container, database)
    with dump.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        # Preserve ownership, database attributes and ACLs for a complete rollback.
        run(["docker", "exec", container, "sh", "-ec",
             'exec pg_dump --format=custom --create -U "$POSTGRES_USER" -d "$POSTGRES_DB"'], stdout=output)
        output.flush()
        os.fsync(output.fileno())
    if signature(container, database) != before:
        raise RuntimeError("Database changed during checkpoint")
    verify_dump(container, dump, before)
    manifest = {"verified": True, "dump_sha256": file_sha256(dump), "signature": before}
    record = directory / "verified.json"
    record.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    record.chmod(0o600)


def require_quiescence(directory: Path) -> None:
    if os.geteuid() != 0 or not MAINTENANCE.is_file() or MAINTENANCE.is_symlink():
        raise RuntimeError("Root and active ingress maintenance are required")
    if directory.parent.resolve() != BACKUPS.resolve() or directory.is_symlink():
        raise RuntimeError("Checkpoint must be a direct child of the private backup root")
    units = run(["systemctl", "list-units", "--state=active", "--type=service", "--no-legend",
                 "sro-robot-web.service", "sro-robot-ocr*.service", "sro-robot-preview.service"])
    if units.strip():
        raise RuntimeError("Application writers must be stopped")


def restore(container: str, directory: Path) -> None:
    manifest_path = directory / "verified.json"
    dump = directory / "rollback.dump"
    if manifest_path.is_symlink() or dump.is_symlink():
        raise RuntimeError("Checkpoint cannot contain symlinks")
    manifest = json.loads(manifest_path.read_text())
    if not manifest.get("verified") or file_sha256(dump) != manifest["dump_sha256"]:
        raise RuntimeError("Checkpoint proof or hash is invalid")
    database = database_name(container)
    quoted = database.replace("'", "''")
    clients = int(query(container, "postgres",
                        f"SELECT count(*) FROM pg_stat_activity WHERE datname='{quoted}';"))
    if clients:
        raise RuntimeError("Database still has connected clients")
    with dump.open("rb") as source:
        run(["docker", "exec", "-i", container, "sh", "-ec",
             'exec pg_restore --clean --if-exists --create --exit-on-error -U "$POSTGRES_USER" -d postgres'],
            stdin=source)
    if signature(container, database) != manifest["signature"]:
        raise RuntimeError("Rollback read-back differs from checkpoint")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "restore"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        require_quiescence(args.directory)
        if args.action == "snapshot":
            snapshot(CONTAINER, args.directory)
        else:
            restore(CONTAINER, args.directory)
        print("release_database=" + args.action + "_verified")
        return 0
    except Exception as exc:
        # Driver output and exceptions can include private database details.
        print("release_database=failed error_type=" + type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
