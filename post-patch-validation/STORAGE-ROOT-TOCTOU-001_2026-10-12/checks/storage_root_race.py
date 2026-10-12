from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.services import tasks
from app.services.tasks import (
    _existing_document_file_matches,
    _stored_document_file_matches,
)


def make_root(base: Path, inside: bytes, outside: bytes) -> tuple[Path, Path, Path, str]:
    root = base / "private"
    outside_root = base / "outside"
    root.mkdir(mode=0o700)
    outside_root.mkdir(mode=0o700)
    inside_file = root / "document.pdf"
    outside_file = outside_root / "document.pdf"
    inside_file.write_bytes(inside)
    outside_file.write_bytes(outside)
    inside_file.chmod(0o600)
    outside_file.chmod(0o600)
    return root, outside_root, outside_file, hashlib.sha256(outside).hexdigest()


def control() -> None:
    payload = b"synthetic matching private file"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        root.chmod(0o700)
        path = root / "document.pdf"
        path.write_bytes(payload)
        path.chmod(0o600)
        matched = _existing_document_file_matches(
            path, hashlib.sha256(payload).hexdigest()
        )
    print(f"matched={matched}")
    assert matched is True


def exploit_root_swap_before_child_check() -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        payload = b"synthetic matching private file"
        root, outside_root, outside_file, digest = make_root(base, payload, payload)
        parked_root = base / "private-original"
        original_lstat = Path.lstat
        swapped = False

        def swap_after_root_check(path: Path):
            nonlocal swapped
            result = original_lstat(path)
            if path == root and not swapped:
                root.rename(parked_root)
                root.symlink_to(outside_root, target_is_directory=True)
                swapped = True
            return result

        with (
            patch.object(tasks.settings, "upload_dir", root),
            patch.object(Path, "lstat", autospec=True, side_effect=swap_after_root_check),
        ):
            accepted = _stored_document_file_matches(str(root / "document.pdf"), digest)

        assert swapped is True
        print("PPV_REACHED", flush=True)
        assert accepted is False
        assert outside_file.read_bytes() == payload


def variant_orphan_reuse_with_symlink_root() -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        payload = b"synthetic matching private file"
        root, outside_root, outside_file, digest = make_root(base, payload, payload)
        parked_root = base / "private-original"
        root.rename(parked_root)
        root.symlink_to(outside_root, target_is_directory=True)
        accepted = _existing_document_file_matches(root / "document.pdf", digest)
        print("PPV_REACHED", flush=True)
        assert accepted is False
        assert outside_file.read_bytes() == payload


def behavior_and_security() -> None:
    payload = b"synthetic matching private file"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        root.chmod(0o700)
        target = root / "target.pdf"
        target.write_bytes(payload)
        target.chmod(0o600)
        link = root / "link.pdf"
        link.symlink_to(target)
        matched = _existing_document_file_matches(
            target, hashlib.sha256(payload).hexdigest()
        )
        linked = _existing_document_file_matches(
            link, hashlib.sha256(payload).hexdigest()
        )
    print(f"matched={matched};symlink_rejected={not linked}")
    assert matched is True
    assert linked is False


if __name__ == "__main__":
    modes = {
        "control": control,
        "exploit": exploit_root_swap_before_child_check,
        "variant": variant_orphan_reuse_with_symlink_root,
        "behavior": behavior_and_security,
        "security": behavior_and_security,
    }
    modes[sys.argv[1]]()
