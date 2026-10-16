from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.api.routes_tasks import task_document, task_document_preview
from app.services.artifacts import read_document


class DocumentArtifactTests(unittest.TestCase):
    def make_document(self, root: Path, content: bytes, suffix: str = ".pdf") -> tuple[Path, str]:
        content_hash = hashlib.sha256(content).hexdigest()
        path = root / f"{content_hash}{suffix}"
        path.write_bytes(content)
        path.chmod(0o600)
        return path, content_hash

    def test_reads_exact_private_content_and_returns_fixed_media_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            content = b"%PDF synthetic only"
            path, content_hash = self.make_document(root, content)

            self.assertEqual(
                read_document(str(path), content_hash, root, 4096),
                (content, "application/pdf", ".pdf"),
            )

    def test_rejects_wrong_hash_oversize_and_non_document_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            content = b"synthetic bounded image"
            path, content_hash = self.make_document(root, content, ".png")

            with self.assertRaises(FileNotFoundError):
                read_document(str(path), "0" * 64, root, 4096)
            with self.assertRaises(FileNotFoundError):
                read_document(str(path), content_hash, root, len(content) - 1)

            path.write_bytes(b"tampered synthetic image")
            path.chmod(0o600)
            with self.assertRaises(FileNotFoundError):
                read_document(str(path), content_hash, root, 4096)

            text_path, text_hash = self.make_document(root, b"synthetic text", ".txt")
            with self.assertRaises(FileNotFoundError):
                read_document(str(text_path), text_hash, root, 4096)

    def test_rejects_path_escape_symlinks_and_insecure_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "private"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            outside = parent / "outside.pdf"
            content = b"synthetic outside file"
            outside.write_bytes(content)
            outside.chmod(0o600)
            content_hash = hashlib.sha256(content).hexdigest()

            with self.assertRaises(FileNotFoundError):
                read_document(str(outside), content_hash, root, 4096)

            link = root / f"{content_hash}.pdf"
            link.symlink_to(outside)
            with self.assertRaises(FileNotFoundError):
                read_document(str(link), content_hash, root, 4096)
            link.unlink()

            private_path, private_hash = self.make_document(root, b"synthetic private", ".pdf")
            private_path.chmod(0o644)
            with self.assertRaises(FileNotFoundError):
                read_document(str(private_path), private_hash, root, 4096)

            root_link = parent / "storage-link"
            root_link.symlink_to(root, target_is_directory=True)
            linked_path = root_link / private_path.name
            private_path.chmod(0o600)
            with self.assertRaises(FileNotFoundError):
                read_document(str(linked_path), private_hash, root_link, 4096)

    def test_authenticated_task_endpoint_returns_attachment_with_private_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            content = b"%PDF synthetic attachment"
            path, content_hash = self.make_document(root, content)
            task = SimpleNamespace(
                id=91,
                document=SimpleNamespace(stored_path=str(path), content_hash=content_hash),
            )
            db = MagicMock()
            with (
                patch("app.api.routes_tasks._require_task", return_value=task),
                patch("app.api.routes_tasks.settings.upload_dir", root),
                patch("app.api.routes_tasks.settings.upload_max_file_bytes", 4096),
            ):
                response = task_document(91, SimpleNamespace(id=7), db)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, content)
        self.assertEqual(response.media_type, "application/pdf")
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="source-document.pdf"')
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["content-security-policy"], "default-src 'none'; sandbox")

    def test_missing_document_is_redacted_as_not_found(self) -> None:
        with (
            patch("app.api.routes_tasks._require_task", return_value=SimpleNamespace(document=None)),
            self.assertRaises(HTTPException) as raised,
        ):
            task_document(91, SimpleNamespace(id=7), MagicMock())
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, "Исходный документ не найден")

    def test_pdf_preview_is_inline_and_sandboxed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            content = b"%PDF synthetic preview"
            path, content_hash = self.make_document(root, content)
            task = SimpleNamespace(document=SimpleNamespace(stored_path=str(path), content_hash=content_hash))
            with (
                patch("app.api.routes_tasks._require_task", return_value=task),
                patch("app.api.routes_tasks.settings.upload_dir", root),
                patch("app.api.routes_tasks.settings.upload_max_file_bytes", 4096),
            ):
                response = task_document_preview(91, SimpleNamespace(id=7), MagicMock())
        self.assertEqual(response.body, content)
        self.assertEqual(response.headers["content-disposition"], 'inline; filename="source-document.pdf"')
        self.assertEqual(response.headers["content-security-policy"], "default-src 'none'; sandbox")

    def test_non_pdf_is_not_rendered_inline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            content = b"synthetic image bytes"
            path, content_hash = self.make_document(root, content, suffix=".png")
            task = SimpleNamespace(document=SimpleNamespace(stored_path=str(path), content_hash=content_hash))
            with (
                patch("app.api.routes_tasks._require_task", return_value=task),
                patch("app.api.routes_tasks.settings.upload_dir", root),
                patch("app.api.routes_tasks.settings.upload_max_file_bytes", 4096),
                self.assertRaises(HTTPException) as raised,
            ):
                task_document_preview(91, SimpleNamespace(id=7), MagicMock())
        self.assertEqual(raised.exception.status_code, 415)


if __name__ == "__main__":
    unittest.main()
