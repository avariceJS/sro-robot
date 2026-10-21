#!/usr/bin/env python3
"""Validate an extracted release before provisioning mutates host state."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def validate(base: Path, commit: str) -> None:
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError("Invalid release commit")
    release = base / "releases" / commit
    marker = release / ".release-commit"
    venv = base / "venvs" / commit
    if release.is_symlink() or not release.is_dir():
        raise ValueError("Release source directory is unavailable")
    if marker.is_symlink() or not marker.is_file():
        raise ValueError("Release source marker is unavailable")
    if marker.read_text(encoding="ascii").strip() != commit:
        raise ValueError("Release source marker is invalid")
    if (release / ".env").exists() or (release / ".env").is_symlink():
        raise ValueError("Release archive must not contain a private environment file")
    if venv.exists() or venv.is_symlink():
        raise ValueError("Release virtual environment already exists; refusing replacement")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    try:
        validate(args.base, args.commit)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"release_preflight=refused reason={exc}", file=sys.stderr)
        return 2
    print("release_preflight=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
