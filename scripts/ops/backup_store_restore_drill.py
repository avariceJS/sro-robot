#!/usr/bin/env python3
"""Private, isolated verifier for a root-only SRO daily backup.

This does not restore PostgreSQL or touch any running application service. It verifies
all backup hashes, restores documents into a private directory, and optionally loads
redis.rdb in a disposable Redis container with networking disabled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

EXPECTED = {"postgres.dump", "documents.tar.gz", "redis.rdb"}
REDIS_SOURCE_CONTAINER = "deploy-redis-1"
MAX_FILE = 20 * 1024**3
MAX_DOC_TAR = 20 * 1024**3
MAX_DOC_EXPANDED = 32 * 1024**3
MAX_DOC_COUNT = 100_000
MAX_ARCHIVE_MEMBERS = 120_000
MAX_DOC_MEMBER = 2 * 1024**3
MAX_RDB = 2 * 1024**3
MAX_REDIS_MIB = 4096
COMMAND_TIMEOUT = 15
READY_TIMEOUT = 90
MAX_RUNTIME_SECONDS = 3600


class SafeFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_manifest(path: Path) -> dict[str, str]:
    try:
        raw = path.read_bytes()
        text = raw.decode("ascii")
    except (OSError, UnicodeDecodeError):
        raise SafeFailure("manifest_unreadable") from None
    if len(raw) > 1024 or not text.endswith("\n"):
        raise SafeFailure("manifest_format")
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
        if not match:
            raise SafeFailure("manifest_format")
        digest, name = match.groups()
        if name in result:
            raise SafeFailure("manifest_duplicate")
        result[name] = digest
    if set(result) != EXPECTED:
        raise SafeFailure("manifest_entries")
    return result


def validate_backup_root(path: Path, *, require_root: bool = True) -> dict[str, Path]:
    try:
        root = path.lstat()
        if not stat.S_ISDIR(root.st_mode) or stat.S_IMODE(root.st_mode) != 0o700:
            raise SafeFailure("backup_root_mode_or_type")
        if require_root and root.st_uid != 0:
            raise SafeFailure("backup_root_owner")
        manifest_path = path / "SHA256SUMS"
        manifest_stat = manifest_path.lstat()
        if not stat.S_ISREG(manifest_stat.st_mode) or stat.S_IMODE(manifest_stat.st_mode) != 0o600:
            raise SafeFailure("manifest_mode_or_type")
        if require_root and manifest_stat.st_uid != 0:
            raise SafeFailure("manifest_owner")
        manifest = parse_manifest(manifest_path)
        files: dict[str, Path] = {}
        for name in sorted(EXPECTED):
            candidate = path / name
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise SafeFailure("artifact_mode_or_type")
            if require_root and info.st_uid != 0:
                raise SafeFailure("artifact_owner")
            if info.st_size < 1 or info.st_size > MAX_FILE:
                raise SafeFailure("artifact_size_limit")
            if name == "documents.tar.gz" and info.st_size > MAX_DOC_TAR:
                raise SafeFailure("documents_archive_size_limit")
            if name == "redis.rdb" and info.st_size > MAX_RDB:
                raise SafeFailure("redis_rdb_size_limit")
            if sha256_file(candidate) != manifest[name]:
                raise SafeFailure("artifact_hash_mismatch")
            files[name] = candidate
        if {entry.name for entry in path.iterdir()} != EXPECTED | {"SHA256SUMS"}:
            raise SafeFailure("backup_extra_entries")
        return files
    except SafeFailure:
        raise
    except OSError:
        raise SafeFailure("backup_unreadable") from None


def normalized_member(name: str) -> tuple[str, ...]:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise SafeFailure("archive_path_invalid")
    parts = PurePosixPath(name).parts
    if not parts or parts[0] != "documents" or any(p in ("", ".", "..") for p in parts):
        raise SafeFailure("archive_path_invalid")
    if any(":" in p for p in parts):
        raise SafeFailure("archive_path_invalid")
    return parts


def inspect_tar(path: Path) -> tuple[int, int, int]:
    count = total = member_count = 0
    explicit: dict[tuple[str, ...], str] = {}
    try:
        with tarfile.open(path, mode="r|gz") as archive:
            for member in archive:
                member_count += 1
                if member_count > MAX_ARCHIVE_MEMBERS:
                    raise SafeFailure("archive_member_count_limit")
                parts = normalized_member(member.name.rstrip("/") if member.isdir() else member.name)
                if member.pax_headers and any(k in member.pax_headers for k in ("path", "linkpath", "GNU.sparse.map", "GNU.sparse.size")):
                    raise SafeFailure("archive_pax_path_or_sparse")
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise SafeFailure("archive_link_or_special_file")
                if getattr(member, "sparse", None):
                    raise SafeFailure("archive_sparse_file")
                if not (member.isdir() or member.isreg()):
                    raise SafeFailure("archive_member_type")
                kind = "dir" if member.isdir() else "file"
                previous = explicit.get(parts)
                if previous is not None:
                    raise SafeFailure("archive_duplicate_path")
                explicit[parts] = kind
                if kind == "file":
                    if member.size < 0 or member.size > MAX_DOC_MEMBER:
                        raise SafeFailure("archive_member_size_limit")
                    total += member.size
                    count += 1
                    if count > MAX_DOC_COUNT or total > MAX_DOC_EXPANDED:
                        raise SafeFailure("archive_total_limit")
    except SafeFailure:
        raise
    except (OSError, tarfile.TarError, EOFError):
        raise SafeFailure("archive_unreadable") from None
    return count, total, len(explicit)


def _mkdir_chain(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for component in parts:
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise SafeFailure("restore_path_collision") from None
    return current


def extract_and_verify(tar_path: Path, destination: Path, expected: tuple[int, int, int]) -> tuple[int, int, str]:
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(destination, 0o700)
    files = 0
    total = 0
    aggregate = hashlib.sha256()
    explicit: set[tuple[str, ...]] = set()
    member_count = 0
    try:
        with tarfile.open(tar_path, mode="r|gz") as archive:
            for member in archive:
                member_count += 1
                if member_count > MAX_ARCHIVE_MEMBERS:
                    raise SafeFailure("archive_member_count_limit")
                parts = normalized_member(member.name.rstrip("/") if member.isdir() else member.name)
                if member.pax_headers and any(k in member.pax_headers for k in ("path", "linkpath", "GNU.sparse.map", "GNU.sparse.size")):
                    raise SafeFailure("archive_pax_path_or_sparse")
                if member.issym() or member.islnk() or member.isdev() or member.isfifo() or not (member.isdir() or member.isreg()):
                    raise SafeFailure("archive_member_type")
                if getattr(member, "sparse", None):
                    raise SafeFailure("archive_sparse_file")
                if parts in explicit:
                    raise SafeFailure("archive_duplicate_path")
                explicit.add(parts)
                relative = parts[1:]
                if member.isdir():
                    _mkdir_chain(destination, relative)
                    continue
                if member.size > MAX_DOC_MEMBER:
                    raise SafeFailure("archive_member_size_limit")
                parent = _mkdir_chain(destination, relative[:-1]) if len(relative) > 1 else destination
                target = parent / relative[-1]
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    fd = os.open(target, flags, 0o600)
                except OSError:
                    raise SafeFailure("restore_file_create") from None
                source = archive.extractfile(member)
                if source is None:
                    os.close(fd)
                    raise SafeFailure("archive_member_unreadable")
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(fd, "wb", closefd=True) as output, source:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        total += len(block)
                        if size > member.size or total > MAX_DOC_EXPANDED:
                            raise SafeFailure("archive_stream_size_limit")
                        digest.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if size != member.size:
                    raise SafeFailure("archive_size_mismatch")
                # Read back the actual restored bytes and compare to archive stream digest.
                readback = hashlib.sha256()
                read_size = 0
                with target.open("rb") as restored:
                    for block in iter(lambda: restored.read(1024 * 1024), b""):
                        read_size += len(block)
                        readback.update(block)
                if read_size != size or readback.digest() != digest.digest():
                    raise SafeFailure("restored_file_hash_mismatch")
                relative_name = "/".join(relative).encode("utf-8", "strict")
                aggregate.update(len(relative_name).to_bytes(4, "big"))
                aggregate.update(relative_name)
                aggregate.update(size.to_bytes(8, "big"))
                aggregate.update(readback.digest())
                files += 1
        if (files, total, len(explicit)) != expected:
            raise SafeFailure("archive_restore_count_or_size_mismatch")
        return files, total, aggregate.hexdigest()
    except SafeFailure:
        raise
    except (OSError, tarfile.TarError, EOFError, UnicodeError):
        raise SafeFailure("archive_restore_failed") from None


def run_command(args: list[str], timeout: int = COMMAND_TIMEOUT, *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, check=True, text=True, capture_output=capture, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SafeFailure("docker_command_timeout") from None
    except (OSError, subprocess.CalledProcessError):
        raise SafeFailure("docker_command_failed") from None


def parse_keyspace_rows(info: str) -> list[tuple[int, int]]:
    """Return only database index and key count; tolerate newer Redis info fields."""
    rows = re.findall(r"(?m)^db(\d+):keys=(\d+),expires=(\d+),avg_ttl=(-?\d+)(?:,[^\r\n]*)?\r?$", info)
    return [(int(database), int(keys)) for database, keys, _expires, _ttl in rows]


def docker_disk_preflight(docker: str, additional_bytes: int) -> int | None:
    docker_root = run_command([docker, "info", "--format", "{{.DockerRootDir}}"]).stdout.strip()
    root_path = Path(docker_root)
    # On Linux VPS the Docker data root is on the host and can be measured directly.
    # Desktop engines keep it inside their VM; report that boundary instead of
    # mistaking the host filesystem's free bytes for the VM's available space.
    if not root_path.is_dir():
        return None
    free = shutil.disk_usage(root_path).free
    required = additional_bytes + max(128 * 1024**2, additional_bytes // 10)
    if free < required:
        raise SafeFailure("docker_disk_preflight_insufficient")
    return free


def redis_restore(rdb: Path, docker: str, memory_mib: int, evidence: Path) -> tuple[int, int, int | None]:
    if not 256 <= memory_mib <= MAX_REDIS_MIB:
        raise SafeFailure("redis_memory_limit")
    image = run_command([docker, "inspect", "--format", "{{.Image}}", REDIS_SOURCE_CONTAINER]).stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise SafeFailure("redis_source_image_invalid")
    docker_free = docker_disk_preflight(docker, rdb.stat().st_size)
    name = "sro-backup-drill-" + uuid.uuid4().hex[:16]
    cleanup_required = False
    try:
        create = [docker, "create", "--network", "none", "--name", name,
                  "--memory", f"{memory_mib}m", "--cpus", "1", "--pids-limit", "64",
                  image, "redis-server", "--save", "", "--appendonly", "no",
                  "--dir", "/tmp", "--dbfilename", "dump.rdb", "--protected-mode", "yes"]
        # A daemon can create the container and then lose its response. Register
        # cleanup before the request so this partial-success case is still removed.
        cleanup_required = True
        run_command(create)
        # docker cp creates files as root and preserves the private 0600 mode.
        # The official Redis image drops privileges, so stage a private 0644 copy
        # inside this 0700 evidence directory; the no-network container can read it.
        staged_rdb = evidence / "redis-rdb-for-container"
        shutil.copyfile(rdb, staged_rdb)
        os.chmod(staged_rdb, 0o644)
        try:
            run_command([docker, "cp", str(staged_rdb), f"{name}:/tmp/dump.rdb"])
        finally:
            staged_rdb.unlink(missing_ok=True)
        run_command([docker, "start", name])
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                ping = subprocess.run([docker, "exec", name, "redis-cli", "ping"],
                                      check=False, text=True, capture_output=True, timeout=5)
                if ping.returncode == 0 and ping.stdout.strip() == "PONG":
                    break
                state = subprocess.run([docker, "inspect", "--format", "{{.State.Running}}", name],
                                       check=False, text=True, capture_output=True, timeout=5)
                if state.returncode == 0 and state.stdout.strip().lower() == "false":
                    raise SafeFailure("redis_process_exited")
            except SafeFailure:
                raise
            except (OSError, subprocess.TimeoutExpired):
                pass
            time.sleep(1)
        else:
            raise SafeFailure("redis_readiness_timeout")
        persistence = run_command([docker, "exec", name, "redis-cli", "info", "persistence"]).stdout
        if not re.search(r"(?m)^loading:0\s*$", persistence):
            raise SafeFailure("redis_still_loading")
        keyspace = run_command([docker, "exec", name, "redis-cli", "info", "keyspace"]).stdout
        rows = parse_keyspace_rows(keyspace)
        # Read every database containing keys with DBSIZE; INFO is used only to
        # enumerate nonempty DB indexes. No key names or values leave the container.
        sizes: list[int] = []
        for database, _info_key_count in rows:
            size_text = run_command([docker, "exec", name, "redis-cli", "-n", str(database), "dbsize"]).stdout.strip()
            if not re.fullmatch(r"\d+", size_text):
                raise SafeFailure("redis_dbsize_invalid")
            sizes.append(int(size_text))
        databases = len(sizes)
        aggregate = sum(sizes)
        return databases, aggregate, docker_free
    except SafeFailure:
        raise
    finally:
        if cleanup_required:
            # Capture only container runtime diagnostics privately; never print them.
            try:
                proc = subprocess.run([docker, "logs", name], check=False, text=True,
                                      capture_output=True, timeout=COMMAND_TIMEOUT)
                log_path = evidence / "redis-container.log"
                with log_path.open("w", encoding="utf-8") as log:
                    os.chmod(log_path, 0o600)
                    log.write((proc.stdout or "")[:200_000])
                    log.write((proc.stderr or "")[:200_000])
            except (OSError, subprocess.TimeoutExpired):
                pass
            cleanup_failed = True
            try:
                subprocess.run([docker, "rm", "--force", name], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=COMMAND_TIMEOUT)
                readback = subprocess.run(
                    [docker, "ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"],
                    check=False, text=True, capture_output=True, timeout=COMMAND_TIMEOUT)
                present = {line.strip() for line in readback.stdout.splitlines() if line.strip()}
                cleanup_failed = readback.returncode != 0 or bool(present)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_failed = True
            if cleanup_failed:
                raise SafeFailure("redis_container_cleanup_unverified")


def disk_preflight(destination_parent: Path, expanded: int) -> int:
    usage = shutil.disk_usage(destination_parent)
    reserve = max(128 * 1024**2, expanded // 10)
    required = expanded + reserve
    if usage.free < required:
        raise SafeFailure("disk_preflight_insufficient")
    return usage.free


def write_report(evidence: Path, report: dict[str, object]) -> None:
    target = evidence / "report.json"
    with target.open("w", encoding="utf-8") as out:
        os.chmod(target, 0o600)
        json.dump(report, out, sort_keys=True, indent=2)
        out.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_dir", type=Path)
    parser.add_argument("--evidence-parent", type=Path, default=Path("/tmp"))
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--redis-memory-mib", type=int, default=2048)
    parser.add_argument("--skip-redis", action="store_true", help="validate artifacts and restore documents only")
    args = parser.parse_args(argv)
    evidence = Path(tempfile.mkdtemp(prefix="sro-backup-drill-", dir=args.evidence_parent))
    os.chmod(evidence, 0o700)
    report: dict[str, object] = {"status": "failed", "postgres": "hash_verified_only", "redis": "not_run"}
    try:
        signal.signal(signal.SIGALRM, lambda _signum, _frame: (_ for _ in ()).throw(SafeFailure("overall_timeout")))
        signal.alarm(MAX_RUNTIME_SECONDS)
        if os.geteuid() != 0:
            raise SafeFailure("must_run_as_root")
        files = validate_backup_root(args.backup_dir)
        count, expanded, members = inspect_tar(files["documents.tar.gz"])
        free = disk_preflight(evidence, expanded)
        docs = evidence / "restored-documents"
        restored_count, restored_bytes, aggregate = extract_and_verify(
            files["documents.tar.gz"], docs, (count, expanded, members))
        report.update({"backup_hashes": "verified", "document_count": restored_count,
                       "document_bytes": restored_bytes, "document_aggregate_sha256": aggregate,
                       "archive_members": members, "disk_free_bytes_preflight": free,
                       "restored_documents": str(docs)})
        if args.skip_redis:
            report["redis"] = "skipped_by_request"
        else:
            db_count, key_count, docker_free = redis_restore(files["redis.rdb"], args.docker, args.redis_memory_mib, evidence)
            report.update({"redis": "loaded_isolated_readback_ok", "redis_nonempty_db_count": db_count,
                           "redis_aggregate_key_count": key_count,
                           "redis_docker_disk_free_bytes_preflight": docker_free})
        report["status"] = "passed"
        write_report(evidence, report)
        signal.alarm(0)
        print(f"status=passed backup_hashes=verified documents={restored_count} "
              f"bytes={restored_bytes} aggregate_sha256={aggregate} redis={report['redis']} evidence={evidence}")
        return 0
    except SafeFailure as exc:
        signal.alarm(0)
        report["failure_code"] = exc.code
        write_report(evidence, report)
        print(f"status=failed code={exc.code} evidence={evidence}", file=sys.stderr)
        return 1
    except Exception as exc:
        signal.alarm(0)
        report["failure_code"] = "unexpected_" + type(exc).__name__
        write_report(evidence, report)
        print(f"status=failed code=unexpected_{type(exc).__name__} evidence={evidence}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
