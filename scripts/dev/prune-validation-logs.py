#!/usr/bin/env python3
"""Prune raw streams from a completed post-patch validation run safely."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATION_ROOT = (REPO_ROOT / "post-patch-validation").resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def recorded_streams(result: dict[str, Any]) -> dict[str, str]:
    streams: dict[str, str] = {}
    checks = result.get("checks")
    if not isinstance(checks, list):
        raise ValueError("result.json has no checks list")
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("runs"), dict):
            raise ValueError("result.json contains a malformed check")
        for run in check["runs"].values():
            if not isinstance(run, dict):
                raise ValueError("result.json contains a malformed run")
            for stream in ("stdout", "stderr"):
                relative = run.get(stream)
                digest = run.get(f"{stream}_sha256")
                if relative is None and digest is None:
                    continue
                if not isinstance(relative, str) or not isinstance(digest, str):
                    raise ValueError(f"Run has an incomplete {stream} record")
                candidate = Path(relative)
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise ValueError(f"Unsafe recorded path: {relative}")
                if candidate.suffix != f".{stream}":
                    raise ValueError(f"Unexpected {stream} path: {relative}")
                if relative in streams and streams[relative] != digest:
                    raise ValueError(f"Conflicting hashes for {relative}")
                streams[relative] = digest
    return streams


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def resolve_run_dir(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if path.is_symlink():
        raise ValueError(f"Refusing symlink run directory: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or not resolved.is_relative_to(VALIDATION_ROOT):
        raise ValueError(f"Run directory must be under {VALIDATION_ROOT}: {path}")
    if not resolved.name.startswith("results"):
        raise ValueError(f"Run directory name must start with 'results': {path}")
    return resolved


def inspect_run(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result_path = path / "result.json"
    report_path = path / "report.md"
    manifest_path = path / "artifact-manifest.json"
    if not result_path.is_file() or not report_path.is_file() or not manifest_path.is_file():
        raise ValueError("Run must contain result.json, report.md, and artifact-manifest.json")
    result = load_json(result_path)
    assessment = result.get("assessment")
    if not isinstance(assessment, dict) or assessment.get("status") != "complete":
        raise ValueError("Refusing to prune a run that is not complete")
    if assessment.get("findings") or assessment.get("gaps"):
        raise ValueError("Refusing to prune a run with findings or validation gaps")
    streams = recorded_streams(result)
    pruned_path = path / "pruned-outputs.json"
    prior_hashes: dict[str, str] = {}
    if pruned_path.exists():
        if pruned_path.is_symlink():
            raise ValueError("Refusing symlink pruned-outputs.json")
        prior = load_json(pruned_path)
        for item in prior.get("files", []):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
                raise ValueError("Malformed pruned-outputs.json entry")
            prior_hashes[item["path"]] = item["sha256"]
    entries: list[dict[str, Any]] = []
    for relative, expected_hash in sorted(streams.items()):
        target = path / relative
        for part in (target, *target.parents):
            if part.is_symlink():
                raise ValueError(f"Refusing symlink in recorded stream path: {target}")
            if part == path:
                break
        if not target.exists() and prior_hashes.get(relative) == expected_hash:
            continue
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"Recorded stream is missing or not a regular file: {target}")
        if sha256(target) != expected_hash:
            raise ValueError(f"Recorded stream hash mismatch: {target}")
        entries.append({
            "path": relative,
            "bytes": target.stat().st_size,
            "sha256": expected_hash,
        })
    return result, entries


def manifest_for(path: Path) -> dict[str, Any]:
    files = []
    for item in sorted(path.rglob("*")):
        if item.name == "artifact-manifest.json" or not item.is_file():
            continue
        if item.is_symlink():
            raise ValueError(f"Refusing symlink in run directory: {item}")
        files.append({
            "path": item.relative_to(path).as_posix(),
            "bytes": item.stat().st_size,
            "sha256": sha256(item),
        })
    return {"schema_version": "1.0", "files": files}


def prune(path: Path, apply: bool) -> None:
    result, entries = inspect_run(path)
    existing_record = path / "pruned-outputs.json"
    removed = {item["path"] for item in entries}
    new_entries = list(entries)
    if existing_record.is_file():
        prior = load_json(existing_record)
        prior_entries = prior.get("files", [])
        known = {item.get("path") for item in prior_entries if isinstance(item, dict)}
        new_entries = [*prior_entries, *(item for item in entries if item["path"] not in known)]
    print(f"{path.relative_to(REPO_ROOT)}: {len(removed)} streams pending, "
          f"{sum(item['bytes'] for item in entries)} bytes; "
          f"assessment={result['assessment']['status']}")
    if not apply:
        return
    for relative in sorted(removed):
        (path / relative).unlink()
    if new_entries:
        write_json_atomic(existing_record, {
            "schema_version": "1.0",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "files": new_entries,
        })
    write_json_atomic(path / "artifact-manifest.json", manifest_for(path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="validate and list streams only")
    mode.add_argument("--apply", action="store_true", help="remove validated raw streams")
    parser.add_argument("run_dirs", nargs="+", help="explicit results directory paths")
    args = parser.parse_args()
    try:
        paths = [resolve_run_dir(raw) for raw in args.run_dirs]
        if len(set(paths)) != len(paths):
            raise ValueError("The same run directory was supplied more than once")
        # Validate every target before changing any of them.
        inspected = [(path, inspect_run(path)) for path in paths]
        for path, (result, entries) in inspected:
            print(f"{path.relative_to(REPO_ROOT)}: {len(entries)} streams, "
                  f"{sum(item['bytes'] for item in entries)} bytes; "
                  f"assessment={result['assessment']['status']}")
        if args.apply:
            for path, _ in inspected:
                prune(path, apply=True)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
