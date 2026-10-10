from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
