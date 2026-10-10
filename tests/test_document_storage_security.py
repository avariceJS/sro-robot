from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.tasks import _write_document_file


class DocumentStoragePermissionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX file permissions are required")
    def test_document_file_is_private_even_when_process_umask_is_permissive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-document.pdf"
            previous_umask = os.umask(0)
            try:
                _write_document_file(path, b"synthetic local document")
            finally:
                os.umask(previous_umask)

            self.assertEqual(path.read_bytes(), b"synthetic local document")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions are required")
    def test_restrictive_umask_does_not_remove_service_owner_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-document.pdf"
            previous_umask = os.umask(0o777)
            try:
                _write_document_file(path, b"synthetic local document")
            finally:
                os.umask(previous_umask)

            self.assertEqual(path.read_bytes(), b"synthetic local document")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_existing_document_path_is_not_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-document.pdf"
            path.write_bytes(b"preserve existing bytes")

            with self.assertRaises(FileExistsError):
                _write_document_file(path, b"replacement bytes")

            self.assertEqual(path.read_bytes(), b"preserve existing bytes")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW is not available")
    def test_symlink_destination_does_not_modify_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.pdf"
            destination = root / "document.pdf"
            target.write_bytes(b"preserve symlink target")
            destination.symlink_to(target)

            with self.assertRaises(FileExistsError):
                _write_document_file(destination, b"replacement bytes")

            self.assertEqual(target.read_bytes(), b"preserve symlink target")
            self.assertTrue(destination.is_symlink())

    def test_sync_failure_removes_only_the_partial_file_created_by_this_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-document.pdf"
            with patch("app.services.tasks.os.fsync", side_effect=OSError("synthetic sync error")):
                with self.assertRaisesRegex(OSError, "synthetic sync error"):
                    _write_document_file(path, b"synthetic partial document")

            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
