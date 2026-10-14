#!/usr/bin/env python3
"""Validate permissions for the local SSH identity used by VPS publishing."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def is_safe_private_identity(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        return False
    if metadata.st_uid != os.geteuid():
        return False
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        return False
    return os.access(path, os.R_OK)


def main() -> int:
    if len(sys.argv) != 2 or not is_safe_private_identity(Path(sys.argv[1])):
        print(
            "SSH identity must be a readable regular file owned by the current "
            "user with mode 0600 or stricter.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
