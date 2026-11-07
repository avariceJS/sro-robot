from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.dev.quality import corpus_acquisition as acquisition
from scripts.dev.quality import download_current_kad as downloader


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

    def test_queue_deduplicates_pdf_guid_across_kad_routes(self):
        acquisition.enqueue(self.root, self.manifest([{
            "source_id":"first-version", "source_url":KAD, "channel":"kad-browser-pdf",
            "case_id":"private-case", "year":"2023",
        }]))
        added=acquisition.enqueue(self.root, self.manifest([{
            "source_id":"second-version", "source_url":DIRECT, "channel":"kad-direct-pdf",
            "case_id":"private-case", "year":"2023",
        }]))
        self.assertEqual(added["added"], 0)
        self.assertEqual(added["queued"], 1)

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

    def test_aggregate_profile_is_private_and_contains_no_queue_metadata(self):
        acquisition.enqueue(self.root, self.manifest([self.local_row("PRIVATE_SYNTHETIC_SOURCE")]), self.export_root)
        with acquisition.profile_stage(self.root, "queue.readback"):
            pass
        path = self.root / acquisition.PROFILE_FILE
        profile = json.loads(path.read_text())
        self.assertEqual(profile["stages"]["queue.readback"]["count"], 1)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("PRIVATE_SYNTHETIC_SOURCE", path.read_text())

    def test_weak_pdf_text_flags_are_synthetic_candidate_signals_only(self):
        acquisition.private_root(self.root)
        base = [1, 2, 3, 4, 5, 6, 7, 8, 9, 0]
        first = sum(a*b for a, b in zip(base, (7,2,4,10,3,5,9,4,6,8))) % 11 % 10
        digits = base + [first]
        second = sum(a*b for a, b in zip(digits, (3,7,2,4,10,3,5,9,4,6,8))) % 11 % 10
        synthetic_inn = "".join(map(str, digits + [second]))
        text = (f"Должник Иванов Иван Иванович ИНН {synthetic_inn}. Адрес регистрации: тестовый адрес. "
                "Синтетический фрагмент для проверки локальных текстовых признаков. " * 2)
        features = acquisition._weak_text_features_from_text(text)
        self.assertTrue(features["weak_pdf_text"])
        self.assertTrue(features["fio_debtor_anchor"])
        self.assertTrue(features["valid_inn_candidate"])
        self.assertTrue(features["inn_near_debtor_anchor"])
        self.assertTrue(features["address_anchor"])
        self.assertTrue(features["likely_personal_fields"])
        acquisition.record_research_features(self.root, features)
        profile_text = (self.root / acquisition.PROFILE_FILE).read_text()
        profile = json.loads(profile_text)
        self.assertEqual(profile["research"]["documents"], 1)
        self.assertEqual(profile["research"]["likely_personal_fields"], 1)
        self.assertNotIn(synthetic_inn, profile_text)
        self.assertNotIn("Иванов", profile_text)
        self.assertNotIn("Иванов", profile_text)

    def test_profile_io_failure_does_not_mask_access_challenge(self):
        from scripts.dev.quality.kad_discovery import AccessChallenge
        with patch.object(acquisition, "write_json", side_effect=OSError("synthetic profile disk failure")):
            with self.assertRaises(AccessChallenge):
                with acquisition.profile_stage(self.root, "source.navigation"):
                    raise AccessChallenge("challenge")

    def test_profile_pause_failure_cannot_prevent_durable_source_disable(self):
        row = {"source_id": "private", "source_url": KAD, "channel": "kad-direct-pdf"}
        acquisition.enqueue(self.root, self.manifest([row]))
        def denied(*_args):
            raise acquisition.SourcePause("http_access_denied", disable=True)
        with patch.object(acquisition, "record_pause", side_effect=OSError("synthetic profile failure")):
            result = acquisition.run(self.root, fetch=denied)
        self.assertEqual((result["state"], result["pending"]), ("paused", 1))
        held = acquisition.run(self.root, fetch=lambda *_: self.fail("disabled source retried"))
        self.assertEqual((held["state"], held["pending"]), ("disabled", 1))

    def test_low_disk_backpressure_leaves_source_queue_pending(self):
        row = {"source_id": "private", "source_url": KAD, "channel": "kad-direct-pdf"}
        acquisition.enqueue(self.root, self.manifest([row]))
        with patch.object(acquisition.shutil, "disk_usage", return_value=SimpleNamespace(free=1)):
            result = acquisition.run(self.root, fetch=lambda *_: self.fail("source called below disk reserve"))
        self.assertEqual((result["state"], result["pending"], result["downloaded"]), ("paused", 1, 0))

    def test_pending_queue_limit_rejects_new_work_without_mutating_state(self):
        row = self.local_row("private")
        with patch.object(acquisition, "MAX_PENDING_ITEMS", 0):
            with self.assertRaisesRegex(acquisition.QueueError, "pending_queue_limit"):
                acquisition.enqueue(self.root, self.manifest([row]), self.export_root)
        self.assertEqual(acquisition.load_queue(self.root)["items"], [])

    def test_cached_candidate_enqueue_requires_same_case_and_deduplicates_guids(self):
        acquisition.private_root(self.root)
        discovery = self.root / "discovery"
        discovery.mkdir(mode=0o700, parents=True)
        second_doc = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        variant = KAD.replace("act.pdf", "version-two.pdf")
        other_pdf = KAD.replace(DOC, second_doc)
        candidate = {"channel":"kad-browser-pdf", "source_url":KAD, "source_id":acquisition.digest(KAD.encode()),
                     "case_id":CARD, "year":"2023", "act_type":"decision", "participant_kind":"unknown",
                     "caption":"Решение: признать гражданина банкротом и ввести реализацию имущества.",
                     "provenance":{"card_url":"https://kad.arbitr.ru/Card/"+CARD,
                                   "judicial_act_filter_requested":True}}
        duplicate = dict(candidate, source_url=variant, source_id=acquisition.digest(variant.encode()))
        cross_case = dict(candidate, source_url=other_pdf, source_id=acquisition.digest(other_pdf.encode()),
                          case_id="33333333-3333-4333-8333-333333333333")
        malformed = dict(candidate, source_url="https://invalid.example/act.pdf", source_id="malformed")
        acquisition.write_json(discovery / "synthetic.json", {
            "case":{"case_id":CARD,"card_url":"https://kad.arbitr.ru/Card/"+CARD,"case_number":"synthetic"},
            "candidates":[candidate,duplicate,cross_case,malformed],
        })
        self.assertEqual(acquisition.enqueue_cached_candidates(self.root, limit=10), 1)
        self.assertEqual(acquisition.enqueue_cached_candidates(self.root, limit=10), 0)
        queued=[item for item in acquisition.load_queue(self.root)["items"] if item["channel"]=="kad-browser-pdf"]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["metadata"]["case_id"], CARD)

    def test_cached_candidate_enqueue_rejects_repository_root(self):
        with self.assertRaisesRegex(acquisition.QueueError, "private_root_inside_repository"):
            acquisition.enqueue_cached_candidates(acquisition.ROOT)

    def test_cached_case_year_filter_uses_official_number_not_query_stratum(self):
        acquisition.private_root(self.root)
        discovery = self.root / "discovery"
        discovery.mkdir(mode=0o700, parents=True)
        other_case = "33333333-3333-4333-8333-333333333333"
        other_doc = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        def record(case_id, case_number, document_id, query_year):
            card_url = "https://kad.arbitr.ru/Card/" + case_id
            url = f"https://kad.arbitr.ru/Kad/PdfDocument/{case_id}/{document_id}/act.pdf"
            row = {"source_id":acquisition.digest(url.encode()),"source_url":url,"channel":"kad-browser-pdf",
                   "case_id":case_id,"year":query_year,"act_type":"decision","participant_kind":"unknown",
                   "caption":"Решение: признать гражданина банкротом и ввести реализацию имущества.",
                   "provenance":{"card_url":card_url,"judicial_act_filter_requested":True}}
            return {"case":{"case_id":case_id,"card_url":card_url,"case_number":case_number},"candidates":[row]}
        acquisition.write_json(discovery / "eligible.json", record(CARD,"А00-123/2025",DOC,"2023"))
        acquisition.write_json(discovery / "wrong-year.json", record(other_case,"А00-456/2024",other_doc,"2025"))
        self.assertEqual(acquisition.enqueue_cached_candidates(
            self.root, limit=10, preferred_year=None, allowed_case_years={2025}), 1)
        item=acquisition.load_queue(self.root)["items"][0]
        self.assertEqual(item["metadata"]["case_number"], "А00-123/2025")
        self.assertEqual(item["metadata"]["year"], "2023")

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
        with patch.dict("sys.modules", {"scripts.dev.quality.kad_discovery": discovery}), patch.object(
                downloader, "save_and_publish", return_value=fake_result) as save:
            result = acquisition.run(self.root)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(calls, [KAD])
        self.assertEqual(save.call_args.args, (DIRECT, inbox.resolve()))
        receipt = json.loads(next((self.root / "receipts").glob("*.json")).read_text())
        self.assertEqual(receipt["source_url"], KAD)
        self.assertEqual(receipt["resolved_url"], DIRECT)
        self.assertEqual(receipt["candidate_metadata"]["case_number"], "private")

    def test_unresolved_native_save_holds_before_browser_navigation(self):
        from scripts.dev.quality.corpus_acquisition import SourcePause
        inbox = self.root / "inbox"
        inbox.mkdir(mode=0o700, parents=True)
        discovery = ModuleType("scripts.dev.quality.kad_discovery")
        discovery.AccessChallenge = type("AccessChallenge", (Exception,), {})
        discovery.BrowserNavigationError = type("BrowserNavigationError", (Exception,), {})
        discovery.navigate_pdf = lambda *_args, **_kwargs: self.fail("navigation ran with unresolved Save")
        item = {"source_url": KAD, "attempts": 1}
        with patch.dict("sys.modules", {"scripts.dev.quality.kad_discovery": discovery}):
            unresolved_states = [inbox / ".native-save-pending.json", inbox / ".native-kad-save-recovery"]
            unresolved_states[0].write_text("private unresolved state")
            unresolved_states[1].mkdir(mode=0o700)
            for marker in unresolved_states:
                with self.subTest(state=marker.name), self.assertRaises(SourcePause) as caught:
                    acquisition.download_browser(item, self.root)
                self.assertEqual(caught.exception.code, "browser_save_unresolved")
                self.assertTrue(caught.exception.disable)
                self.assertTrue(marker.exists())

    def test_owned_browser_passes_verified_native_pid_to_save(self):
        row = {"source_id": "private", "source_url": KAD, "channel": "kad-browser-pdf"}
        acquisition.enqueue(self.root, self.manifest([row]))
        lease_path = self.root / "discovery" / "browser-lease.json"
        lease_path.parent.mkdir(mode=0o700, parents=True)
        lease_path.write_text("private lease")
        discovery = ModuleType("scripts.dev.quality.kad_discovery")
        discovery.AccessChallenge = type("AccessChallenge", (Exception,), {})
        discovery.BrowserNavigationError = type("BrowserNavigationError", (Exception,), {})
        discovery.navigate_pdf = lambda url, **kwargs: url
        discovery._load_owned_lease = lambda _root: {"native_pid": 23456}
        inbox = self.root / "inbox"
        inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        saved = inbox / (acquisition.digest(PDF) + ".pdf")
        saved.write_bytes(PDF)
        result = SimpleNamespace(digest=acquisition.digest(PDF), size=len(PDF), duplicate=False, path=saved)
        with patch.dict("sys.modules", {"scripts.dev.quality.kad_discovery": discovery}), patch.object(
                downloader, "save_and_publish", return_value=result) as save:
            acquisition.download_browser({"source_url": KAD, "attempts": 1}, self.root)
        self.assertEqual(save.call_args.args, (KAD, inbox))
        self.assertEqual(save.call_args.kwargs, {"native_process_id": 23456})

    def test_owned_browser_without_verified_native_pid_holds_before_save(self):
        row = {"source_id": "private", "source_url": KAD, "channel": "kad-browser-pdf"}
        acquisition.enqueue(self.root, self.manifest([row]))
        lease_path = self.root / "discovery" / "browser-lease.json"
        lease_path.parent.mkdir(mode=0o700, parents=True)
        lease_path.write_text("private lease")
        discovery = ModuleType("scripts.dev.quality.kad_discovery")
        discovery.AccessChallenge = type("AccessChallenge", (Exception,), {})
        discovery.BrowserNavigationError = type("BrowserNavigationError", (Exception,), {})
        discovery.navigate_pdf = lambda url, **kwargs: url
        discovery._load_owned_lease = lambda _root: {"native_pid": None}
        with patch.dict("sys.modules", {"scripts.dev.quality.kad_discovery": discovery}), patch.object(
                downloader, "save_and_publish") as save:
            with self.assertRaises(acquisition.SourcePause) as caught:
                acquisition.download_browser({"source_url": KAD, "attempts": 1}, self.root)
        self.assertEqual(caught.exception.code, "browser_native_identity_unavailable")
        self.assertTrue(caught.exception.disable)
        save.assert_not_called()

    def test_confirmed_empty_native_save_becomes_cooldown_not_disabled_queue(self):
        row = {"source_id": "private", "source_url": KAD, "channel": "kad-browser-pdf"}
        acquisition.enqueue(self.root, self.manifest([row]))
        discovery = ModuleType("scripts.dev.quality.kad_discovery")
        discovery.AccessChallenge = type("AccessChallenge", (Exception,), {})
        discovery.BrowserNavigationError = type("BrowserNavigationError", (Exception,), {})
        discovery.navigate_pdf = lambda url: url
        with patch.dict("sys.modules", {"scripts.dev.quality.kad_discovery": discovery}), patch.object(
                downloader, "save_and_publish",
                side_effect=downloader.NativeSaveNotStarted("synthetic explicit status")):
            result = acquisition.run(self.root)
        self.assertEqual((result["state"], result["pending"]), ("paused", 1))
        self.assertEqual(acquisition.status(self.root)["disabled_adapters"], 0)
        self.assertEqual(acquisition.status(self.root)["cooling_adapters"], 1)
        self.assertFalse((self.root / "inbox" / ".native-save-pending.json").exists())

    def test_regular_reconciliation_note_does_not_disable_browser_adapter(self):
        row = {"source_id": "private", "source_url": KAD, "channel": "kad-browser-pdf"}
        acquisition.enqueue(self.root, self.manifest([row]))
        (self.root / "inbox" / ".native-kad-save-reconciliation.json").write_text("private note")
        discovery = ModuleType("scripts.dev.quality.kad_discovery")
        discovery.AccessChallenge = type("AccessChallenge", (Exception,), {})
        discovery.BrowserNavigationError = type("BrowserNavigationError", (Exception,), {})
        discovery.navigate_pdf = lambda url: url
        with patch.dict("sys.modules", {"scripts.dev.quality.kad_discovery": discovery}), patch.object(
                downloader, "save_and_publish",
                side_effect=downloader.DownloadError("synthetic retryable failure")):
            result = acquisition.run(self.root)
        self.assertEqual(result["state"], "paused")
        self.assertEqual(acquisition.status(self.root)["disabled_adapters"], 0)
        self.assertEqual(acquisition.status(self.root)["cooling_adapters"], 1)


if __name__ == "__main__":
    unittest.main()
