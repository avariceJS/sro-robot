from __future__ import annotations

import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import cli


@unittest.skipUnless(os.name == "posix", "POSIX file permissions are required")
class StorageCheckCommandTests(unittest.TestCase):
    def test_private_storage_passes_without_printing_document_names_or_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            (root / "synthetic-private-name.pdf").write_text("synthetic-document-content")
            (root / "synthetic-private-name.pdf").chmod(0o600)
            output = StringIO()
            with (
                patch.object(cli.settings, "upload_dir", root),
                patch.object(cli, "session_scope") as session_scope,
                redirect_stdout(output),
            ):
                result = cli.cmd_storage_check(Namespace())

        self.assertEqual(result, 0)
        self.assertIn("directory_owner_matches=True", output.getvalue())
        self.assertIn("files=1", output.getvalue())
        self.assertNotIn("synthetic-private-name", output.getvalue())
        self.assertNotIn("synthetic-document-content", output.getvalue())
        session_scope.assert_not_called()

    def test_database_audit_reports_only_aggregate_storage_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            referenced = root / "synthetic-referenced-document.pdf"
            orphan = root / "synthetic-orphan-document.pdf"
            for path in (referenced, orphan):
                path.write_bytes(b"synthetic document bytes")
                path.chmod(0o600)

            db = MagicMock()
            db.execute.return_value.scalars.return_value = iter(
                (
                    str(referenced),
                    str(referenced),
                    str(root / "synthetic-missing-document.pdf"),
                    "/synthetic/outside/document.pdf",
                )
            )
            output = StringIO()
            with (
                patch.object(cli.settings, "upload_dir", root),
                patch.object(cli, "session_scope", return_value=nullcontext(db)),
                redirect_stdout(output),
            ):
                result = cli.cmd_storage_check(Namespace(database=True))

        self.assertEqual(result, 1)
        self.assertIn("database_documents=4", output.getvalue())
        self.assertIn("missing_files=1", output.getvalue())
        self.assertIn("unreferenced_files=1", output.getvalue())
        self.assertIn("paths_outside_storage=1", output.getvalue())
        self.assertIn("duplicate_paths=1", output.getvalue())
        for sensitive in (
            "synthetic-referenced-document",
            "synthetic-orphan-document",
            "synthetic-missing-document",
            "/synthetic/outside/document.pdf",
            "synthetic document bytes",
        ):
            self.assertNotIn(sensitive, output.getvalue())

    def test_database_audit_fails_without_disclosing_database_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            db = MagicMock()
            db.execute.side_effect = RuntimeError("synthetic database URL secret")
            output = StringIO()
            with (
                patch.object(cli.settings, "upload_dir", root),
                patch.object(cli, "session_scope", return_value=nullcontext(db)),
                redirect_stdout(output),
            ):
                result = cli.cmd_storage_check(Namespace(database=True))

        self.assertEqual(result, 2)
        self.assertIn("Document storage reference audit failed: RuntimeError", output.getvalue())
        self.assertNotIn("synthetic database URL secret", output.getvalue())

    def test_flags_group_access_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as target:
            root = Path(directory)
            root.chmod(0o700)
            unsafe_file = root / "unsafe.pdf"
            unsafe_file.write_bytes(b"synthetic")
            unsafe_file.chmod(0o640)
            (root / "linked.pdf").symlink_to(Path(target) / "outside.pdf")
            output = StringIO()
            with patch.object(cli.settings, "upload_dir", root), redirect_stdout(output):
                result = cli.cmd_storage_check(Namespace())

        self.assertEqual(result, 1)
        self.assertIn("files_with_group_or_other_access=1", output.getvalue())
        self.assertIn("symlinks=1", output.getvalue())
        self.assertNotIn(str(unsafe_file), output.getvalue())

    def test_fails_if_storage_is_owned_by_another_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            output = StringIO()
            with (
                patch.object(cli.settings, "upload_dir", root),
                patch.object(cli.os, "geteuid", return_value=os.geteuid() + 1),
                redirect_stdout(output),
            ):
                result = cli.cmd_storage_check(Namespace())

        self.assertEqual(result, 1)
        self.assertIn("directory_owner_matches=False", output.getvalue())


if __name__ == "__main__":
    unittest.main()
