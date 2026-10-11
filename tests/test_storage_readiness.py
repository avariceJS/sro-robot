from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main


@unittest.skipUnless(os.name == "posix", "POSIX directory ownership is required")
class DocumentStorageReadinessTests(unittest.TestCase):
    def test_accepts_only_the_owned_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            storage.chmod(0o700)
            with patch.object(main.settings, "upload_dir", storage):
                self.assertTrue(main._document_storage_is_ready())

                storage.chmod(0o750)
                self.assertFalse(main._document_storage_is_ready())

    def test_rejects_missing_and_symlink_storage_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            target = parent / "target"
            target.mkdir(mode=0o700)
            missing = parent / "missing"
            linked = parent / "linked"
            linked.symlink_to(target, target_is_directory=True)

            for storage in (missing, linked):
                with self.subTest(storage=storage.name), patch.object(
                    main.settings, "upload_dir", storage
                ):
                    self.assertFalse(main._document_storage_is_ready())


if __name__ == "__main__":
    unittest.main()
