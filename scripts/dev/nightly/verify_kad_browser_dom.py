#!/usr/bin/env python3
"""Exercise the production KAD DOM automation in local synthetic Chromium pages.

Every browser request is intercepted. Only in-memory fixtures for the exact
``kad.arbitr.ru`` origin are fulfilled; every other request is aborted and
causes the run to fail. This is not a live KAD, extension, or backend test.
"""

from __future__ import annotations

import argparse
import asyncio
from importlib.metadata import version
import json
from pathlib import Path
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "browser-extension" / "kad-helper"
CASE = "А50-12345/2026"
CARD_ID = "11111111-2222-4333-8444-555555555555"
CARD_URL = f"https://kad.arbitr.ru/Card/{CARD_ID}"
REQUESTED_DATE = "2026-02-10"


def search_fixture() -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>Картотека арбитражных дел</title>
<style>i.b-icon.add { display:inline-block; width:12px; height:12px; }</style></head>
<body>
  <main>
    <input placeholder="например, А50-5568/08">
    <div id="sug-cases"><div class="tag"><i class="b-icon add"></i></div></div>
    <div id="b-form-submit"><button type="submit">Найти</button></div>
    <div id="results"><p>Укажите атрибуты дела для поиска</p></div>
  </main>
  <script>
    window.syntheticSearchSubmissions = Number(localStorage.getItem('syntheticSearchSubmissions') || '0');
    const input = document.querySelector('input[placeholder="например, А50-5568/08"]');
    document.querySelector('#sug-cases .tag > i.b-icon.add').addEventListener('click', () => {
      const tag = document.createElement('span');
      tag.className = 'tag added';
      const committed = document.createElement('input');
      committed.value = input.value;
      tag.append(committed);
      document.querySelector('#sug-cases').append(tag);
      input.value = '';
    });
    document.querySelector('#b-form-submit button[type="submit"]').addEventListener('click', event => {
      event.preventDefault();
      window.syntheticSearchSubmissions += 1;
      localStorage.setItem('syntheticSearchSubmissions', String(window.syntheticSearchSubmissions));
      const committed = document.querySelector('#sug-cases .tag.added input').value;
      document.querySelector('#results').innerHTML = `
        <p>Найдено 1 дело</p>
        <table><tbody><tr><td>${committed}</td><td>
          <a href="__CARD_URL__">${committed}</a>
        </td></tr></tbody></table>`;
    });
  </script>
</body></html>""".replace("__CARD_URL__", CARD_URL)


def card_fixture() -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>__CASE__</title></head>
<body>
  <h1>__CASE__ · Арбитражный суд</h1>
  <section>
    <div class="b-chrono-item-header">
      <span class="b-case-instance-number">__CASE__</span>
      <button class="b-collapse js-collapse" type="button">Развернуть</button>
    </div>
    <div class="js-chrono-items-container" style="display:none">
      <article class="js-chrono-item" data-date="09.02.2026">
        <span class="case-date">09.02.2026</span>
        <a href="https://kad.arbitr.ru/Kad/PdfDocument/__CARD_ID__/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee/earlier.pdf">Ранее опубликованный акт.pdf</a>
      </article>
      <article class="js-chrono-item" data-date="10.02.2026">
        <span class="case-date">10.02.2026</span>
        <a href="https://kad.arbitr.ru/Kad/PdfDocument/__CARD_ID__/99999999-8888-4777-8666-555555555555/selected-act.pdf">Акт на выбранную дату.pdf</a>
      </article>
    </div>
  </section>
  <script>
    document.querySelector('.b-collapse.js-collapse').addEventListener('click', event => {
      event.currentTarget.closest('.b-chrono-item-header').classList.add('b-chrono-item-header-expanded');
      event.currentTarget.closest('.b-chrono-item-header').nextElementSibling.style.display = 'block';
    });
  </script>
</body></html>""".replace("__CASE__", CASE).replace("__CARD_ID__", CARD_ID)


async def run() -> dict[str, object]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit("The repository virtualenv must provide Playwright; no package install is attempted") from exc

    fixtures = {"/": search_fixture(), f"/Card/{CARD_ID}": card_fixture()}
    observed_requests: list[str] = []
    unexpected_requests: list[str] = []

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:
            raise SystemExit(f"Cached Playwright Chromium is required; no browser download is attempted: {type(exc).__name__}") from exc
        try:
            context = await browser.new_context()

            async def intercept(route) -> None:
                request_url = route.request.url
                parsed = urlparse(request_url)
                observed_requests.append(request_url)
                if parsed.scheme not in {"http", "https"} or parsed.hostname != "kad.arbitr.ru":
                    unexpected_requests.append(request_url)
                    await route.abort()
                    return
                fixture = fixtures.get(parsed.path)
                if route.request.is_navigation_request and fixture is not None:
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=fixture)
                    return
                # Any resource not explicitly described by a fixture is denied.
                await route.abort()

            await context.route("**/*", intercept)
            page = await context.new_page()
            await page.goto("https://kad.arbitr.ru/", wait_until="load")
            await page.add_script_tag(path=str(HELPER / "automation.js"))
            await page.add_script_tag(path=str(HELPER / "protocol.js"))

            search = await page.evaluate(
                """async ({caseNumber}) => {
                  return await SroKadAutomation.runSearch(document, caseNumber, {
                    timeoutMs: 3000, pollIntervalMs: 10, settleMs: 0,
                  });
                }""",
                {"caseNumber": CASE.replace("А", "A", 1)},
            )
            submissions = await page.evaluate("window.syntheticSearchSubmissions")
            if search.get("state") != "matched" or len(search.get("links", [])) != 1:
                raise AssertionError("Synthetic search did not return one exact case card")
            if submissions != 1:
                raise AssertionError(f"Expected exactly one search submission, observed {submissions}")
            if search["links"][0]["href"] != CARD_URL:
                raise AssertionError("Search selected a card outside the synthetic exact result")
            if search["links"][0]["label"] != CASE:
                raise AssertionError("Latin A was not normalized to the canonical Cyrillic case prefix")

            await page.goto(CARD_URL, wait_until="load")
            await page.add_script_tag(path=str(HELPER / "automation.js"))
            await page.add_script_tag(path=str(HELPER / "protocol.js"))
            card = await page.evaluate(
                """async ({caseNumber}) => {
                  return await SroKadAutomation.runCard(document, caseNumber, {
                    cardUrl: location.href,
                    timeoutMs: 3000, pollIntervalMs: 10,
                    settleMs: 0, candidateSettleMs: 30,
                  });
                }""",
                {"caseNumber": CASE},
            )
            if card.get("state") != "candidates" or len(card.get("candidates", [])) != 2:
                raise AssertionError("Card automation did not return both visible synthetic acts")
            selection = await page.evaluate(
                """({candidates, requestedDate}) => SroKadProtocol.documentSelection(
                  candidates.map(({href, label, actDate}) => ({url: href, label, actDate})),
                  requestedDate,
                  candidates[1].href,
                  false,
                )""",
                {"candidates": card["candidates"], "requestedDate": REQUESTED_DATE},
            )
            selected = selection.get("candidate")
            if not selected or selected.get("actDate") != REQUESTED_DATE:
                raise AssertionError("The requested synthetic act date did not validate the employee's candidate")
            if selection.get("reason") != "employee_selected_visible_document":
                raise AssertionError("The selected synthetic PDF did not retain employee selection provenance")
            if selected["url"] != card["candidates"][1]["href"]:
                raise AssertionError("Employee selection validated the wrong synthetic PDF")
            missing_date = await page.evaluate(
                """({candidates}) => SroKadProtocol.documentSelection(
                  candidates.map(({href, label, actDate}) => ({url: href, label, actDate})),
                  '2026-02-11',
                  candidates[1].href,
                  false,
                )""",
                {"candidates": card["candidates"]},
            )
            if missing_date != {"error": "invalid_selection"}:
                raise AssertionError("A non-matching requested date was not rejected explicitly")
            if not await page.locator(".b-chrono-item-header").evaluate(
                "element => element.classList.contains('b-chrono-item-header-expanded')"
            ):
                raise AssertionError("Matching chronology was not expanded")

            # Exercise the production content script across real page navigation
            # with an in-memory background-message adapter; no extension or API
            # endpoint is loaded in Chromium.
            content_context = await browser.new_context()
            await content_context.route("**/*", intercept)
            content_page = await content_context.new_page()
            content_messages: list[dict[str, object]] = []
            content_state: dict[str, object] = {"job": None}
            candidates_ready = asyncio.Event()

            async def runtime_message(message: dict[str, object]) -> dict[str, object]:
                content_messages.append(message)
                kind = message.get("type")
                job = content_state["job"]
                if kind == "begin":
                    job = {
                        "requestId": message["requestId"],
                        "caseNumber": message["caseNumber"],
                        "actDate": message["actDate"],
                        "phase": "searching",
                        "startedAt": int(time.time() * 1000),
                        "bridge": {"serviceTab": 1},
                    }
                elif kind == "card" and isinstance(job, dict):
                    job = {**job, "phase": "card", "cardUrl": message["cardUrl"]}
                elif kind == "candidates" and isinstance(job, dict):
                    job = {**job, "phase": "documents", "candidates": message["candidates"],
                           "candidateInventoryComplete": message["inventoryComplete"]}
                    candidates_ready.set()
                elif kind == "tick":
                    return {"ok": True}
                elif kind == "get" and isinstance(job, dict):
                    return {"job": job}
                elif kind == "stop":
                    job = {**job, "phase": "stopped", "reason": message.get("reason")}
                else:
                    raise AssertionError(f"Unexpected synthetic helper message: {kind}")
                content_state["job"] = job
                return {"job": job}

            await content_page.expose_function("syntheticRuntimeMessage", runtime_message)
            await content_page.add_init_script(
                """window.browser = {runtime: {
                  getManifest: () => ({version: '0.2.12'}),
                  sendMessage: message => window.syntheticRuntimeMessage(message),
                  onMessage: {addListener: () => {}},
                }};"""
            )
            run_id = "22222222-3333-4444-8555-666666666666"
            launch_url = (
                "https://kad.arbitr.ru/#sro_robot=1"
                f"&case={CASE}&run={run_id}&act_date={REQUESTED_DATE}"
            )
            await content_page.goto(launch_url, wait_until="load")
            card_navigation = asyncio.create_task(content_page.wait_for_url(CARD_URL, timeout=5000))
            for filename in ("protocol.js", "automation.js", "content.js"):
                await content_page.add_script_tag(path=str(HELPER / filename))
            await card_navigation
            await content_page.wait_for_load_state("load")
            for filename in ("protocol.js", "automation.js", "content.js"):
                await content_page.add_script_tag(path=str(HELPER / filename))
            await asyncio.wait_for(candidates_ready.wait(), timeout=5)
            if content_state["job"].get("candidateInventoryComplete") is not False:
                raise AssertionError("The observed chronology was incorrectly marked complete")
            if any(message.get("type") in {"select", "acquire"} for message in content_messages):
                raise AssertionError("The content script transferred a PDF before employee selection")
            selected_url = card["candidates"][1]["href"]
            message_types = [str(message.get("type")) for message in content_messages]
            expected_chain = ["begin", "card", "get", "candidates"]
            positions = [message_types.index(kind) if kind in message_types else -1 for kind in expected_chain]
            if any(position < 0 for position in positions) or positions != sorted(positions):
                raise AssertionError(f"Production content-script handoff was incomplete: {message_types}")
            if selection["reason"] != "employee_selected_visible_document" or selected["url"] != selected_url:
                raise AssertionError("The employee's synthetic PDF choice was not validated with provenance")
            if content_state["job"].get("selectedUrl") is not None:
                raise AssertionError("The helper runtime recorded a PDF before employee choice")
            if await content_page.evaluate("Number(localStorage.getItem('syntheticSearchSubmissions'))") != 1:
                raise AssertionError("The content script submitted the synthetic search more than once")
            if content_page.url != CARD_URL:
                raise AssertionError("The bridge-mode content script unexpectedly navigated away from the card")
            await content_page.get_by_role("button", name="Остановить").click()
            await content_page.get_by_role("status").get_by_text("Поиск остановлен сотрудником.").wait_for()
            message_types = [str(message.get("type")) for message in content_messages]
            if content_state["job"].get("phase") != "stopped":
                raise AssertionError("Stopping during employee review did not persist a stopped run")
            if any(kind in message_types for kind in ("select", "acquire")):
                raise AssertionError("Stopped run advanced to document acquisition")

            if unexpected_requests:
                raise AssertionError("Browser attempted an out-of-fixture network request")
            return {
                "scenario": "synthetic Chromium DOM search-to-employee-selected-PDF",
                "browser": "Playwright Chromium headless",
                "playwright_version": version("playwright"),
                "chromium_version": browser.version,
                "search_state": search["state"],
                "search_submissions": submissions,
                "card_candidates": len(card["candidates"]),
                "selected_date_match": True,
                "selection_reason": selection["reason"],
                "chronology_expanded": True,
                "production_content_script_messages": expected_chain,
                "synthetic_extension_runtime_adapter": True,
                "employee_selection_required_before_acquisition": True,
                "cancel_button_stopped_waiting_employee_review": True,
                "stopped_run_never_reached_document_acquisition": True,
                "out_of_fixture_network_requests": 0,
                "intercepted_kad_requests": len(observed_requests),
                "network_route_policy": "intercept-all; fixture-only fulfillment; abort all other requests",
                "external_kad_or_backend_contacted": False,
                "full_extension_backend_import_test": False,
            }
        finally:
            await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = asyncio.run(run())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
