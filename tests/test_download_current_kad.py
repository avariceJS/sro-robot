"""Synthetic tests for the current-tab native KAD Save workflow."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.dev.quality import download_current_kad as downloader


CARD_ID = "11111111-2222-3333-4444-555555555555"
DOC_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
KAD_PDF = f"https://kad.arbitr.ru/Kad/PdfDocument/{CARD_ID}/{DOC_ID}/act.pdf"
DOC_PDF = f"https://kad.arbitr.ru/Document/Pdf/{CARD_ID}/{DOC_ID}/act.pdf?isAddStamp=True"
PDF_BYTES = b"%PDF-1.7\nsynthetic test bytes\n%%EOF"


class DownloadCurrentKadTests(unittest.TestCase):
    def fake_pdfinfo(self, returncode=0):
        return patch.object(downloader.shutil, "which", return_value="/usr/bin/pdfinfo"), patch.object(
            downloader.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=returncode, stdout="synthetic metadata", stderr="private detail"),
        )

    def publish_with_synthetic_native_save(self, inbox: Path, data: bytes = PDF_BYTES):
        def native_save(path, _url):
            path.write_bytes(data)

        get_pdfinfo, run_pdfinfo = self.fake_pdfinfo()
        with patch.object(downloader, "current_chrome_url", side_effect=[KAD_PDF, KAD_PDF]):
            with get_pdfinfo, run_pdfinfo:
                return downloader.save_and_publish(KAD_PDF, inbox, native_save_driver=native_save)

    def test_accepts_only_reviewed_kad_pdf_routes(self) -> None:
        self.assertEqual(downloader.validate_pdf_url(KAD_PDF), KAD_PDF)
        self.assertEqual(downloader.validate_pdf_url(DOC_PDF), DOC_PDF)

    def test_rejects_untrusted_or_ambiguous_urls(self) -> None:
        invalid = [
            KAD_PDF.replace("https://", "http://"),
            KAD_PDF.replace("kad.arbitr.ru", "kad.arbitr.ru.evil.test"),
            KAD_PDF.replace("kad.arbitr.ru/", "user@kad.arbitr.ru/"),
            KAD_PDF.replace("/Kad/PdfDocument/", "/Card/"),
            KAD_PDF + "?download=1",
            KAD_PDF + "#fragment",
            KAD_PDF.replace("kad.arbitr.ru/", "kad.arbitr.ru:443/"),
            KAD_PDF.replace("act.pdf", "%2fsecret.pdf"),
            DOC_PDF.replace("isAddStamp=True", "other=True"),
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(downloader.DownloadError):
                downloader.validate_pdf_url(url)

    def test_current_url_uses_jxa_active_tab_url_only(self) -> None:
        with patch.object(downloader.sys, "platform", "darwin"):
            with patch.object(
                downloader.subprocess,
                "run",
                return_value=SimpleNamespace(stdout=KAD_PDF),
            ) as run:
                self.assertEqual(downloader.current_chrome_url(), KAD_PDF)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:3], ["osascript", "-l", "JavaScript"])
        self.assertIn("activeTab.url()", args[0][4])
        self.assertNotIn("cookies", args[0][4].lower())
        self.assertNotIn("document", args[0][4].lower())
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], 10)

    def test_native_jxa_script_uses_bounded_reviewed_native_save(self) -> None:
        script = downloader._native_save_script(KAD_PDF, "/private/staging")
        for required in (
            "tab.id()",
            "tab.url()",
            "NATIVE_SAVE_COMPLETE",
            "NATIVE_SAVE_UNKNOWN",
            "menuBarItems",
            "Save Page As",
            "Сохранить страницу как",
            "keyCode(5",
            "keyCode(36)",
            'field.value = stagingDirectory',
            'nameFields[0].value = "document.pdf"',
            "Save panel changed",
            'childRole === "AXSheet" || childRole === "AXDialog"',
            "budget.count > 48",
        ):
            self.assertIn(required, script)
        for forbidden in ("fetch(", "XMLHttpRequest", "cookies", "document.body", "evaluateJavaScript"):
            self.assertNotIn(forbidden, script)

    def test_native_driver_passes_script_on_stdin_and_hides_url_from_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "document.pdf"
            with patch.object(downloader.sys, "platform", "darwin"):
                with patch.object(
                    downloader.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="NATIVE_SAVE_COMPLETE\n"),
                ) as run:
                    downloader._native_save_dialog(staging, KAD_PDF)
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["osascript", "-l", "JavaScript", "-"])
            self.assertNotIn(CARD_ID, " ".join(args[0]))
            self.assertIn(KAD_PDF, kwargs["input"])
            self.assertIn(str(Path(tmp).resolve()), kwargs["input"])
            self.assertEqual(kwargs["timeout"], 30)
            self.assertTrue(kwargs["capture_output"])

    def test_native_driver_maps_timeout_to_unknown_without_printing_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "document.pdf"
            with patch.object(downloader.sys, "platform", "darwin"):
                with patch.object(downloader.subprocess, "run", side_effect=downloader.subprocess.TimeoutExpired("osascript", 30)):
                    with self.assertRaisesRegex(downloader.SaveOutcomeUnknown, "outcome is uncertain") as caught:
                        downloader._native_save_dialog(staging, KAD_PDF)
            self.assertNotIn(CARD_ID, str(caught.exception))

    def test_native_save_checks_current_url_before_and_after(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "document.pdf"
            calls = []

            def native_save(path, source_url):
                calls.append((path, source_url))
                path.write_bytes(PDF_BYTES)

            with patch.object(downloader, "current_chrome_url", side_effect=[KAD_PDF, KAD_PDF]):
                result = downloader.save_current_pdf(staging, native_save_driver=native_save)
            self.assertEqual(result, staging)
            self.assertEqual(calls, [(staging, KAD_PDF)])

    def test_native_save_refuses_tab_mismatch_before_ui_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "document.pdf"
            calls = []
            with patch.object(downloader, "current_chrome_url", return_value=DOC_PDF):
                with self.assertRaisesRegex(downloader.DownloadError, "tab changed"):
                    downloader.save_current_pdf(
                        staging,
                        expected_url=KAD_PDF,
                        native_save_driver=lambda *args: calls.append(args),
                    )
            self.assertEqual(calls, [])
            self.assertFalse(staging.exists())

    def test_changed_tab_after_save_preserves_local_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "document.pdf"
            with patch.object(downloader, "current_chrome_url", side_effect=[KAD_PDF, DOC_PDF]):
                with self.assertRaisesRegex(downloader.SaveOutcomeUnknown, "tab changed"):
                    downloader.save_current_pdf(
                        staging,
                        native_save_driver=lambda path, _url: path.write_bytes(PDF_BYTES),
                    )
            self.assertEqual(staging.read_bytes(), PDF_BYTES)

    def test_native_save_refuses_preexisting_staging_file_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "document.pdf"
            staging.write_bytes(b"preserve")
            with patch.object(downloader, "current_chrome_url", return_value=KAD_PDF):
                with self.assertRaisesRegex(downloader.DownloadError, "not empty"):
                    downloader.save_current_pdf(staging)
            self.assertEqual(staging.read_bytes(), b"preserve")

    def test_snapshot_requires_magic_size_and_stable_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "document.pdf"
            path.write_bytes(PDF_BYTES)
            self.assertEqual(downloader._snapshot_saved_pdf(path), PDF_BYTES)

            path.write_bytes(b"<html>challenge</html>")
            with self.assertRaisesRegex(downloader.DownloadError, "stable PDF snapshot"):
                downloader._snapshot_saved_pdf(path)

            path.write_bytes(PDF_BYTES)
            with patch.object(downloader, "MAX_BYTES", 5):
                with self.assertRaisesRegex(downloader.DownloadError, "bounded regular"):
                    downloader._snapshot_saved_pdf(path)

    def test_pdfinfo_validates_an_immutable_snapshot_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage_dir = Path(tmp)
            get_pdfinfo, run_pdfinfo = self.fake_pdfinfo()
            with get_pdfinfo, run_pdfinfo as run:
                downloader._validate_pdf_snapshot(PDF_BYTES, stage_dir)
            validation_path = Path(run.call_args.args[0][1])
            self.assertEqual(validation_path.parent, stage_dir)
            self.assertFalse(validation_path.exists())
            self.assertEqual(list(stage_dir.iterdir()), [])

    def test_pdfinfo_failure_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(downloader.shutil, "which", return_value="/usr/bin/pdfinfo"):
                with patch.object(
                    downloader.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=1, stdout="private text", stderr="private error"),
                ):
                    with self.assertRaisesRegex(downloader.DownloadError, "completeness check failed") as caught:
                        downloader._validate_pdf_snapshot(PDF_BYTES, Path(tmp))
            self.assertNotIn("private text", str(caught.exception))
            self.assertNotIn("private error", str(caught.exception))

    def test_save_and_publish_creates_verified_hash_receipt_and_cleans_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "storage" / "ocr-research-inbox"
            result = self.publish_with_synthetic_native_save(inbox)
            digest = hashlib.sha256(PDF_BYTES).hexdigest()
            source_key = hashlib.sha256(KAD_PDF.encode()).hexdigest()
            self.assertEqual(result.path, inbox / f"{digest}.pdf")
            self.assertEqual(result.path.read_bytes(), PDF_BYTES)
            self.assertFalse(result.duplicate)
            self.assertEqual(result.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(inbox.stat().st_mode & 0o777, 0o700)
            receipt_path = inbox / f"{digest}.{source_key}.receipt.json"
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(receipt["source_url"], KAD_PDF)
            self.assertEqual(receipt["sha256"], digest)
            self.assertEqual(receipt["bytes"], len(PDF_BYTES))
            self.assertTrue(receipt["saved_at_utc"].endswith("+00:00"))
            self.assertFalse((inbox / ".native-save-pending.json").exists())
            self.assertEqual(list(inbox.glob(".native-kad-save-*")), [])

    def test_duplicate_content_is_verified_and_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            first = self.publish_with_synthetic_native_save(inbox)
            original = first.path.read_bytes()
            second = self.publish_with_synthetic_native_save(inbox)
            self.assertTrue(second.duplicate)
            self.assertEqual(second.path.read_bytes(), original)
            self.assertEqual(len(list(inbox.glob("*.receipt.json"))), 1)
            self.assertFalse((inbox / ".native-save-pending.json").exists())
            self.assertEqual(list(inbox.glob(".native-kad-save-*")), [])

    def test_corrupt_existing_hash_file_is_not_overwritten_and_pending_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir(mode=0o700)
            digest = hashlib.sha256(PDF_BYTES).hexdigest()
            path = inbox / f"{digest}.pdf"
            path.write_bytes(b"preserve this corrupt prior file")
            with self.assertRaisesRegex(downloader.DownloadError, "publication did not complete"):
                self.publish_with_synthetic_native_save(inbox)
            self.assertEqual(path.read_bytes(), b"preserve this corrupt prior file")
            self.assertTrue((inbox / ".native-save-pending.json").exists())
            self.assertEqual(len(list(inbox.glob(".native-kad-save-*"))), 1)

    def test_reconciliation_receipt_file_is_not_mistaken_for_stage_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp)
            (inbox / ".native-kad-save-reconciliation.json").write_text("synthetic")
            self.assertFalse(downloader._unresolved_save_exists(inbox))
            (inbox / ".native-kad-save-staging").mkdir()
            self.assertTrue(downloader._unresolved_save_exists(inbox))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support is required")
    def test_symlinked_storage_ancestor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            (root / "storage").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(downloader.DownloadError, "symlink"):
                downloader._default_inbox_for(root)
            self.assertEqual(list(outside.iterdir()), [])

    def test_uncertain_native_outcome_retains_stage_and_blocks_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            calls = []

            def uncertain(stage, _url):
                calls.append(stage)
                stage.write_bytes(PDF_BYTES)
                raise downloader.SaveOutcomeUnknown("private transport detail")

            with patch.object(downloader, "current_chrome_url", side_effect=[KAD_PDF]):
                with self.assertRaisesRegex(downloader.DownloadError, "outcome is unresolved") as caught:
                    downloader.save_and_publish(KAD_PDF, inbox, native_save_driver=uncertain)
            self.assertNotIn("private transport detail", str(caught.exception))
            self.assertTrue((inbox / ".native-save-pending.json").exists())
            stage_dirs = list(inbox.glob(".native-kad-save-*"))
            self.assertEqual(len(stage_dirs), 1)
            self.assertEqual((stage_dirs[0] / "document.pdf").read_bytes(), PDF_BYTES)
            with self.assertRaisesRegex(downloader.DownloadError, "prior native Save is unresolved"):
                downloader.save_and_publish(KAD_PDF, inbox, native_save_driver=uncertain)
            self.assertEqual(len(calls), 1)

    def test_receipt_failure_preserves_pdf_pending_marker_and_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            original = downloader._atomic_create

            def fail_receipt(path, writer):
                if path.name.endswith(".receipt.json"):
                    raise downloader.DownloadError("private detail")
                return original(path, writer)

            with patch.object(downloader, "_atomic_create", side_effect=fail_receipt):
                with self.assertRaisesRegex(downloader.DownloadError, "publication did not complete") as caught:
                    self.publish_with_synthetic_native_save(inbox)
            self.assertNotIn("private detail", str(caught.exception))
            digest = hashlib.sha256(PDF_BYTES).hexdigest()
            self.assertEqual((inbox / f"{digest}.pdf").read_bytes(), PDF_BYTES)
            self.assertTrue((inbox / ".native-save-pending.json").exists())
            self.assertEqual(len(list(inbox.glob(".native-kad-save-*"))), 1)


if __name__ == "__main__":
    unittest.main()
