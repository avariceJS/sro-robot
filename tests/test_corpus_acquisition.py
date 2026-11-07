from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.dev.quality import corpus_acquisition as acquisition


CARD = "11111111-2222-3333-4444-555555555555"
DOC = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
KAD = f"https://kad.arbitr.ru/Kad/PdfDocument/{CARD}/{DOC}/act.pdf"
DIRECT = f"https://kad.arbitr.ru/Document/Pdf/{CARD}/{DOC}/act.pdf?isAddStamp=True"
PDF = b"%PDF-1.7\nsynthetic local PDF\n%%EOF\n"


class CorpusAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "private" / "corpus"
        self.export_root = self.base / "export"
        self.export_root.mkdir(mode=0o700)

    def manifest(self, rows):
        path = self.base / "source.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def local_row(self, source_id, name="act.pdf", channel="internal-export"):
        source = self.export_root / name
        source.write_bytes(PDF)
        return {"source_id": source_id, "channel": channel, "local_path": str(source),
                "authority": "owner_assertion", "case_id": "private-case-id", "case_number": "private"}

    def test_queue_import_is_private_and_preserves_diversity_metadata(self):
        row = {"source_id": "private-source-id", "source_url": KAD, "channel": "kad-browser-pdf",
               "authority": "owner_assertion", "case_id": "private-case-id", "court": "private-court",
               "year": "2024", "act_type": "decision", "participant_kind": "individual"}
        self.assertEqual(acquisition.enqueue(self.root, self.manifest([row])), {"added": 1, "queued": 1})
        state = acquisition.load_queue(self.root)
        self.assertEqual(state["items"][0]["metadata"]["act_type"], "decision")
        self.assertEqual(state["items"][0]["authority"], "owner_assertion")
        self.assertEqual((self.root / "queue.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o077, 0)

    def test_rejects_unreviewed_origin_or_non_pdf_route(self):
        for url in ("https://kad.arbitr.ru/Card/" + CARD, KAD.replace("https://", "http://"),
                    KAD.replace("kad.arbitr.ru", "kad.arbitr.ru.evil.invalid"), KAD + "?x=1"):
            row = {"source_id": "private", "source_url": url, "channel": "kad-browser-pdf"}
            with self.subTest(url=url), self.assertRaises(acquisition.QueueError):
                acquisition.enqueue(self.root, self.manifest([row]))
        self.assertEqual(acquisition.validate_kad_url(DIRECT), DIRECT)

    def test_local_export_must_stay_inside_private_export_root(self):
        outside = self.base / "outside.pdf"
        outside.write_bytes(PDF)
        row = {"source_id": "private", "channel": "internal-export", "local_path": str(outside)}
        with self.assertRaisesRegex(acquisition.QueueError, "outside_root"):
            acquisition.enqueue(self.root, self.manifest([row]), self.export_root)

    def test_import_is_idempotent_and_content_deduplicated(self):
        rows = [self.local_row("source-1"), self.local_row("source-2", "second.pdf")]
        manifest = self.manifest(rows)
        self.assertEqual(acquisition.enqueue(self.root, manifest, self.export_root)["added"], 2)
        self.assertEqual(acquisition.enqueue(self.root, manifest, self.export_root)["added"], 0)
        result = acquisition.run(self.root)
        self.assertEqual((result["downloaded"], result["duplicates"], result["pending"]), (2, 1, 0))
        self.assertEqual(len(list((self.root / "inbox").glob("*.pdf"))), 1)
        self.assertEqual(acquisition.status(self.root)["complete"], 2)
        self.assertEqual(len(list((self.root / "receipts").glob("*.json"))), 2)

    def test_rate_limit_pauses_durable_queue_then_resumes_after_spacing(self):
        row = {"source_id": "private", "source_url": KAD, "channel": "kad-direct-pdf"}
        acquisition.enqueue(self.root, self.manifest([row]))
        clock = [100.0]
        def limited(_url, _destination):
            raise acquisition.SourcePause("http_rate_limited", cooldown_seconds=5)
        paused = acquisition.run(self.root, fetch=limited, now=lambda: clock[0], sleep=lambda seconds: None)
        self.assertEqual((paused["state"], paused["pending"]), ("paused", 1))
        clock[0] = 105.0
        resumed = acquisition.run(self.root, fetch=lambda *_: (PDF, KAD), now=lambda: clock[0], sleep=lambda seconds: None)
        self.assertEqual((resumed["state"], resumed["downloaded"], resumed["pending"]), ("complete", 1, 0))

    def test_enforces_single_stream_minimum_spacing(self):
        row = {"source_id": "private", "source_url": KAD, "channel": "kad-direct-pdf"}
        acquisition.enqueue(self.root, self.manifest([row]))
        with self.assertRaisesRegex(acquisition.QueueError, "spacing"):
            acquisition.run(self.root, min_spacing_seconds=1)

    def test_direct_redirect_must_keep_case_and_document_identity(self):
        class FakeResponse(BytesIO):
            status = 200
            headers = {"Content-Type": "application/pdf", "Content-Length": str(len(PDF))}
            def geturl(self):
                return f"https://kad.arbitr.ru/Document/Pdf/{CARD}/{'bbbbbbbb-cccc-dddd-eeee-ffffffffffff'}/act.pdf?isAddStamp=True"

        class FakeOpener:
            def open(self, *_args, **_kwargs):
                return FakeResponse(PDF)

        with patch.object(acquisition, "build_opener", return_value=FakeOpener()):
            with self.assertRaisesRegex(acquisition.SourcePause, "identity_changed"):
                acquisition.download_kad(KAD, self.base)

    def test_access_denial_disables_source_until_explicit_queue_edit(self):
        row = {"source_id": "private", "source_url": KAD, "channel": "kad-direct-pdf"}
        acquisition.enqueue(self.root, self.manifest([row]))
        result = acquisition.run(self.root, fetch=lambda *_: (_ for _ in ()).throw(
            acquisition.SourcePause("http_access_denied", disable=True)))
        self.assertEqual(result["state"], "paused")
        later = acquisition.run(self.root, fetch=lambda *_: (PDF, KAD))
        self.assertEqual(later["state"], "disabled")
        self.assertEqual(later["pending"], 1)

    def test_adapter_reset_requires_explicit_source_restoration(self):
        with self.assertRaisesRegex(acquisition.QueueError, "confirmation"):
            acquisition.reset_adapter(self.root, "kad-browser-pdf", confirm_source_restored=False)
        result = acquisition.reset_adapter(self.root, "kad-browser-pdf", confirm_source_restored=True)
        self.assertTrue(result["adapter_enabled"])

    def test_browser_adapter_uses_validated_normal_navigation_and_native_save(self):
        row = {"source_id": "private", "source_url": KAD, "channel": "kad-browser-pdf",
               "authority": "owner_assertion", "case_number": "private"}
        acquisition.enqueue(self.root, self.manifest([row]))
        discovery = ModuleType("scripts.dev.quality.kad_discovery")
        calls = []
        discovery.AccessChallenge = type("AccessChallenge", (Exception,), {})
        discovery.BrowserNavigationError = type("BrowserNavigationError", (Exception,), {})
        discovery.navigate_pdf = lambda url: calls.append(url) or DIRECT
        inbox = self.root / "inbox"
        inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        saved = inbox / (acquisition.digest(PDF) + ".pdf")
        saved.write_bytes(PDF)
        fake_result = SimpleNamespace(digest=acquisition.digest(PDF), size=len(PDF), duplicate=False, path=saved)
        with patch.dict("sys.modules", {"scripts.dev.quality.kad_discovery": discovery}), patch(
                "scripts.dev.quality.download_current_kad.save_and_publish", return_value=fake_result) as save:
            result = acquisition.run(self.root)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(calls, [KAD])
        self.assertEqual(save.call_args.args, (DIRECT, inbox.resolve()))
        receipt = json.loads(next((self.root / "receipts").glob("*.json")).read_text())
        self.assertEqual(receipt["source_url"], KAD)
        self.assertEqual(receipt["resolved_url"], DIRECT)
        self.assertEqual(receipt["candidate_metadata"]["case_number"], "private")

    def test_confirmed_empty_native_save_becomes_cooldown_not_disabled_queue(self):
        from scripts.dev.quality import download_current_kad
        row = {"source_id": "private", "source_url": KAD, "channel": "kad-browser-pdf"}
        acquisition.enqueue(self.root, self.manifest([row]))
        discovery = ModuleType("scripts.dev.quality.kad_discovery")
        discovery.AccessChallenge = type("AccessChallenge", (Exception,), {})
        discovery.BrowserNavigationError = type("BrowserNavigationError", (Exception,), {})
        discovery.navigate_pdf = lambda url: url
        with patch.dict("sys.modules", {"scripts.dev.quality.kad_discovery": discovery}), patch(
                "scripts.dev.quality.download_current_kad.save_and_publish",
                side_effect=download_current_kad.NativeSaveNotStarted("synthetic explicit status")):
            result = acquisition.run(self.root)
        self.assertEqual((result["state"], result["pending"]), ("paused", 1))
        self.assertEqual(acquisition.status(self.root)["disabled_adapters"], 0)
        self.assertEqual(acquisition.status(self.root)["cooling_adapters"], 1)
        self.assertFalse((self.root / "inbox" / ".native-save-pending.json").exists())


if __name__ == "__main__":
    unittest.main()
