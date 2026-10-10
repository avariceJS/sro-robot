from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.artifacts import read_screenshot


class ScreenshotArtifactSecurityTests(unittest.TestCase):
    def test_reads_regular_png_directly_from_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "form.png"
            artifact.write_bytes(b"synthetic png bytes")

            self.assertEqual(read_screenshot(str(artifact), root), b"synthetic png bytes")

    def test_rejects_path_outside_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "out"
            outside = Path(directory) / "secret.png"
            root.mkdir()
            outside.write_bytes(b"not a screenshot")

            with self.assertRaises(FileNotFoundError):
                read_screenshot(str(outside), root)

    def test_rejects_symlink_in_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "out"
            root.mkdir()
            outside = Path(directory) / "secret.png"
            outside.write_bytes(b"not a screenshot")
            link = root / "form.png"
            link.symlink_to(outside)

            with self.assertRaises(FileNotFoundError):
                read_screenshot(str(link), root)

    def test_rejects_non_png_and_missing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text_file = root / "secret.txt"
            text_file.write_text("synthetic", encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                read_screenshot(str(text_file), root)
            with self.assertRaises(FileNotFoundError):
                read_screenshot(str(root / "missing.png"), root)

    def test_rejects_non_regular_png_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            not_a_file = root / "directory.png"
            not_a_file.mkdir()

            with self.assertRaises(FileNotFoundError):
                read_screenshot(str(not_a_file), root)


if __name__ == "__main__":
    unittest.main()
