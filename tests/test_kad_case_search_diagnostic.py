"""Offline result-state tests for the standalone Safari KAD diagnostic."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

import scripts.dev.kad_case_search as diagnostic
from scripts.dev.kad_case_search import (
    SearchStopped,
    _click_case_card_link,
    _exact_result_card_link,
    _matching_case_rows,
    _normalize_case_number,
    _result_outcome,
    _result_snapshot_script,
    _runtime_failure_message,
    _snapshot_changed,
    _validate_card_url,
    _verify_case_card,
)


CASE = "А99-12345/2099"
CARD_URL = "https://kad.arbitr.ru/Card/00000000-0000-0000-0000-000000000099"


class FakeDriver:
    def __init__(self, snapshots: list[dict], runtime_errors: list[dict] | None = None) -> None:
        self.snapshots = list(snapshots)
        self.runtime_errors = runtime_errors or []
        self.closed = False
        self.calls: list[tuple[str, str, dict | None]] = []
        self.current_url = "https://kad.arbitr.ru/"
        self.card_href = CARD_URL
        self.card_link_result = {
            "rowCount": 1,
            "linkCount": 1,
            "link": {"element-6066-11e4-a52e-4f735466cecf": "synthetic-link"},
        }
        self.card_state = {
            "titlePresent": True,
            "titleMatches": True,
            "headerMatches": True,
            "electronicCaseVisible": True,
        }
        self.window_handles = ["synthetic-window"]
        self.open_new_window_on_click = False

    def start(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def command(self, method: str, path: str, payload: dict | None = None, **kwargs: object) -> dict:
        self.calls.append((method, path, payload))
        if path == "/execute/sync":
            script = (payload or {}).get("script", "")
            if script == _result_snapshot_script():
                snapshot = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
                return {"value": snapshot}
            if "const exact=rows.filter" in script:
                return {"value": self.card_link_result}
            if "const titleMatches" in script:
                return {"value": self.card_state}
            if "window.__kadRuntimeErrors||[]" in script:
                return {"value": self.runtime_errors}
            if "const e=document.querySelector('#sug-cases')" in script:
                return {"value": {"inputValues": [CASE], "addedTags": 1}}
        if path == "/url" and method == "GET":
            return {"value": self.current_url}
        if path == "/url" and method == "POST":
            self.current_url = (payload or {}).get("url", self.current_url)
            return {"value": None}
        if path == "/window/handles":
            return {"value": self.window_handles}
        if path.endswith("/click") and method == "POST":
            if self.open_new_window_on_click:
                self.window_handles = [*self.window_handles, "synthetic-new-window"]
                self.current_url = CARD_URL
            return {"value": None}
        if path.startswith("/element/") and path.endswith("/attribute/href"):
            return {"value": self.card_href}
        if path == "/window" and method == "POST":
            return {"value": None}
        if path == "/element":
            return {"value": {"element-6066-11e4-a52e-4f735466cecf": "synthetic-element"}}
        return {"value": True}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 0.3
        return self.now

    def sleep(self, _seconds: float) -> None:
        return None


class KadCaseSearchDiagnosticTests(unittest.TestCase):
    def test_case_number_normalizes_latin_and_lowercase_initials(self) -> None:
        self.assertEqual(_normalize_case_number("A99-12345/2099"), CASE)
        self.assertEqual(_normalize_case_number("а99-12345/2099"), CASE)

    def test_existing_safari_mode_requests_supplied_card_without_readback(self) -> None:
        output = io.StringIO()
        with patch.object(diagnostic.sys, "platform", "darwin"), \
                patch.object(diagnostic.subprocess, "run") as open_command, \
                contextlib.redirect_stdout(output):
            diagnostic._request_existing_safari_card("A99-12345/2099", CARD_URL)

        open_command.assert_called_once()
        args, kwargs = open_command.call_args
        self.assertEqual(args[0], ["/usr/bin/open", "-a", "Safari", CARD_URL])
        self.assertTrue(kwargs["check"])
        self.assertEqual(output.getvalue().splitlines(), [
            "browser_open_requested=true",
            "verification=operator",
        ])

    def test_existing_safari_cli_never_starts_safaridriver(self) -> None:
        output = io.StringIO()
        argv = [
            "kad_case_search.py",
            CASE,
            "--existing-safari",
            "--card-url",
            CARD_URL,
        ]
        with patch.object(diagnostic.sys, "platform", "darwin"), \
                patch.object(diagnostic.sys, "argv", argv), \
                patch.object(diagnostic.subprocess, "run"), \
                patch.object(diagnostic, "SafariWebDriver") as driver, \
                contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 0)

        driver.assert_not_called()
        self.assertIn("verification=operator", output.getvalue())

    def test_existing_safari_rejects_invalid_inputs_before_launch(self) -> None:
        with patch.object(diagnostic.sys, "platform", "darwin"), \
                patch.object(diagnostic.subprocess, "run") as open_command:
            with self.assertRaises(SearchStopped):
                diagnostic._request_existing_safari_card("invalid", CARD_URL)
            with self.assertRaises(SearchStopped):
                diagnostic._request_existing_safari_card(CASE, CARD_URL.replace("/Card/", "/Document/"))

        open_command.assert_not_called()

    def test_existing_safari_cli_rejects_search_only_and_hold_options(self) -> None:
        for extra in (("--search-only",), ("--hold-seconds", "1")):
            argv = ["kad_case_search.py", CASE, "--existing-safari", "--card-url", CARD_URL, *extra]
            with self.subTest(extra=extra), \
                    patch.object(diagnostic.sys, "platform", "darwin"), \
                    patch.object(diagnostic.sys, "argv", argv), \
                    patch.object(diagnostic.subprocess, "run") as open_command, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    diagnostic.main()
                open_command.assert_not_called()

    def test_card_url_allows_only_public_card_path_and_https_443(self) -> None:
        self.assertEqual(_validate_card_url(CARD_URL), CARD_URL)
        url_443 = "https://kad.arbitr.ru:443/Card/00000000-0000-0000-0000-000000000099"
        self.assertEqual(_validate_card_url(url_443), url_443)
        for invalid in (
            CARD_URL.replace("https://", "http://"),
            CARD_URL.replace("kad.arbitr.ru", "example.org"),
            CARD_URL.replace("/Card/", "/Document/"),
            CARD_URL + "?x=1",
            CARD_URL + ";x=1",
            CARD_URL + " ",
            CARD_URL.replace("kad.arbitr.ru", "user@kad.arbitr.ru"),
            "https://kad.arbitr.ru:444/Card/00000000-0000-0000-0000-000000000099",
            "https://kad.arbitr.ru/Card/not-a-uuid",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(SearchStopped):
                _validate_card_url(invalid)

    def test_exact_result_card_link_requires_one_exact_row_and_visible_card_anchor(self) -> None:
        driver = FakeDriver([])
        self.assertEqual(_exact_result_card_link(driver, CASE), (CARD_URL, "synthetic-link"))

        driver.card_link_result = {"rowCount": 2, "linkCount": 0, "link": None}
        with self.assertRaisesRegex(SearchStopped, "one exact case row"):
            _exact_result_card_link(driver, CASE)

        driver.card_link_result = {
            "rowCount": 1,
            "linkCount": 1,
            "link": {"element-6066-11e4-a52e-4f735466cecf": "synthetic-link"},
        }
        driver.card_href = "https://other.example/Card/00000000-0000-0000-0000-000000000099"
        with self.assertRaisesRegex(SearchStopped, "valid public KAD card URL"):
            _exact_result_card_link(driver, CASE)

    def test_card_verification_requires_exact_title_header_and_electronic_case_label(self) -> None:
        driver = FakeDriver([])
        driver.current_url = CARD_URL
        with patch.object(diagnostic, "_check_page", return_value="safe card page"):
            _verify_case_card(driver, CASE)

        driver.card_state["titleMatches"] = False
        clock = FakeClock()
        with patch.object(diagnostic.time, "monotonic", side_effect=clock.monotonic), \
                patch.object(diagnostic.time, "sleep", side_effect=clock.sleep), \
                patch.object(diagnostic, "_check_page", return_value="case card"):
            with self.assertRaisesRegex(SearchStopped, "verification did not pass"):
                _verify_case_card(driver, CASE)

    def test_direct_card_mode_skips_search_and_closes_driver_on_mismatch(self) -> None:
        driver = FakeDriver([])
        driver.card_state["titleMatches"] = False
        clock = FakeClock()
        with patch.object(diagnostic, "SafariWebDriver", return_value=driver), \
                patch.object(diagnostic, "_wait_for_page_ready"), \
                patch.object(diagnostic, "_check_page", return_value="case card"), \
                patch.object(diagnostic.time, "monotonic", side_effect=clock.monotonic), \
                patch.object(diagnostic.time, "sleep", side_effect=clock.sleep):
            with self.assertRaisesRegex(SearchStopped, "verification did not pass"):
                diagnostic.search_case("A99-12345/2099", card_url=CARD_URL, open_card=True)

        self.assertTrue(driver.closed)
        self.assertIn(("POST", "/url", {"url": CARD_URL}), driver.calls)
        self.assertFalse(any(payload and payload.get("url") == diagnostic.SEARCH_URL for _, _, payload in driver.calls))

    def test_case_card_click_switches_to_a_unique_new_window(self) -> None:
        driver = FakeDriver([])
        driver.open_new_window_on_click = True
        _click_case_card_link(driver, "synthetic-link")

        self.assertIn(("POST", "/window", {"handle": "synthetic-new-window"}), driver.calls)

    def test_only_exact_case_tokens_count_as_matching_rows(self) -> None:
        snapshot = {
            "rowIds": [1, 2, 3, 4],
            "rowCaseTokens": [[CASE + "-1"], [CASE], [CASE + "0"], []],
        }

        self.assertEqual(_matching_case_rows(snapshot, CASE), 1)

    def test_changed_completed_result_with_exact_row_is_success(self) -> None:
        before = {"rowIds": [1], "rowCaseTokens": [["А99-1/2099"]], "resultCount": "1"}
        after = {"rowIds": [2], "rowCaseTokens": [[CASE]], "resultCount": "1"}

        self.assertTrue(_snapshot_changed(before, after))
        self.assertEqual(_result_outcome(before, after, CASE), 1)

    def test_changed_completed_zero_is_confirmed_empty_result(self) -> None:
        before = {"rowIds": [1], "rowCaseTokens": [[CASE]], "resultCount": "1", "searchPromptVisible": False}
        after = {"rowIds": [], "rowCaseTokens": [], "resultCount": "0", "searchPromptVisible": False}

        self.assertEqual(_result_outcome(before, after, CASE), 0)

    def test_unchanged_cached_matching_rows_are_not_fresh_success(self) -> None:
        snapshot = {"rowIds": [7], "rowCaseTokens": [[CASE]], "resultCount": "1", "searchPromptVisible": False}

        with self.assertRaisesRegex(SearchStopped, "result state is unchanged"):
            _result_outcome(snapshot, snapshot, CASE)

    def test_unrelated_new_row_does_not_refresh_a_cached_exact_row(self) -> None:
        before = {
            "rowIds": [7],
            "rowCaseTokens": [[CASE]],
            "resultCount": "1",
            "searchPromptVisible": False,
        }
        after = {
            "rowIds": [7, 8],
            "rowCaseTokens": [[CASE], ["А99-2/2099"]],
            "resultCount": "2",
            "searchPromptVisible": False,
        }

        with self.assertRaisesRegex(SearchStopped, "did not agree"):
            _result_outcome(before, after, CASE)

    def test_changed_view_without_visible_count_is_incomplete(self) -> None:
        before = {"rowIds": [], "rowCaseTokens": [], "resultCount": None, "searchPromptVisible": True}
        after = {"rowIds": [3], "rowCaseTokens": [[CASE]], "resultCount": None, "searchPromptVisible": True}

        with self.assertRaisesRegex(SearchStopped, "no completed result count"):
            _result_outcome(before, after, CASE)

    def test_positive_count_without_exact_case_row_is_not_reported_as_zero(self) -> None:
        before = {"rowIds": [], "rowCaseTokens": [], "resultCount": None, "searchPromptVisible": True}
        after = {"rowIds": [2], "rowCaseTokens": [[CASE + "-1"]], "resultCount": "1", "searchPromptVisible": False}

        with self.assertRaisesRegex(SearchStopped, "did not agree"):
            _result_outcome(before, after, CASE)

    def test_runtime_errors_are_classified_without_echoing_raw_details(self) -> None:
        generic = _runtime_failure_message([{"kind": "javascript", "message": "private detail"}])
        wasm = _runtime_failure_message([{"kind": "wasm"}])

        self.assertIn("JavaScript runtime failure", generic)
        self.assertNotIn("private detail", generic)
        self.assertIn("WebAssembly initialization failed", wasm)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable; skips browser snapshot fixture")
    def test_javascript_snapshot_extracts_bounded_case_tokens_only(self) -> None:
        script = _result_snapshot_script()
        fixture = (
            "const rows=["
            "{innerText:'А99-12345/2099'},"
            "{innerText:'А99-12345/2099-1'},"
            "{innerText:'А99-12345/20990'},"
            "{innerText:'XА99-12345/2099'},"
            "{innerText:'synthetic-party А99-12345/2099'}];"
            "const document={querySelectorAll:()=>rows,body:{innerText:'Найдено 5 дел'}};"
            "const window={};"
            f"const result=(new Function({json.dumps(script)}))();"
            "process.stdout.write(JSON.stringify(result));"
        )
        result = json.loads(subprocess.run(
            ["node", "-e", fixture], check=True, capture_output=True, text=True
        ).stdout)

        self.assertEqual(result["rowCaseTokens"], [[CASE], [CASE + "-1"], [], [], [CASE]])
        self.assertEqual(result["resultCount"], "5")
        self.assertNotIn("synthetic-party", json.dumps(result))

    def _run_search(self, snapshots: list[dict], *, page_behavior: str = "normal", runtime_errors: list[dict] | None = None) -> tuple[int | None, str, FakeDriver]:
        driver = FakeDriver(snapshots, runtime_errors)
        clock = FakeClock()
        page_calls = 0

        def check_page(_driver: object) -> str:
            nonlocal page_calls
            page_calls += 1
            if page_behavior == "429" and page_calls > 1:
                raise SearchStopped("KAD returned a visible 429/service error; stopped without retrying")
            return "Номер дела"

        output = io.StringIO()
        error: SearchStopped | None = None
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with patch.object(diagnostic, "SafariWebDriver", return_value=driver), \
                    patch.object(diagnostic, "_check_page", side_effect=check_page), \
                    patch.object(diagnostic, "_wait_for_page_ready"), \
                    patch.object(diagnostic, "_dismiss_blocking_notice", return_value=False), \
                    patch.object(diagnostic, "_element", return_value="synthetic-element"), \
                    patch.object(diagnostic, "_pointer_scale", return_value=(1.0, 1.0)), \
                    patch.object(diagnostic, "_click_visible_element", return_value=({}, [])), \
                    patch.object(diagnostic.time, "monotonic", side_effect=clock.monotonic), \
                    patch.object(diagnostic.time, "sleep", side_effect=clock.sleep):
                try:
                    count = diagnostic.search_case(CASE)
                except SearchStopped as exc:
                    error = exc
                    count = None
        if error:
            output.write(f"search_stopped: {error}\n")
        return count, output.getvalue(), driver

    def test_mocked_search_timeout_is_incomplete_and_closes_driver(self) -> None:
        unchanged = {"rowIds": [], "rowCaseTokens": [], "resultCount": None, "searchPromptVisible": True}
        count, output, driver = self._run_search([unchanged])

        self.assertIsNone(count)
        self.assertIn("visible result state did not change", output)
        self.assertTrue(driver.closed)

    def test_mocked_search_confirms_visible_zero_and_closes_driver(self) -> None:
        before = {"rowIds": [], "rowCaseTokens": [], "resultCount": None, "searchPromptVisible": True}
        after = {"rowIds": [], "rowCaseTokens": [], "resultCount": "0", "searchPromptVisible": False}
        count, output, driver = self._run_search([before, after])

        self.assertEqual(count, 0)
        self.assertIn("result_state_changed=true", output)
        self.assertTrue(driver.closed)

    def test_mocked_429_stops_without_retry_and_closes_driver(self) -> None:
        before = {"rowIds": [], "rowCaseTokens": [], "resultCount": None, "searchPromptVisible": True}
        count, output, driver = self._run_search([before], page_behavior="429")

        self.assertIsNone(count)
        self.assertIn("429/service error", output)
        self.assertTrue(driver.closed)

    def test_mocked_generic_runtime_failure_is_not_called_wasm(self) -> None:
        before = {"rowIds": [], "rowCaseTokens": [], "resultCount": None, "searchPromptVisible": True}
        count, output, driver = self._run_search(
            [before], runtime_errors=[{"kind": "javascript"}]
        )

        self.assertIsNone(count)
        self.assertIn("JavaScript runtime failure", output)
        self.assertNotIn("WebAssembly", output)
        self.assertTrue(driver.closed)


if __name__ == "__main__":
    unittest.main()
