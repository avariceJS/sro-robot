from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
from unittest.mock import patch

from app.services.tasks import _write_document_file


root = Path(os.environ["PPV_SCRATCH"])
mode = sys.argv[1]

if mode == "permission":
    path = root / "document.pdf"
    observations: list[int] = []
    original_write_bytes = Path.write_bytes
    original_open = os.open

    def observed_write_bytes(self: Path, data: bytes) -> int:
        result = original_write_bytes(self, data)
        if self == path:
            observations.append(stat.S_IMODE(self.stat().st_mode))
        return result

    def observed_open(file, flags: int, mode_bits: int = 0o777, *args, **kwargs):
        descriptor = original_open(file, flags, mode_bits, *args, **kwargs)
        if Path(file) == path:
            observations.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return descriptor

    previous_umask = os.umask(0)
    try:
        with (
            patch.object(Path, "write_bytes", observed_write_bytes),
            patch.object(os, "open", observed_open),
        ):
            _write_document_file(path, b"synthetic evidence")
    finally:
        os.umask(previous_umask)

    print("PPV_REACHED", flush=True)
    assert observations and observations[0] == 0o600, observations
    print(f"created_mode={observations[0]:04o}")
elif mode == "collision":
    path = root / "document.pdf"
    path.write_bytes(b"preserve")
    refused = False
    try:
        _write_document_file(path, b"overwrite")
    except FileExistsError:
        refused = True
    print("PPV_REACHED", flush=True)
    assert refused and path.read_bytes() == b"preserve"
    print("existing_file=preserved")
elif mode == "symlink":
    target = root / "target.pdf"
    destination = root / "document.pdf"
    target.write_bytes(b"preserve target")
    destination.symlink_to(target)
    refused = False
    try:
        _write_document_file(destination, b"overwrite")
    except FileExistsError:
        refused = True
    print("PPV_REACHED", flush=True)
    assert refused and destination.is_symlink() and target.read_bytes() == b"preserve target"
    print("symlink_target=preserved")
elif mode == "basic":
    path = root / "document.pdf"
    _write_document_file(path, b"synthetic evidence")
    print("content=synthetic evidence")
    print(f"mode={stat.S_IMODE(path.stat().st_mode):04o}")
else:
    raise SystemExit(f"unknown mode: {mode}")
