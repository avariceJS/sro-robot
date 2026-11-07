"""Native KAD Save custody and retry guards, using synthetic local bytes only."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.dev.quality import download_current_kad as kad


URL = "https://kad.arbitr.ru/Kad/PdfDocument/11111111-1111-1111-1111-111111111111/22222222-2222-2222-2222-222222222222/synthetic.pdf"
PDF = b"%PDF-1.4\nsynthetic local-only test bytes\n%%EOF\n"


class KADSaveCustodyTests(unittest.TestCase):
    def setup_url(self):
        return patch.object(kad, "current_chrome_url", return_value=URL)

    def test_uncertain_native_save_retains_stage_and_marker_and_blocks_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary) / "inbox"
            def uncertain(staged_path, expected_url):
                self.assertEqual(expected_url, URL)
                staged_path.write_bytes(PDF)
                raise kad.SaveOutcomeUnknown("synthetic uncertainty")

            with self.setup_url(), self.assertRaisesRegex(kad.DownloadError, "unresolved"):
                kad.save_and_publish(URL, inbox, native_save_driver=uncertain)

            marker = inbox / ".native-save-pending.json"
            self.assertTrue(marker.is_file())
            self.assertEqual(list(inbox.glob(".native-kad-save-*/document.pdf"))[0].read_bytes(), PDF)
            with patch.object(kad, "current_chrome_url", side_effect=AssertionError("retry reached Chrome")):
                with self.assertRaisesRegex(kad.DownloadError, "prior native Save is unresolved"):
                    kad.save_and_publish(URL, inbox, native_save_driver=lambda *_: self.fail("driver called"))
            self.assertTrue(marker.is_file())
            self.assertEqual([path for path in inbox.glob("*.pdf") if path.name != "stage.pdf"], [])

    def test_pending_marker_is_removed_only_after_verified_publish_and_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary) / "inbox"
            validated = []
            def driver(staged_path, expected_url):
                self.assertEqual(expected_url, URL)
                staged_path.write_bytes(PDF)
            def validate(snapshot, staging_directory):
                validated.append(snapshot)
                self.assertTrue((staging_directory / "document.pdf").is_file())
            with self.setup_url(), patch.object(kad, "_validate_pdf_snapshot", side_effect=validate):
                result = kad.save_and_publish(URL, inbox, native_save_driver=driver)
            self.assertFalse((inbox / ".native-save-pending.json").exists())
            self.assertEqual(result.path.read_bytes(), PDF)
            self.assertEqual(result.digest, hashlib.sha256(validated[0]).hexdigest())
            receipts = list(inbox.glob("*.receipt.json"))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(result.path.stat().st_mode & 0o777, 0o600)

    def test_explicit_not_saved_clears_only_matching_empty_owned_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary) / "inbox"
            def driver(*_):
                raise kad.NativeSaveNotStarted("explicit synthetic status")
            with self.setup_url(), self.assertRaisesRegex(kad.NativeSaveNotStarted, "stopped before saving"):
                kad.save_and_publish(URL, inbox, native_save_driver=driver)
            self.assertFalse((inbox / ".native-save-pending.json").exists())
            self.assertEqual(list(inbox.glob(".native-kad-save-*")), [])
            self.assertEqual(list(inbox.glob("*.pdf")), [])

    def test_explicit_not_saved_with_partial_stage_retains_marker_and_blocks_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary) / "inbox"
            def partial(staged_path, _url):
                staged_path.write_bytes(b"partial")
                raise kad.NativeSaveNotStarted("explicit status with contradictory bytes")
            with self.setup_url(), self.assertRaisesRegex(kad.DownloadError, "private staging is retained"):
                kad.save_and_publish(URL, inbox, native_save_driver=partial)
            marker = inbox / ".native-save-pending.json"
            stages = list(inbox.glob(".native-kad-save-*"))
            self.assertTrue(marker.is_file())
            self.assertEqual(len(stages), 1)
            self.assertEqual((stages[0] / "document.pdf").read_bytes(), b"partial")
            with patch.object(kad, "current_chrome_url", side_effect=AssertionError("retry reached Chrome")):
                with self.assertRaisesRegex(kad.DownloadError, "prior native Save is unresolved"):
                    kad.save_and_publish(URL, inbox, native_save_driver=lambda *_: self.fail("driver called"))

    def test_not_saved_marker_mismatch_and_symlink_stage_retain_custody(self):
        for mutate in ("marker", "symlink"):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external:
                inbox = Path(temporary) / "inbox"
                def mismatch(staged_path, _url):
                    if mutate == "marker":
                        marker = staged_path.parent.parent / ".native-save-pending.json"
                        marker.write_text("{}")
                    else:
                        stage = staged_path.parent
                        stage.rmdir()
                        stage.symlink_to(external, target_is_directory=True)
                    raise kad.NativeSaveNotStarted("explicit synthetic status")
                with self.setup_url(), self.assertRaisesRegex(kad.DownloadError, "private staging is retained"):
                    kad.save_and_publish(URL, inbox, native_save_driver=mismatch)
                self.assertTrue((inbox / ".native-save-pending.json").exists())
                self.assertTrue(any(inbox.glob(".native-kad-save-*")))

    def test_published_hash_is_for_the_validated_snapshot_even_if_stage_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary) / "inbox"
            validated = []
            replacement = b"%PDF-1.4\nmutated after snapshot\n%%EOF\n"
            def driver(staged_path, _expected_url):
                staged_path.write_bytes(PDF)
            def validate(snapshot, staging_directory):
                validated.append(snapshot)
                # The file changes after snapshot/validation receives immutable bytes.
                (staging_directory / "document.pdf").write_bytes(replacement)
            with self.setup_url(), patch.object(kad, "_validate_pdf_snapshot", side_effect=validate):
                result = kad.save_and_publish(URL, inbox, native_save_driver=driver)
            self.assertEqual(validated, [PDF])
            self.assertEqual(result.digest, hashlib.sha256(PDF).hexdigest())
            self.assertEqual(result.path.read_bytes(), PDF)
            self.assertNotEqual(result.path.read_bytes(), replacement)

    def test_inbox_lock_is_nonblocking_and_released_after_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary) / "inbox"
            inbox.mkdir(mode=0o700)
            with kad._inbox_lock(inbox):
                with self.assertRaisesRegex(kad.DownloadError, "operation is active"):
                    with kad._inbox_lock(inbox):
                        self.fail("nested lock unexpectedly acquired")
            with kad._inbox_lock(inbox):
                self.assertTrue((inbox / ".native-save.lock").is_file())

    def test_symlink_and_oversize_staging_are_rejected_with_fixed_errors_and_no_publish(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external:
            inbox = Path(temporary) / "inbox"
            inbox.mkdir(mode=0o700)
            outside = Path(external) / "synthetic.pdf"
            outside.write_bytes(PDF)
            def symlink_driver(staged_path, _expected_url):
                staged_path.symlink_to(outside)
            with self.setup_url(), self.assertRaisesRegex(kad.SaveOutcomeUnknown, "unverified local file"):
                kad.save_current_pdf(inbox / "stage.pdf", expected_url=URL, native_save_driver=symlink_driver)
            self.assertEqual([path.name for path in inbox.glob("*.pdf")], ["stage.pdf"])
            self.assertTrue((inbox / "stage.pdf").is_symlink())
            with self.assertRaisesRegex(kad.DownloadError, "readable PDF file") as caught:
                kad._snapshot_saved_pdf(inbox / "stage.pdf")
            self.assertEqual(str(caught.exception), "Native Save did not produce a readable PDF file.")

            oversized = Path(external) / "oversized.pdf"
            with oversized.open("wb") as stream:
                stream.truncate(kad.MAX_BYTES + 1)
            with self.assertRaisesRegex(kad.DownloadError, "bounded regular PDF") as caught:
                kad._snapshot_saved_pdf(oversized)
            self.assertEqual(str(caught.exception), "Native Save did not produce a bounded regular PDF file.")
            self.assertEqual([path for path in inbox.glob("*.pdf") if path.name != "stage.pdf"], [])


if __name__ == "__main__":
    unittest.main()
