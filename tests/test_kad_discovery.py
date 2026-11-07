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
    def test_owned_discovery_skips_wrong_or_missing_official_case_year_before_browser_case_load(self):
        for number in ('А40-123/2023', ''):
            with self.subTest(number=number), self.owned_root() as root:
                case = {'case_id': CASE_ID, 'card_url': CARD, 'case_number': number, 'year': '2026'}
                output = io.StringIO()
                args = ['kad_discovery.py', '--root', str(root), '--year', '2026', '--owned-browser', '--download']
                with patch.object(sys, 'argv', args), patch.object(discovery, '_owned_browser_preflight'), patch.object(discovery, 'reconcile_owned_browser'), patch.object(discovery, 'restore_owner_focus'), patch.object(discovery, 'search', return_value=[case]), patch.object(discovery, 'card_candidates', side_effect=AssertionError('unqualified browser navigation')), contextlib.redirect_stdout(output):
                    self.assertEqual(discovery.main(), 0)
                self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['skipped_case_year'], 1)

    def test_fresh_qualification_uses_official_serial_year_and_rich_generic_caption(self):
        case = {'case_number': 'А40-123/2026', 'year': '2023'}
        rich = {'caption': 'Признать должника банкротом и ввести реализацию имущества'}
        weaker = {'caption': 'Признать должника банкротом'}
        procedural = {'caption': 'Назначить судебное заседание'}
        selected, skipped = discovery.qualify_candidates(case, [procedural, rich, weaker], case_year=2026, max_caption_rank=0)
        self.assertEqual(selected, [rich])
        self.assertEqual(skipped['skipped_caption'], 2)
        self.assertEqual(discovery.official_case_year({'case_number':'СИП-5/25'}), 2025)
        for number in ('А40-123/2023', '', 'query-2026', 'А40-123/026'):
            selected, skipped = discovery.qualify_candidates({'case_number':number, 'year':'2026'}, [rich], case_year=2026, max_caption_rank=0)
            self.assertEqual(selected, [])
            self.assertEqual(skipped['skipped_case_year'], 1)

    def test_fresh_enqueue_cannot_import_unqualified_historical_feed_entries(self):
        from scripts.dev.quality import corpus_acquisition as acquisition
        import hashlib
        with self.owned_root() as root:
            def row(url, year):
                return {'source_id':hashlib.sha256(url.encode()).hexdigest(), 'source_url':url,
                    'channel':'kad-browser-pdf', 'authority':'owner_assertion', 'case_id':CASE_ID,
                    'case_number':'А40-123/'+str(year), 'court':'unknown', 'year':str(year),
                    'act_type':'decision', 'participant_kind':'unknown'}
            stale = row(PDF.replace(DOC_ID, OTHER_ID), 2023)
            selected = row(PDF, 2026)
            discovery.append_feed(root, [stale])
            self.assertEqual(discovery.enqueue_selected(root, [selected])['added'], 1)
            queued = acquisition.load_queue(root)['items']
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0]['source_url'], PDF)
            self.assertEqual(queued[0]['metadata']['case_number'], 'А40-123/2026')
            self.assertEqual(list((root/'staging').iterdir()), [])

    def test_month_search_bounds_include_leap_year_and_preserve_full_year_default(self):
        self.assertEqual(discovery._search_dates(2026), ("01.01.2026", "31.12.2026"))
        self.assertEqual(discovery._search_dates(2026, 3), ("01.03.2026", "31.03.2026"))
        self.assertEqual(discovery._search_dates(2026, 2), ("01.02.2026", "28.02.2026"))
        self.assertEqual(discovery._search_dates(2024, 2), ("01.02.2024", "29.02.2024"))
        for month in (0, 13, True):
            with self.subTest(month=month), self.assertRaises(discovery.BrowserNavigationError):
                discovery._search_dates(2026, month)

    def test_bankruptcy_caption_ranking_selects_richer_act_without_case_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            scope = dict(role="AXStaticText", name="", value="Судебные акты", description="", path=[1])
            ordinary = dict(role="AXLink", name="Назначить заседание", value="", description="", url=PDF)
            decision = dict(ordinary, name="Признать гражданина банкротом и ввести реализацию имущества", url=PDF.replace(DOC_ID, OTHER_ID))
            with patch.object(discovery, "navigate", return_value=CARD), patch.object(discovery, "snapshot", side_effect=[[scope], [ordinary, decision]]), patch.object(discovery, "ax"), patch.object(discovery.time, "sleep"):
                rows = discovery.card_candidates(dict(card_url=CARD, case_id=CASE_ID, case_number="А00-7/2026"), root)
            self.assertEqual(rows[0]["source_url"], decision["url"])
            self.assertFalse(rows[0]["provenance"]["judicial_act_filter_confirmed"])

    @contextlib.contextmanager
    def owned_root(self):
        from scripts.dev.quality import corpus_acquisition as acquisition
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            acquisition.init(root)
            with patch.object(discovery, "_BROWSER_TARGET", None), patch.object(discovery, "_OWNED_BROWSER_ROOT", None):
                yield root

    def owned_lease(self, root, url=CARD, pending=None):
        value = {"window": "11", "tab": "22", "url": url}
        if pending:
            value["pending_navigation"] = pending
        discovery._save_owned_lease(root, value)
        return value

    def test_owned_source_and_native_holds_precede_any_browser_creation(self):
        from scripts.dev.quality import corpus_acquisition as acquisition
        for hold in ("disabled", "native", "coordinator"):
            with self.subTest(hold=hold), self.owned_root() as root:
                if hold == "disabled":
                    state = acquisition.load_queue(root)
                    state["source_controls"]["kad-browser-pdf"] = {"disabled": True}
                    discovery.write_json(root / "queue.json", state)
                elif hold == "native":
                    discovery.write_json(root / "inbox/.native-save-pending.json", {})
                else:
                    discovery.write_json(root / "coordinator/held.json", {"status": "unknown"})
                with patch.object(discovery, "_jxa", side_effect=AssertionError("browser used before hold")):
                    with self.assertRaises(discovery.AccessChallenge):
                        discovery.ensure_owned_browser(root)

    def test_broken_lease_symlink_never_triggers_window_creation(self):
        with self.owned_root() as root:
            path = discovery._owned_lease_path(root)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.symlink_to(path.parent / 'missing')
            with patch.object(discovery, '_jxa', side_effect=AssertionError('browser creation on invalid custody')):
                with self.assertRaises((ValueError, discovery.BrowserNavigationError)):
                    discovery.ensure_owned_browser(root)

    def test_owned_creation_challenge_is_global_durable_and_window_not_recreated(self):
        from scripts.dev.quality import corpus_acquisition as acquisition
        with self.owned_root() as root:
            result = {"window": "11", "tab": "22", "url": "https://kad.arbitr.ru/", "title": "CAPTCHA", "loaded": True, "previous": {}}
            with patch.object(discovery, "_jxa", return_value=result) as driver:
                with self.assertRaises(discovery.AccessChallenge):
                    discovery.ensure_owned_browser(root)
                with self.assertRaises(discovery.AccessChallenge):
                    discovery.ensure_owned_browser(root)
            self.assertEqual(driver.call_count, 1)
            script = driver.call_args.args[0]
            self.assertLess(script.index("c.activate();"), script.index("c.Window().make()"))
            self.assertTrue(acquisition.load_queue(root)["source_controls"]["kad-browser-pdf"]["disabled"])

    def test_current_owner_focus_is_hashed_and_owned_window_does_not_replace_it(self):
        with self.owned_root() as root:
            target = self.owned_lease(root)
            previous = {"bundle": "com.google.Chrome", "name": "Chrome", "window": "33", "tab": "44", "url": "http://127.0.0.1:8767/"}
            discovery._save_owner_focus(root, target, previous)
            path = root / 'discovery' / discovery.FOCUS_RESTORE
            original = path.read_bytes()
            self.assertNotIn(previous['url'], original.decode())
            discovery._save_owner_focus(root, target, dict(previous, window='11', tab='22', url=CARD))
            self.assertEqual(path.read_bytes(), original)
            discovery._save_owner_focus(root, target, dict(previous, window='11', tab='44'))
            self.assertEqual(json.loads(path.read_text())['tab'], '44')

    def test_restore_cross_instance_uses_proven_pids_without_foreign_tab_reads(self):
        with self.owned_root() as root:
            target = self.owned_lease(root)
            target['native_pid'] = 123
            discovery._save_owned_lease(root, target)
            discovery._save_owner_focus(root, target, {'bundle': 'com.google.Chrome', 'native_pid': 456,
                'window': '33', 'tab': '44', 'url': 'http://127.0.0.1:8767/'})
            with patch.object(discovery, '_read_owned_target', side_effect=AssertionError('foreign instance tab read')), patch.object(discovery, '_jxa', return_value=True) as driver:
                self.assertTrue(discovery.restore_owner_focus(root))
            script = driver.call_args.args[0]
            self.assertIn('Number(ps[0].unixId())!==target.native_pid', script)
            self.assertIn('se.processes.whose({unixId:previous.native_pid})', script)
            self.assertNotIn('Application(previous.bundle)', script)

    def test_restore_without_proven_source_pid_does_not_touch_browser(self):
        with self.owned_root() as root:
            target = self.owned_lease(root)
            discovery._save_owner_focus(root, target, {'bundle': 'com.google.Chrome', 'native_pid': 456,
                'window': '33', 'tab': '44', 'url': 'http://127.0.0.1:8767/'})
            with patch.object(discovery, '_jxa', side_effect=AssertionError('unproven focus restore')):
                self.assertFalse(discovery.restore_owner_focus(root))

    def test_owned_body_challenge_is_checked_before_leaving_initial_page(self):
        from scripts.dev.quality import corpus_acquisition as acquisition
        with self.owned_root() as root:
            result = {"window": "11", "tab": "22", "url": "https://kad.arbitr.ru/", "title": "KAD", "loaded": True, "previous": {}}
            with patch.object(discovery, "_jxa", return_value=result) as driver, patch.object(discovery, "snapshot", side_effect=discovery.AccessChallenge(observed=True)):
                with self.assertRaises(discovery.AccessChallenge):
                    discovery.ensure_owned_browser(root)
            self.assertEqual(driver.call_count, 1)
            self.assertTrue(acquisition.load_queue(root)["source_controls"]["kad-browser-pdf"]["disabled"])

    def test_owned_pending_transition_reconciles_only_same_document_alias(self):
        with self.owned_root() as root:
            self.owned_lease(root, CARD, {"from": CARD, "to": PDF})
            observed = {"window": "11", "tab": "22", "url": DIRECT, "title": "synthetic", "loading": False}
            with patch.object(discovery, "_read_owned_target", return_value=observed):
                discovery.reconcile_owned_browser(root)
            lease = discovery._load_owned_lease(root)
            self.assertEqual(lease["url"], DIRECT)
            self.assertNotIn("pending_navigation", lease)
            self.owned_lease(root, CARD, {"from": CARD, "to": PDF})
            original = discovery._owned_lease_path(root).read_bytes()
            observed["url"] = DIRECT.replace(DOC_ID, OTHER_ID)
            with patch.object(discovery, "_read_owned_target", return_value=observed):
                with self.assertRaises(discovery.BrowserNavigationError):
                    discovery.reconcile_owned_browser(root)
            self.assertEqual(discovery._owned_lease_path(root).read_bytes(), original)

    def test_owned_navigation_timeout_adopts_only_verified_completed_transition(self):
        with self.owned_root() as root:
            self.owned_lease(root, "https://kad.arbitr.ru/")
            before = {"window": "11", "tab": "22", "url": "https://kad.arbitr.ru/", "title": "synthetic", "loading": False}
            after = dict(before, url=CARD)
            with patch.object(discovery, "_jxa", side_effect=[before, discovery.BrowserNavigationError("timeout"), after]) as driver:
                self.assertEqual(discovery.navigate(CARD, root=root), CARD)
            self.assertEqual(discovery._load_owned_lease(root)["url"], CARD)
            script = driver.call_args_list[1].args[0]
            self.assertNotIn("c.windows[0].activeTab.id()", script)
            self.assertIn("String(win.id())!==target.window", script)
            self.assertIn("String(tab.url())!==target.url", script)
            self.assertIn("c.windows.byId(Number(target.window))", script)

    def test_owned_lease_never_substitutes_front_review_tab(self):
        with self.owned_root() as root:
            self.owned_lease(root)
            observed = {"window": "11", "tab": "22", "url": "http://127.0.0.1:8767/", "title": "private review", "loading": False}
            with patch.object(discovery, "_read_owned_target", return_value=observed), patch.object(discovery, "_jxa", side_effect=AssertionError("must not create replacement")):
                with self.assertRaises(discovery.BrowserNavigationError):
                    discovery.ensure_owned_browser(root)

    def test_owned_ax_requires_focus_and_pins_native_focused_window(self):
        with self.owned_root() as root, patch.object(discovery, "_OWNED_BROWSER_ROOT", root):
            result = subprocess.CompletedProcess([], 0, stdout=b"[]")
            with patch.object(discovery, "focus_owned_browser") as focus, patch.object(discovery, "ax_binary", return_value=Path('/synthetic/helper')), patch.object(discovery.subprocess, "run", return_value=result) as driver:
                self.assertEqual(discovery.ax("snapshot", CARD), [])
            focus.assert_called_once_with(root, CARD)
            self.assertEqual(driver.call_args.args[0][-1], "--focused")

    def test_owned_focus_uses_native_key_window_verification_not_just_jxa_order(self):
        with self.owned_root() as root:
            self.owned_lease(root)
            observed = {"window": "11", "tab": "22", "url": CARD, "title": "synthetic", "window_title": "PRIVATE_SYNTHETIC_TITLE", "bounds": [10, 30, 1010, 830], "loading": False}
            with patch.object(discovery, '_read_owned_target', return_value=observed), patch.object(discovery, '_remember_owner_focus', return_value={}), patch.object(discovery, 'ax_binary', return_value=Path('/synthetic/helper')), patch.object(discovery.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=b'{"native_pid":123}')) as native_focus:
                discovery.focus_owned_browser(root, CARD)
            self.assertEqual(discovery._load_owned_lease(root)['native_pid'], 123)
            command = native_focus.call_args.args[0]
            self.assertEqual(command[1], 'focus-owned')
            self.assertNotIn(observed['window_title'], command)
            self.assertEqual(json.loads(native_focus.call_args.kwargs['input']), {'title': observed['window_title'], 'bounds': observed['bounds']})
            with patch.object(discovery, '_read_owned_target', return_value=observed), patch.object(discovery, '_remember_owner_focus', return_value={}), patch.object(discovery, 'ax_binary', return_value=Path('/synthetic/helper')), patch.object(discovery.subprocess, 'run', return_value=subprocess.CompletedProcess([], 10)):
                with self.assertRaises(discovery.BrowserNavigationError):
                    discovery.focus_owned_browser(root, CARD)

    def test_owned_hidden_pdf_menu_reveal_is_once_and_still_requires_native_pid_proof(self):
        with self.owned_root() as root:
            target = self.owned_lease(root)
            target['native_pid'] = 123
            discovery._save_owned_lease(root, target)
            observed = dict(target, title='synthetic', window_title='synthetic', bounds=[10, 30, 1010, 830], loading=False)
            good = subprocess.CompletedProcess([], 0, stdout=b'{"native_pid":123}')
            for result, accepted in ((good, True), (subprocess.CompletedProcess([], 0, stdout=b'{"native_pid":456}'), False)):
                with patch.object(discovery, '_read_owned_target', return_value=observed), patch.object(discovery, '_remember_owner_focus', return_value={}), patch.object(discovery, '_reveal_owned_window_menu') as reveal, patch.object(discovery, 'ax_binary', return_value=Path('/synthetic/helper')), patch.object(discovery.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 9), result]) as driver:
                    if accepted:
                        self.assertEqual(discovery.focus_owned_browser(root, CARD), 123)
                    else:
                        with self.assertRaises(discovery.BrowserNavigationError):
                            discovery.focus_owned_browser(root, CARD)
                    reveal.assert_called_once()
                    self.assertEqual(driver.call_count, 2)
                    self.assertEqual(json.loads(driver.call_args.kwargs['input'])['native_pid'], 123)

    def test_window_menu_reveal_requires_positive_proven_pid_and_exact_unique_item(self):
        with patch.object(discovery, '_jxa', return_value=True) as driver:
            with self.assertRaises(discovery.BrowserNavigationError):
                discovery._reveal_owned_window_menu({'window':'11','tab':'22'}, 'synthetic')
            driver.assert_not_called()
            discovery._reveal_owned_window_menu({'window':'11','tab':'22','native_pid':123}, 'synthetic')
            script = driver.call_args.args[0]
            self.assertIn('se.processes.whose({unixId:target.native_pid})', script)
            self.assertIn('String(items[j].name())===title', script)
            self.assertIn('matches.length!==1', script)
            self.assertEqual(script.count('matches[0].click()'), 1)

    def test_owned_focus_reveals_only_exact_owned_window_before_native_proof(self):
        with self.owned_root() as root:
            target = self.owned_lease(root)
            observed = dict(target, title='synthetic', window_title='synthetic',
                            bounds={'x': 10, 'y': 30, 'width': 1000, 'height': 800}, loading=False)
            with patch.object(discovery, '_jxa', return_value=observed) as driver:
                discovery._read_owned_target(target, focus=True)
            script = driver.call_args.args[0]
            self.assertIn('win.visible=true;win.minimized=false;', script)
            self.assertIn('c.windows.byId(Number(target.window))', script)
            self.assertNotIn('c.windows[0]', script)

    def test_chrome_rectangle_dictionary_is_normalized_without_private_output(self):
        self.assertEqual(discovery._window_bounds({'x': -20, 'y': 30, 'width': 1000, 'height': 800}), [-20, 30, 980, 830])
        self.assertEqual(discovery._window_bounds([10, 30, 1010, 830]), [10, 30, 1010, 830])
        for value in ({'x': 0, 'y': 0, 'width': -1, 'height': 100}, [0, 0, float('inf'), 20], [True, 0, 100, 100]):
            with self.subTest(value=value), self.assertRaises(discovery.BrowserNavigationError):
                discovery._window_bounds(value)

    def test_owned_focus_never_raises_a_window_while_native_save_is_unresolved(self):
        with self.owned_root() as root:
            self.owned_lease(root)
            discovery.write_json(root / 'inbox/.native-save-pending.json', {})
            with patch.object(discovery, '_jxa', side_effect=AssertionError('focus attempted under unknown Save')):
                with self.assertRaises(discovery.AccessChallenge):
                    discovery.focus_owned_browser(root, CARD)

    def test_retry_reuses_loaded_same_pdf_without_reload_and_keeps_tab_guards(self):
        target = {"window": "11", "tab": "22", "url": DIRECT}
        with patch.object(discovery, "_BROWSER_TARGET", None), patch.object(discovery, "_jxa", side_effect=[target, {"url": DIRECT, "title": "synthetic.pdf"}]) as driver:
            self.assertEqual(discovery.navigate_pdf(PDF), DIRECT)
        script = driver.call_args_list[1].args[0]
        self.assertNotIn("tab.url=", script)
        self.assertIn('String(tab.url())!==target.url', script)
        self.assertIn('win.tabs.byId(Number(target.tab))', script)
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
