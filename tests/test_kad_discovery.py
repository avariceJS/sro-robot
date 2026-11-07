"""Ordinary browser discovery contracts with synthetic metadata only."""
import json
import contextlib
import io
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts.dev.quality import kad_discovery as discovery

CASE_ID = "11111111-1111-4111-8111-111111111111"
DOC_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ID = "33333333-3333-4333-8333-333333333333"
CARD = "https://kad.arbitr.ru/Card/" + CASE_ID
PDF = f"https://kad.arbitr.ru/Kad/PdfDocument/{CASE_ID}/{DOC_ID}/synthetic.pdf"
DIRECT = f"https://kad.arbitr.ru/Document/Pdf/{CASE_ID}/{DOC_ID}/synthetic.pdf?isAddStamp=True"


class KadDiscoveryTests(unittest.TestCase):
    def test_retry_reuses_loaded_same_pdf_without_reload_and_keeps_tab_guards(self):
        target = {"window": "11", "tab": "22", "url": DIRECT}
        with patch.object(discovery, "_BROWSER_TARGET", None), patch.object(discovery, "_jxa", side_effect=[target, {"url": DIRECT, "title": "synthetic.pdf"}]) as driver:
            self.assertEqual(discovery.navigate_pdf(PDF), DIRECT)
        script = driver.call_args_list[1].args[0]
        self.assertNotIn("tab.url=", script)
        self.assertIn('String(tabs[index].url())!==target.url', script)
        self.assertIn('String(c.windows[0].activeTab.id())!==id', script)
        self.assertIn('while(tab.loading()', script)

    def test_fixed_native_failure_reasons_survive_without_exposing_stderr(self):
        for reason in ("timeout", "identity", "unavailable"):
            error = subprocess.CalledProcessError(1, ["osascript"], stderr=f"PRIVATE_SYNTHETIC_PREFIX execution error: Error: {reason} (-2700)\n")
            with self.subTest(reason=reason), patch.object(discovery.subprocess, "run", side_effect=error):
                with self.assertRaises(discovery.BrowserNavigationError) as raised:
                    discovery._jxa("synthetic")
                self.assertEqual(raised.exception.reason, reason)
                self.assertNotIn("PRIVATE_SYNTHETIC_PREFIX", str(raised.exception))

    def test_invalid_cli_does_not_echo_private_arguments(self):
        stream = io.StringIO()
        with patch.object(sys, "argv", ["kad_discovery", "--category", "PRIVATE_SYNTHETIC_VALUE"]), contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            self.assertEqual(discovery.main(), 1)
        self.assertNotIn("PRIVATE_SYNTHETIC_VALUE", stream.getvalue())
        self.assertEqual(json.loads(stream.getvalue())["status"], "discovery-failed")

    def test_browser_discovery_does_not_inherit_another_adapter_hold(self):
        from scripts.dev.quality import corpus_acquisition as acquisition
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            case = dict(case_id=CASE_ID, card_url=CARD, case_number="А00-7/2026")
            discovery.write_json(root / "discovery/cases.json", [case])
            acquisition.init(root)
            state = acquisition.load_queue(root)
            state["source_controls"] = {"kad-direct-pdf": {"disabled": True}}
            discovery.write_json(root / "queue.json", state)
            args = ["kad_discovery", "--root", str(root), "--resume-cases", "--download"]
            with patch.object(sys, "argv", args), patch.object(discovery, "card_candidates", return_value=[]) as collect, patch.object(discovery.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(discovery.main(), 0)
            collect.assert_called_once()
            self.assertTrue(acquisition.load_queue(root)["source_controls"]["kad-direct-pdf"]["disabled"])

    def test_cli_and_queue_import_share_one_browser_cursor(self):
        script = Path(discovery.__file__).resolve()
        code = '''import runpy,sys,importlib,contextlib,io
sys.argv=[sys.argv[1],"--help"]
with contextlib.redirect_stdout(io.StringIO()):
 try:runpy.run_path(sys.argv[0],run_name="__main__")
 except SystemExit as e:assert e.code==0
m=importlib.import_module("scripts.dev.quality.kad_discovery")
assert m.__name__=="__main__"
m._BROWSER_TARGET={"synthetic":"shared"}
assert importlib.import_module("scripts.dev.quality.kad_discovery")._BROWSER_TARGET=={"synthetic":"shared"}
'''
        result = subprocess.run([sys.executable, "-c", code, str(script)], cwd=discovery.ROOT, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, "CLI and imported queue adapter must share browser ownership")

    def test_navigation_is_exact_kad_and_read_only_routes(self):
        for url in ("https://kad.arbitr.ru/", CARD, PDF, DIRECT):
            self.assertEqual(discovery.valid_navigation(url), url)
        for url in ("http://kad.arbitr.ru/", "https://kad.arbitr.ru.example.org/", "https://secret@kad.arbitr.ru/", "https://kad.arbitr.ru:443/", "https://kad.arbitr.ru/login", "file:///private/source.pdf"):
            with self.subTest(url=url), self.assertRaises(discovery.BrowserNavigationError):
                discovery.valid_navigation(url)

    def test_pdf_normal_route_redirect_preserves_case_and_document(self):
        with patch.object(discovery, "navigate", return_value=DIRECT):
            self.assertEqual(discovery.navigate_pdf(PDF), DIRECT)
        with patch.object(discovery, "navigate", return_value=DIRECT.replace(DOC_ID, OTHER_ID)):
            with self.assertRaisesRegex(discovery.BrowserNavigationError, "identity"):
                discovery.navigate_pdf(PDF)

    def test_case_inventory_deduplicates_and_prefers_case_label(self):
        rows = [dict(url=CARD, name="", value="", description=""),
                dict(url=CARD, name="А00–123/2023", value="", description=""),
                dict(url="https://kad.arbitr.ru/", name="", value="", description="")]
        result = discovery.cases(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["case_number"], "А00-123/2023")
        self.assertEqual(result[0]["case_id"], CASE_ID)

    def test_challenge_and_rate_limit_are_not_retried(self):
        for text, reason in (("CAPTCHA", "challenge"), ("проверка, что вы не робот", "challenge"), ("Ошибка 429", "rate_limited")):
            with self.subTest(reason=reason), patch.object(discovery, "ax", return_value=[dict(role="AXStaticText", name="", value=text, description="")]) as reader:
                with self.assertRaises(discovery.AccessChallenge) as raised:
                    discovery.snapshot(CARD)
                self.assertEqual(raised.exception.reason, reason)
                self.assertEqual(reader.call_count, 1)

    def test_feed_is_private_strict_transport_schema_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            candidate = dict(source_id="synthetic-source", source_url=PDF, channel="kad-browser-pdf", case_id=CASE_ID,
                             authority="owner_assertion", caption="PRIVATE_SYNTHETIC_CAPTION", provenance={"private": True})
            discovery.append_feed(root, [candidate, candidate])
            discovery.append_feed(root, [candidate])
            path = root / "feeds/kad.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertNotIn("caption", rows[0])
            self.assertNotIn("provenance", rows[0])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_act_selection_requires_judicial_filter_and_same_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            scope = dict(role="AXStaticText", name="", value="Судебные акты", description="", path=[1])
            valid = dict(role="AXLink", name="Определение", value="", description="", url=PDF)
            wrong = dict(valid, url=PDF.replace(CASE_ID, OTHER_ID))
            with patch.object(discovery, "navigate", return_value=CARD), patch.object(discovery, "snapshot", side_effect=[[scope], [valid, wrong]]), patch.object(discovery, "ax") as action, patch.object(discovery.time, "sleep"):
                result = discovery.card_candidates(dict(card_url=CARD, case_id=CASE_ID, case_number="А00-123/2023"), root)
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0]["provenance"]["judicial_act_filter_requested"])
            self.assertFalse(result[0]["provenance"]["judicial_act_filter_confirmed"])
            self.assertFalse(result[0]["provenance"]["complete_case_history"])
            action.assert_called_once_with("press", CARD, [1], "Судебные акты")
