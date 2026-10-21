#!/usr/bin/env python3
"""Run and export the local exact-candidate release gate result."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
VERIFY_COMMAND = "SRO_DRILL_TASKS=100000 MIGRATION_DATABASE_URL='' bash scripts/dev/verify.sh"


class EvidenceError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode:
        raise EvidenceError("GitReadFailed")
    return result.stdout.strip()


def require_candidate_checkout(repo: Path, commit: str) -> str:
    if not COMMIT_RE.fullmatch(commit):
        raise EvidenceError("InvalidCommit")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise EvidenceError("WorktreeNotClean")
    if git(repo, "branch", "--show-current") != "develop":
        raise EvidenceError("CandidateMustBeOnDevelop")
    if git(repo, "rev-parse", "HEAD") != commit:
        raise EvidenceError("HEADDoesNotMatchCandidate")
    if git(repo, "rev-parse", "origin/develop") != commit:
        raise EvidenceError("OriginDevelopDoesNotMatchCandidate")
    return git(repo, "rev-parse", f"{commit}^{{tree}}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_record(record: object, commit: str, tree: str) -> None:
    if not isinstance(record, dict):
        raise EvidenceError("InvalidEvidenceRecord")
    if record.get("schema_version") != 1:
        raise EvidenceError("UnsupportedEvidenceVersion")
    if record.get("source_commit") != commit or record.get("source_tree") != tree:
        raise EvidenceError("EvidenceSourceMismatch")
    if record.get("verification_command") != VERIFY_COMMAND or record.get("verification_exit_code") != 0:
        raise EvidenceError("ExactLocalGateNotPassed")
    if not isinstance(record.get("verification_log_sha256"), str) or not SHA_RE.fullmatch(record["verification_log_sha256"]):
        raise EvidenceError("InvalidVerificationLogHash")
    if not record.get("verified_at_utc"):
        raise EvidenceError("MissingVerificationTimestamp")


def export(repo: Path, commit: str, output: Path) -> int:
    tree = require_candidate_checkout(repo, commit)
    output = output.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists() or output.is_symlink():
        raise EvidenceError("EvidenceOutputAlreadyExists")
    fd, raw_name = tempfile.mkstemp(prefix=f"sro-release-gate-{commit[:12]}-", suffix=".log")
    os.fchmod(fd, 0o600)
    raw_log = Path(raw_name)
    exit_code = 1
    process: subprocess.Popen[bytes] | None = None
    try:
        with os.fdopen(fd, "wb") as log:
            env = os.environ.copy()
            env["SRO_DRILL_TASKS"] = "100000"
            env["MIGRATION_DATABASE_URL"] = ""
            process = subprocess.Popen(
                ["bash", "scripts/dev/verify.sh"],
                cwd=repo,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            assert process.stdout is not None
            for block in iter(lambda: process.stdout.read(64 * 1024), b""):
                log.write(block)
                sys.stdout.buffer.write(block)
                sys.stdout.buffer.flush()
            exit_code = process.wait()
            log.flush()
            os.fsync(log.fileno())

        log_hash = sha256_file(raw_log)
        if exit_code:
            print(f"release_gate=failed commit={commit} exit_code={exit_code} log={raw_log}", file=sys.stderr)
            return exit_code

        final_tree = require_candidate_checkout(repo, commit)
        if final_tree != tree:
            raise EvidenceError("CandidateTreeChangedDuringVerification")

        record = {
            "schema_version": 1,
            "source_commit": commit,
            "source_tree": tree,
            "verification_command": VERIFY_COMMAND,
            "verification_exit_code": 0,
            "verification_log_sha256": log_hash,
            "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        validate_record(record, commit, tree)
        out_fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(out_fd, "w", encoding="utf-8") as target:
            json.dump(record, target, sort_keys=True, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        raw_log.unlink()
        print(f"release_gate=passed commit={commit} tree={tree} log_sha256={log_hash} evidence={output}")
        return 0
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, 15)
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
        print(f"release_gate=interrupted log={raw_log}", file=sys.stderr)
        raise
    except Exception:
        if raw_log.exists():
            print(f"release_gate=failed log={raw_log}", file=sys.stderr)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        output = args.output or Path(tempfile.gettempdir()) / f"sro-release-gate-{args.commit}.json"
        return export(args.repo.resolve(), args.commit, output)
    except EvidenceError as exc:
        print(f"release_gate=refused reason={exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
