#!/usr/bin/env python3
"""Create and integrity-check a root-only local VPS backup."""

from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


APP_ROOT = Path("/opt/sro-robot")
DOCUMENT_ROOT = APP_ROOT / "storage" / "documents"
BACKUP_ROOT = Path("/var/backups/sro-robot")
LOCK_PATH = Path("/run/lock/sro-robot-backup.lock")
REDIS_TEMP = "/tmp/sro-robot-backup.rdb"
EXPECTED_CONTAINERS = {"database": "deploy-db-1", "redis": "deploy-redis-1"}
def run(args: list[str], *, stdout=None, env=None) -> None:
    subprocess.run(args, check=True, stdout=stdout, stderr=subprocess.PIPE, env=env)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_safe_path(path: Path, *, directory: bool) -> None:
    valid_type = path.is_dir() if directory else path.is_file()
    if path.is_symlink() or not valid_type:
        raise RuntimeError(f"Required path is missing or unsafe: {path}")


def check_containers() -> None:
    for label, name in EXPECTED_CONTAINERS.items():
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", name],
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() != "healthy":
            raise RuntimeError(f"{label} container is not healthy")


def create_backup() -> Path:
    if os.geteuid() != 0:
        raise RuntimeError("Run as root")
    require_safe_path(APP_ROOT, directory=True)
    require_safe_path(DOCUMENT_ROOT, directory=True)
    require_safe_path(BACKUP_ROOT, directory=True)
    if (BACKUP_ROOT.stat().st_mode & 0o077) != 0:
        raise RuntimeError("Backup root must be private (mode 0700 or stricter)")
    check_containers()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_dir = BACKUP_ROOT / f"daily-{timestamp}"
    if final_dir.exists():
        raise RuntimeError("Backup destination already exists")

    with tempfile.TemporaryDirectory(prefix=".sro-backup-", dir=BACKUP_ROOT) as scratch:
        work_dir = Path(scratch)
        os.chmod(work_dir, 0o700)
        database_path = work_dir / "postgres.dump"
        with database_path.open("xb") as database_file:
            os.fchmod(database_file.fileno(), 0o600)
            run(
                [
                    "docker", "exec", EXPECTED_CONTAINERS["database"], "sh", "-ec",
                    'pg_dump --format=custom --no-owner --no-privileges '
                    '--username "$POSTGRES_USER" --dbname "$POSTGRES_DB"',
                ],
                stdout=database_file,
            )
            database_file.flush()
            os.fsync(database_file.fileno())

        redis_env = os.environ.copy()
        redis_password = redis_env.get("REDIS_PASSWORD")
        if not redis_password:
            raise RuntimeError("REDIS_PASSWORD is missing from the root backup environment")
        redis_env["REDISCLI_AUTH"] = redis_password
        redis_container = EXPECTED_CONTAINERS["redis"]
        try:
            run(["docker", "exec", "-e", "REDISCLI_AUTH", redis_container,
                 "redis-cli", "--rdb", REDIS_TEMP], env=redis_env)
            redis_path = work_dir / "redis.rdb"
            run(["docker", "cp", f"{redis_container}:{REDIS_TEMP}", str(redis_path)])
        finally:
            subprocess.run(
                ["docker", "exec", redis_container, "rm", "-f", REDIS_TEMP],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        os.chmod(redis_path, 0o600)

        documents_path = work_dir / "documents.tar.gz"
        run([
            "tar", "--create", "--gzip", "--file", str(documents_path),
            "--directory", str(APP_ROOT / "storage"), "documents",
        ])
        os.chmod(documents_path, 0o600)

        run(["pg_restore", "--list", str(database_path)], stdout=subprocess.DEVNULL)
        run(["redis-check-rdb", str(redis_path)], stdout=subprocess.DEVNULL)
        run(["tar", "--list", "--gzip", "--file", str(documents_path)], stdout=subprocess.DEVNULL)

        artifacts = (database_path, redis_path, documents_path)
        manifest = work_dir / "SHA256SUMS"
        with manifest.open("x", encoding="ascii") as output:
            os.fchmod(output.fileno(), 0o600)
            for artifact in artifacts:
                output.write(f"{sha256(artifact)}  {artifact.name}\n")
            output.flush()
            os.fsync(output.fileno())
        run(["sha256sum", "--check", str(manifest)], stdout=subprocess.DEVNULL)

        for item in work_dir.iterdir():
            if not item.is_file() or item.is_symlink():
                raise RuntimeError("Unexpected entry in backup workspace")
            os.chmod(item, 0o600)
        os.chmod(work_dir, 0o700)
        os.replace(work_dir, final_dir)
        dir_fd = os.open(BACKUP_ROOT, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return final_dir


def main() -> int:
    try:
        LOCK_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with LOCK_PATH.open("a", encoding="ascii") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = create_backup()
        print(f"backup=verified path={result} sha256_manifest={sha256(result / 'SHA256SUMS')}")
        return 0
    except BlockingIOError:
        print("Backup already running", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"Backup failed safely: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
