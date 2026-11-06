from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.tasks import (
    _existing_document_file_matches,
    _stored_document_file_matches,
    _write_document_file,
)
from app.services import tasks


class DocumentStoragePermissionTests(unittest.TestCase):
    def test_prepared_request_hash_is_bound_to_immutable_bytes_and_reused(self) -> None:
        from dataclasses import FrozenInstanceError
        from unittest.mock import MagicMock
        content = b"synthetic request bytes"
        digest = hashlib.sha256(content).hexdigest()
        db = MagicMock()
        db.scalar.return_value = None
        with patch.object(tasks.hashlib, "sha256", wraps=hashlib.sha256) as hashing:
            prepared = tasks.HashedDocumentContent(content)
            with (patch.object(tasks, "_lock_document_hash"),
                  patch.object(tasks, "_write_document_file") as write):
                result = tasks.store_document(db, "synthetic.pdf", prepared, tasks.IntakeSource.upload)
            self.assertEqual(hashing.call_count, 1)
        self.assertEqual(result.content_hash, digest)
        self.assertEqual(write.call_args.args[1], content)
        with self.assertRaises(FrozenInstanceError):
            prepared.content = b"changed"
        with self.assertRaises(TypeError):
            tasks.HashedDocumentContent(bytearray(content))
        self.assertNotIn("synthetic request", repr(prepared))

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

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions are required")
    def test_matching_private_orphan_file_can_be_reused_after_database_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-document.pdf"
            content = b"synthetic orphan from a rolled-back database transaction"
            path.write_bytes(content)
            path.chmod(0o600)

            self.assertTrue(
                _existing_document_file_matches(path, hashlib.sha256(content).hexdigest())
            )

    def test_mismatched_or_symlink_file_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b"synthetic expected document"
            mismatch = root / "mismatch.pdf"
            mismatch.write_bytes(b"different synthetic bytes")
            target = root / "target.pdf"
            target.write_bytes(content)
            link = root / "link.pdf"
            link.symlink_to(target)
            expected_hash = hashlib.sha256(content).hexdigest()

            self.assertFalse(_existing_document_file_matches(mismatch, expected_hash))
            self.assertFalse(_existing_document_file_matches(link, expected_hash))
            self.assertEqual(target.read_bytes(), content)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX no-follow support is required")
    def test_rejects_storage_root_replaced_by_symlink_before_parent_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "private"
            parked_root = base / "private-original"
            outside = base / "outside"
            root.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            payload = b"synthetic file outside the replaced storage root"
            content_hash = hashlib.sha256(payload).hexdigest()
            (root / "document.pdf").write_bytes(payload)
            (root / "document.pdf").chmod(0o600)
            outside_file = outside / "document.pdf"
            outside_file.write_bytes(payload)
            outside_file.chmod(0o600)

            original_lstat = Path.lstat
            swapped = False

            def swap_root_after_first_lstat(path):
                nonlocal swapped
                result = original_lstat(path)
                if Path(path) == root and not swapped:
                    root.rename(parked_root)
                    root.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return result

            with (
                patch.object(tasks.settings, "upload_dir", root),
                patch.object(Path, "lstat", autospec=True, side_effect=swap_root_after_first_lstat),
            ):
                accepted = _stored_document_file_matches(
                    str(root / "document.pdf"), content_hash
                )

            self.assertTrue(swapped)
            self.assertFalse(accepted)
            self.assertEqual(outside_file.read_bytes(), payload)

    def test_database_references_must_remain_private_direct_children_with_matching_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            content = b"synthetic referenced source"
            content_hash = hashlib.sha256(content).hexdigest()
            stored = root / f"{content_hash}.pdf"
            _write_document_file(stored, content)

            with patch.object(tasks.settings, "upload_dir", root):
                self.assertTrue(_stored_document_file_matches(str(stored), content_hash))
                self.assertFalse(
                    _stored_document_file_matches(str(root / "missing.pdf"), content_hash)
                )
                self.assertFalse(
                    _stored_document_file_matches(str(stored), hashlib.sha256(b"other").hexdigest())
                )

                external = root.parent / f"{root.name}-external.pdf"
                _write_document_file(external, content)
                try:
                    self.assertFalse(
                        _stored_document_file_matches(str(external), content_hash)
                    )
                finally:
                    external.unlink()

                link = root / "linked.pdf"
                link.symlink_to(stored)
                self.assertFalse(_stored_document_file_matches(str(link), content_hash))

    def test_database_reference_rejects_symlinked_or_insecure_storage_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "private"
            root.mkdir(mode=0o700)
            content = b"synthetic referenced source"
            content_hash = hashlib.sha256(content).hexdigest()
            stored = root / f"{content_hash}.pdf"
            _write_document_file(stored, content)

            with patch.object(tasks.settings, "upload_dir", root):
                root.chmod(0o750)
                self.assertFalse(_stored_document_file_matches(str(stored), content_hash))
                root.chmod(0o700)

            link = parent / "storage-link"
            link.symlink_to(root, target_is_directory=True)
            linked_path = link / stored.name
            with patch.object(tasks.settings, "upload_dir", link):
                self.assertFalse(
                    _stored_document_file_matches(str(linked_path), content_hash)
                )

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
