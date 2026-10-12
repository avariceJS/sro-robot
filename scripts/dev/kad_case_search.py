#!/usr/bin/env python3
"""Search KAD and open one verified case card in visible Safari.

This is a standalone tool, not a web-app task or worker. By default it uses
Safari's built-in WebDriver and the public search form, then verifies the exact
case card. Explicit --existing-safari --card-url mode instead asks ordinary
Safari to open a known public card; the operator must verify its identity.
It does not open documents or print party data. WebDriver stops on challenges.

WebDriver prerequisite: in Safari's Develop menu, open Developer Settings and enable
Allow Remote Automation, or run `safaridriver --enable` yourself once. This
script never changes Safari's security settings. Existing-Safari mode does not
require Remote Automation. See
`python scripts/dev/kad_case_search.py --help`.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

SEARCH_URL = "https://kad.arbitr.ru/"
CASE_RE = re.compile(r"^[АA]\d+-\d+/\d{4}$", re.IGNORECASE)
CARD_PATH_RE = re.compile(
    r"^/Card/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
CHALLENGE_MARKERS = (
    "captcha",
    "капча",
    "проверка, что вы не робот",
    "доступ ограничен",
    "access denied",
    "too many requests",
)
SERVICE_ERROR_MARKERS = (
    "ошибка 429",
    "ошибка получения информации с сервиса",
)


class SearchStopped(RuntimeError):
    """The safe public UI search could not complete."""


def _validate_card_url(url: str) -> str:
    """Accept only a public KAD card URL with a canonical UUID path."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise SearchStopped("Card URL is not a valid public KAD card URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "kad.arbitr.ru"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in url)
        or not CARD_PATH_RE.fullmatch(parsed.path)
    ):
        raise SearchStopped("Card URL is not a valid public KAD card URL")
    return url


def _normalize_case_number(case_number: str) -> str:
    if case_number and case_number[0] in "AаАa":
        return "А" + case_number[1:]
    return case_number


def _request_existing_safari_card(case_number: str, card_url: str) -> None:
    """Ask the user's existing Safari session to open a supplied public card."""
    case_number = _normalize_case_number(case_number)
    if not CASE_RE.fullmatch(case_number):
        raise SearchStopped("Enter a case number in the supported format")
    safe_url = _validate_card_url(card_url)
    if sys.platform != "darwin":
        raise SearchStopped("Existing Safari mode is available only on macOS")
    try:
        subprocess.run(
            ["/usr/bin/open", "-a", "Safari", safe_url],
            check=True,
            timeout=15,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        raise SearchStopped("Could not request the existing Safari session to open the card") from None
    print("browser_open_requested=true")
    print("verification=operator")


class SafariWebDriver:
    """Small standard-library WebDriver client for Apple's SafariDriver."""

    def __init__(self, port: int) -> None:
        self.base_url = f"http://127.0.0.1:{port}"
        self.process: subprocess.Popen[str] | None = None
        self.session_id: str | None = None
        self.pointer_scale: tuple[float, float] | None = None

    def _request(
        self, method: str, path: str, payload: dict | None = None, *, timeout: int = 15
    ) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as parse_exc:
                raise SearchStopped("Safari WebDriver returned an unreadable error") from parse_exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            reason = getattr(exc, "reason", None)
            detail = str(reason or exc)
            raise SearchStopped(
                f"SafariDriver request {method} {path} at {self.base_url} failed: {detail}"
            ) from exc
        value = result.get("value", {})
        if isinstance(value, dict) and value.get("error"):
            error = value["error"]
            detail = str(value.get("message", "")).strip()
            detail = f": {detail[:240]}" if detail else ""
            if error in {"session not created", "unknown error"}:
                raise SearchStopped(
                    "Safari refused WebDriver startup"
                    + (f": {detail[:240]}" if detail else ".")
                    + " Enable Remote Automation in Safari's Develop menu; the script does not alter browser settings."
                )
            raise SearchStopped(f"Safari WebDriver action failed ({error}){detail}")
        return result

    def start(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        safaridriver = shutil.which("safaridriver")
        if not safaridriver:
            raise SearchStopped("SafariDriver is available only on macOS with Safari installed")
        self.process = subprocess.Popen(
            [safaridriver, "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                details = ""
                if self.process.stderr:
                    details = self.process.stderr.read().strip()[-500:]
                suffix = f" Driver message: {details}" if details else ""
                raise SearchStopped(
                    "SafariDriver exited before startup. Enable Develop > Developer Settings > "
                    "Allow Remote Automation, or run `safaridriver --enable` yourself once."
                    + suffix
                )
            try:
                self.base_url = f"http://127.0.0.1:{port}"
                self._request("GET", "/status")
                break
            except SearchStopped:
                time.sleep(0.2)
        else:
            raise SearchStopped(
                f"SafariDriver did not respond on 127.0.0.1:{port}. Check Safari's "
                "Allow Remote Automation setting; the script does not change it."
            )

        result = self._request(
            "POST",
            "/session",
            {
                "capabilities": {
                    "alwaysMatch": {
                        "browserName": "safari",
                        "pageLoadStrategy": "eager",
                    }
                }
            },
            timeout=90,
        )
        value = result.get("value", {})
        self.session_id = value.get("sessionId") or result.get("sessionId")
        if not self.session_id:
            raise SearchStopped("SafariDriver returned no session")

    def command(
        self, method: str, path: str, payload: dict | None = None, *, timeout: int = 15
    ) -> dict:
        if not self.session_id:
            raise SearchStopped("Safari session is not active")
        return self._request(method, f"/session/{self.session_id}{path}", payload, timeout=timeout)

    def close(self) -> None:
        if self.session_id:
            try:
                self._request("DELETE", f"/session/{self.session_id}")
            except SearchStopped:
                pass
            self.session_id = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


def _element(driver: SafariWebDriver, using: str, value: str) -> str:
    result = driver.command("POST", "/element", {"using": using, "value": value})
    found = result.get("value", {})
    element_id = found.get("element-6066-11e4-a52e-4f735466cecf")
    if not element_id:
        raise SearchStopped("Could not safely identify a required search control in KAD")
    return element_id


def _check_page(driver: SafariWebDriver) -> str:
    url = driver.command("GET", "/url").get("value", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "kad.arbitr.ru":
        raise SearchStopped("Navigation left the public KAD site; stopped")
    body = driver.command("POST", "/execute/sync", {
        "script": "return document.body ? document.body.innerText : '';",
        "args": [],
    }).get("value", "")
    if any(marker in body.casefold() for marker in CHALLENGE_MARKERS):
        raise SearchStopped("KAD presented an access challenge; stopped without bypassing it")
    if any(marker in body.casefold() for marker in SERVICE_ERROR_MARKERS):
        raise SearchStopped("KAD returned a visible 429/service error; stopped without retrying")
    return body


def _wait_for_page_ready(driver: SafariWebDriver, timeout: float = 30) -> None:
    """Wait for Safari's eager navigation to finish page and same-origin scripts."""
    deadline = time.monotonic() + timeout
    last_signature: tuple[str, ...] | None = None
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        state = driver.command("POST", "/execute/sync", {
            "script": "const s=Array.from(document.scripts).map(e=>e.src).filter(Boolean).filter(u=>new URL(u).hostname===location.hostname).sort(),r=new Set(performance.getEntriesByType('resource').map(e=>e.name)); return {readyState:document.readyState,scripts:s,loaded:s.every(u=>r.has(u))};",
            "args": [],
        }).get("value", {})
        signature = tuple(state.get("scripts", []))
        if signature != last_signature:
            last_signature = signature
            stable_since = time.monotonic()
        if state.get("loaded") and time.monotonic() - stable_since >= 2:
            return
        time.sleep(0.25)
    raise SearchStopped(
        "KAD page scripts did not settle before the safe search timeout: "
        + json.dumps({"readyState": state.get("readyState"), "scriptCount": len(state.get("scripts", [])), "scriptsLoaded": state.get("loaded")})
    )


def _dismiss_blocking_notice(driver: SafariWebDriver) -> bool:
    """Close KAD's informational release notice through its visible close link."""
    visible_script = """const e = document.querySelector('.b-promo_notification-popup_wrapper');
        if (!e) return false;
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden';"""
    visible = driver.command("POST", "/execute/sync", {
        "script": visible_script,
        "args": [],
    }).get("value", False)
    if not visible:
        return False

    close_link = _element(
        driver,
        "css selector",
        ".b-promo_notification-popup_wrapper a.b-promo_notification-popup-close",
    )
    # This must be a trusted visible pointer click: closing the notice by
    # synthetic DOM click hides it but can leave KAD's interactive search inert.
    scale = _pointer_scale(driver)
    _click_visible_element(
        driver, close_link, pointer_id="kad-notice-close-mouse", scale=scale
    )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        visible = driver.command("POST", "/execute/sync", {
            "script": visible_script,
            "args": [],
        }).get("value", False)
        if not visible:
            return True
        time.sleep(0.1)
    raise SearchStopped("KAD's informational notice stayed open after its close control was clicked")


def _pointer_scale(driver: SafariWebDriver) -> tuple[float, float]:
    """Convert SafariDriver's backing-store coordinates to DOM CSS pixels."""
    if driver.pointer_scale is not None:
        return driver.pointer_scale
    dpr = driver.command("POST", "/execute/sync", {
        "script": "return window.devicePixelRatio;",
        "args": [],
    }).get("value")
    if not isinstance(dpr, (int, float)) or dpr <= 0:
        raise SearchStopped("Safari did not report a usable display scale")
    # On this Mac SafariDriver injects pointer coordinates at backing scale 2,
    # while DOM hit-testing uses devicePixelRatio CSS pixels (observed 1.5).
    scale_x = scale_y = 2 / dpr
    if not (0.5 <= scale_x <= 2):
        raise SearchStopped("Safari pointer coordinate scale is outside the safe expected range")
    driver.pointer_scale = (scale_x, scale_y)
    return driver.pointer_scale


def _click_visible_element(
    driver: SafariWebDriver,
    element: str,
    *,
    pointer_id: str,
    scale: tuple[float, float],
) -> tuple[dict, list[dict]]:
    state = driver.command("POST", "/execute/sync", {
        "script": "const e=arguments[0],r=e.getBoundingClientRect(),h=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2); window.__kadPointerEvents=[]; for(const n of ['pointerdown','pointerup','mousedown','mouseup','click']) { document.addEventListener(n,v=>window.__kadPointerEvents.push({type:v.type,trusted:v.isTrusted,target:v.target.tagName,clientX:v.clientX,clientY:v.clientY,onElement:v.target===e||e.contains(v.target),defaultPrevented:v.defaultPrevented}),{once:true,capture:true}); if(n==='click') document.addEventListener(n,v=>window.__kadPointerEvents.push({type:'click-bubble',target:v.target.tagName,defaultPrevented:v.defaultPrevented}),{once:true}); } return {visible:r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden',disabled:!!e.disabled,x:r.x,y:r.y,width:r.width,height:r.height,hitTag:h?.tagName||'',hitMatches:h===e||e.contains(h)};",
        "args": [{"element-6066-11e4-a52e-4f735466cecf": element}],
    }).get("value", {})
    if not state.get("visible") or state.get("disabled") or not state.get("hitMatches"):
        raise SearchStopped("A required KAD control is not visibly clickable")
    click_x = round((state["x"] + state["width"] / 2) / scale[0])
    click_y = round((state["y"] + state["height"] / 2) / scale[1])
    driver.command("POST", "/actions", {
        "actions": [{
            "type": "pointer",
            "id": pointer_id,
            "parameters": {"pointerType": "mouse"},
            "actions": [
                {"type": "pointerMove", "duration": 0, "origin": "viewport", "x": click_x, "y": click_y},
                {"type": "pointerDown", "button": 0},
                {"type": "pointerUp", "button": 0},
            ],
        }]
    }, timeout=30)
    audit = driver.command("POST", "/execute/sync", {
        "script": "return window.__kadPointerEvents;",
        "args": [],
    }).get("value", [])
    if not any(event.get("type") == "click" and event.get("trusted") and event.get("onElement") for event in audit):
        raise SearchStopped(
            "Safari received the pointer action outside the intended KAD control: "
            + json.dumps({"state": state, "clickPoint": [click_x, click_y], "events": audit}, ensure_ascii=False)[:1200]
        )
    return state, audit


def _result_snapshot_script() -> str:
    """Return only case tokens and row identity, never party text."""
    return r"""const rows = Array.from(document.querySelectorAll('tr, [role="row"]'));
        window.__kadRowIds ||= new WeakMap();
        window.__kadNextRowId ||= 1;
        const rowIds = rows.map(row => {
          if (!window.__kadRowIds.has(row)) window.__kadRowIds.set(row, window.__kadNextRowId++);
          return window.__kadRowIds.get(row);
        });
        const rowCaseTokens = rows.map(row =>
          Array.from((row.innerText || '').matchAll(/(^|[^A-Za-zА-Яа-яЁё0-9])([АA]\d+-\d+\/\d{2,4}(?:-\d+)?)(?![A-Za-zА-Яа-яЁё0-9/-])/g), m => m[2])
            .map(token => token.replace(/^A/, 'А'))
        );
        const bodyText = document.body?.innerText || '';
        const countMatch = bodyText.match(/Найдено\s+(\d+)\s+дел\w*/i);
        return {rowIds, rowCaseTokens, resultCount: countMatch ? countMatch[1] : null,
          searchPromptVisible: bodyText.includes('Укажите атрибуты дела для поиска')};"""


def _snapshot_changed(before: dict, after: dict) -> bool:
    """Whether the result view differs without comparing or returning party text."""
    return any(
        before.get(key) != after.get(key)
        for key in ("rowIds", "rowCaseTokens", "resultCount", "searchPromptVisible")
    )


def _matching_case_rows(snapshot: dict, case_number: str) -> int:
    return len(_matching_case_row_ids(snapshot, case_number))


def _matching_case_row_ids(snapshot: dict, case_number: str) -> set[int]:
    canonical_query = case_number.replace("A", "А")
    row_ids = snapshot.get("rowIds", [])
    row_tokens = snapshot.get("rowCaseTokens", [])
    matching: set[int] = set()
    for index, tokens in enumerate(row_tokens):
        if index < len(row_ids) and isinstance(tokens, list) and canonical_query in tokens:
            matching.add(row_ids[index])
    return matching


def _fresh_matching_case_rows(before: dict, after: dict, case_number: str) -> int:
    """Count exact rows newly present or newly matching after the click."""
    return len(
        _matching_case_row_ids(after, case_number)
        - _matching_case_row_ids(before, case_number)
    )


def _completed_result_count(snapshot: dict) -> int | None:
    value = snapshot.get("resultCount")
    if not isinstance(value, str) or not value.isdecimal():
        return None
    return int(value)


def _result_outcome(before: dict, after: dict, case_number: str) -> int:
    """Accept only a changed result view with a visible completed count."""
    if not _snapshot_changed(before, after):
        raise SearchStopped(
            "KAD's visible result state is unchanged; cached rows cannot confirm completion"
        )
    completed_count = _completed_result_count(after)
    if completed_count is None:
        raise SearchStopped("KAD changed its result view but showed no completed result count")
    if after.get("searchPromptVisible"):
        raise SearchStopped("KAD still shows the search prompt; result completion is unconfirmed")
    exact_rows = _matching_case_rows(after, case_number)
    fresh_exact_rows = _fresh_matching_case_rows(before, after, case_number)
    if (
        completed_count == 0
        and exact_rows == 0
        and before.get("resultCount") != after.get("resultCount")
    ):
        return 0
    if completed_count > 0 and fresh_exact_rows > 0:
        return exact_rows
    raise SearchStopped("KAD's visible result count did not agree with exact case rows")


def _runtime_failure_message(runtime_errors: list[dict]) -> str:
    """Classify browser errors without exposing exception text or stacks."""
    if any(error.get("kind") == "wasm" for error in runtime_errors):
        return "KAD's WebAssembly initialization failed before results"
    return "KAD's page reported a JavaScript runtime failure before results"


def _exact_result_card_link(driver: SafariWebDriver, case_number: str) -> tuple[str, str]:
    result = driver.command("POST", "/execute/sync", {
        "script": r"""const q=arguments[0].replace(/^A/, 'А');
          const rows=Array.from(document.querySelectorAll('tr, [role="row"]'));
          const tokens=text=>Array.from(text.matchAll(/(^|[^A-Za-zА-Яа-яЁё0-9])([АA]\d+-\d+\/\d{2,4}(?:-\d+)?)(?![A-Za-zА-Яа-яЁё0-9/-])/g),m=>m[2].replace(/^A/,'А'));
          const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
          const exact=rows.filter(row=>tokens(row.innerText||'').includes(q));
          if(exact.length!==1)return {rowCount:exact.length,linkCount:0,link:null};
          const links=Array.from(exact[0].querySelectorAll('a[href]')).filter(a=>{
            if(!visible(a)||(a.innerText||'').replace(/\s+/g,' ').trim()!==q)return false;
            try{const u=new URL(a.href,location.href);return u.protocol==='https:'&&u.hostname==='kad.arbitr.ru'&&(!u.port||u.port==='443')&&!u.username&&!u.password&&!u.search&&!u.hash&&/^\/Card\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(u.pathname);}catch{return false;}
          });
          return {rowCount:exact.length,linkCount:links.length,link:links.length===1?links[0]:null};""",
        "args": [case_number],
    }).get("value", {})
    if result.get("rowCount") != 1:
        raise SearchStopped("KAD did not show one exact case row; card opening stopped")
    if result.get("linkCount") != 1:
        raise SearchStopped("The exact KAD case row did not have one public card link")
    element = result.get("link")
    element_id = (
        element.get("element-6066-11e4-a52e-4f735466cecf")
        if isinstance(element, dict)
        else None
    )
    if not element_id:
        raise SearchStopped("Safari could not identify the exact case card link")
    href = driver.command("GET", f"/element/{element_id}/attribute/href").get("value", "")
    return _validate_card_url(href), element_id


def _verify_case_card(driver: SafariWebDriver, case_number: str) -> None:
    deadline = time.monotonic() + 15
    last_state = {
        "titlePresent": False,
        "titleMatches": False,
        "headerMatches": False,
        "electronicCaseVisible": False,
    }
    while time.monotonic() < deadline:
        _check_page(driver)
        current_url = driver.command("GET", "/url").get("value", "")
        try:
            _validate_card_url(current_url)
        except SearchStopped:
            time.sleep(0.2)
            continue
        verified = driver.command("POST", "/execute/sync", {
        "script": r"""const q=arguments[0].replace(/^[AaАа]/,'А');
          const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
          const tokens=text=>Array.from(text.matchAll(/(^|[^A-Za-zА-Яа-яЁё0-9])([АA]\d+-\d+\/\d{2,4}(?:-\d+)?)(?![A-Za-zА-Яа-яЁё0-9/-])/g),m=>m[2].replace(/^A/,'А'));
          const title=document.title.trim();
          const titlePresent=title.length>0;
          const titleMatches=title.replace(/^[AaАа]/,'А')===q;
          const headerMatches=Array.from(document.querySelectorAll('h1,h2,h3,h4,[role="heading"]')).some(e=>visible(e)&&tokens(e.innerText||'').includes(q));
          const electronicCaseVisible=/Электронное дело/i.test(document.body?.innerText||'');
          return {titlePresent,titleMatches,headerMatches,electronicCaseVisible};""",
            "args": [case_number],
        }).get("value", {})
        last_state = {
            key: verified.get(key) is True
            for key in ("titlePresent", "titleMatches", "headerMatches", "electronicCaseVisible")
        }
        if all(last_state[key] for key in ("titleMatches", "headerMatches", "electronicCaseVisible")):
            return
        time.sleep(0.2)
    raise SearchStopped("KAD card verification did not pass " + json.dumps(last_state, sort_keys=True))


def _click_case_card_link(driver: SafariWebDriver, element_id: str) -> None:
    before = driver.command("GET", "/window/handles").get("value", [])
    driver.command("POST", f"/element/{element_id}/click", timeout=30)
    deadline = time.monotonic() + 15
    switched = False
    while time.monotonic() < deadline:
        after = driver.command("GET", "/window/handles").get("value", [])
        new_handles = [handle for handle in after if handle not in before]
        if len(new_handles) > 1:
            raise SearchStopped("KAD opened multiple new windows for the case card")
        if new_handles and not switched:
            driver.command("POST", "/window", {"handle": new_handles[0]})
            switched = True
        current_url = driver.command("GET", "/url").get("value", "")
        try:
            parsed = urlparse(current_url)
            port = parsed.port
        except ValueError as exc:
            raise SearchStopped("Navigation left the public KAD site; stopped") from exc
        if current_url and (
            parsed.scheme != "https"
            or parsed.hostname != "kad.arbitr.ru"
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise SearchStopped("Navigation left the public KAD site; stopped")
        if parsed.path.startswith("/Card/"):
            _validate_card_url(current_url)
            return
        time.sleep(0.2)
    raise SearchStopped("KAD did not open a public case card")


def _hold_card(hold_seconds: float, wait_for_enter: bool) -> None:
    if wait_for_enter and sys.stdin.isatty():
        print("case_card_verified=true; press Enter to close Safari", flush=True)
        sys.stdin.readline()
    elif hold_seconds:
        print(f"case_card_verified=true; holding_visible_card_seconds={hold_seconds:g}", flush=True)
        time.sleep(hold_seconds)
    else:
        print("case_card_verified=true; Safari will close immediately (use --hold-seconds to inspect)", flush=True)


def _search_case_in_driver(driver: SafariWebDriver, case_number: str) -> tuple[int, dict]:
    driver.command(
        "POST",
        "/timeouts",
        {"implicit": 0, "pageLoad": 90000, "script": 15000},
    )
    driver.command("POST", "/url", {"url": SEARCH_URL}, timeout=100)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        body = _check_page(driver)
        if "Номер дела" in body:
            break
        time.sleep(0.2)
    else:
        raise SearchStopped("KAD search form did not load in Safari")

    _wait_for_page_ready(driver)
    if _dismiss_blocking_notice(driver):
        print("closed_kad_notice=true", flush=True)
    time.sleep(2)

    field = _element(driver, "css selector", 'input[placeholder="например, А50-5568/08"]')
    driver.command("POST", f"/element/{field}/clear")
    driver.command("POST", f"/element/{field}/value", {"text": case_number, "value": list(case_number)})
    scale = _pointer_scale(driver)
    case_add = _element(driver, "css selector", "#sug-cases .tag > i.b-icon.add")
    _click_visible_element(driver, case_add, pointer_id="kad-case-filter-add-mouse", scale=scale)
    selected_filter = driver.command("POST", "/execute/sync", {
        "script": "const e=document.querySelector('#sug-cases'); return {inputValues:Array.from(e?.querySelectorAll('input')||[]).map(i=>i.value),addedTags:e?.querySelectorAll('.tag.added').length||0};",
        "args": [],
    }).get("value", {})
    if case_number not in selected_filter.get("inputValues", []) or not selected_filter.get("addedTags"):
        raise SearchStopped("KAD did not add the case number as an active search criterion")

    button = _element(driver, "css selector", 'button[type="submit"]')
    before_search = driver.command("POST", "/execute/sync", {
        "script": _result_snapshot_script(), "args": [],
    }).get("value", {})
    driver.command("POST", "/execute/sync", {
        "script": "window.__kadRuntimeErrors=[]; const classify=s=>/wasm|webassembly|__wbindgen_start/i.test(String(s||''))?'wasm':'javascript'; addEventListener('error',e=>window.__kadRuntimeErrors.push({kind:classify(String(e.message||'')+' '+String(e.filename||''))})); addEventListener('unhandledrejection',e=>window.__kadRuntimeErrors.push({kind:classify(e.reason?.message||e.reason||'')})); return true;",
        "args": [],
    })
    _click_visible_element(driver, button, pointer_id="kad-search-mouse", scale=scale)

    deadline = time.monotonic() + 20
    matches: int | None = None
    after_search: dict = {}
    last_result_error: SearchStopped | None = None
    while time.monotonic() < deadline:
        _check_page(driver)
        runtime_errors = driver.command("POST", "/execute/sync", {
            "script": "return window.__kadRuntimeErrors||[];", "args": [],
        }).get("value", [])
        if runtime_errors:
            raise SearchStopped(_runtime_failure_message(runtime_errors))
        after_search = driver.command("POST", "/execute/sync", {
            "script": _result_snapshot_script(), "args": [],
        }).get("value", {})
        if _snapshot_changed(before_search, after_search):
            completed_count = _completed_result_count(after_search)
            try:
                outcome = _result_outcome(before_search, after_search, case_number)
            except SearchStopped as exc:
                last_result_error = exc
            else:
                if completed_count == 0 or _fresh_matching_case_rows(
                    before_search, after_search, case_number
                ) > 0:
                    matches = outcome
                    break
        time.sleep(0.25)

    if matches is None:
        if not _snapshot_changed(before_search, after_search):
            raise SearchStopped("KAD's visible result state did not change before the diagnostic timeout")
        if last_result_error:
            raise last_result_error
        matches = _result_outcome(before_search, after_search, case_number)
    print("result_state_changed=true")
    print(f"exact_case_rows={matches}")
    print(f"kad_result_count={after_search.get('resultCount', 'not_shown')}")
    return matches, after_search


def search_case(
    case_number: str,
    *,
    card_url: str | None = None,
    open_card: bool = False,
    hold_seconds: float = 0,
    wait_for_enter: bool = False,
) -> int:
    """Search KAD and optionally open a verified exact case card.

    `card_url` skips search and opens only a validated public `/Card/<UUID>` URL.
    """
    case_number = _normalize_case_number(case_number)
    if not CASE_RE.fullmatch(case_number):
        raise SearchStopped("Enter a case number in the supported format")
    if not math.isfinite(hold_seconds) or hold_seconds < 0 or hold_seconds > 600:
        raise SearchStopped("Hold duration must be between 0 and 600 seconds")
    if card_url is not None:
        if not open_card:
            raise SearchStopped("A direct card URL requires card-opening mode")
        card_url = _validate_card_url(card_url)

    driver = SafariWebDriver(port=0)
    try:
        driver.start()
        driver.command("POST", "/timeouts", {"implicit": 0, "pageLoad": 90000, "script": 15000})
        if card_url:
            driver.command("POST", "/url", {"url": card_url}, timeout=100)
            _check_page(driver)
            _wait_for_page_ready(driver)
            _check_page(driver)
            _verify_case_card(driver, case_number)
            count = 1
        else:
            count, _snapshot = _search_case_in_driver(driver, case_number)
            if open_card:
                if count != 1:
                    raise SearchStopped("KAD must show one exact case row before opening a card")
                _card_href, element_id = _exact_result_card_link(driver, case_number)
                _click_case_card_link(driver, element_id)
                _verify_case_card(driver, case_number)
        if open_card:
            _hold_card(hold_seconds, wait_for_enter)
        return count
    finally:
        driver.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_number", help="Case number to verify in the public KAD case card")
    parser.add_argument(
        "--search-only",
        action="store_true",
        help="Search and report exact case rows without opening the case card",
    )
    parser.add_argument(
        "--card-url",
        help="Open this exact public https://kad.arbitr.ru/Card/<UUID> URL without searching",
    )
    parser.add_argument(
        "--existing-safari",
        action="store_true",
        help="Ask the existing macOS Safari session to open --card-url; an operator must verify the card",
    )
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=None,
        help="Keep a verified card visible this long in non-interactive runs (0-600; default 0)",
    )
    args = parser.parse_args()
    if args.existing_safari:
        if not args.card_url:
            parser.error("--existing-safari requires --card-url")
        if args.search_only:
            parser.error("--existing-safari cannot be combined with --search-only")
        if args.hold_seconds is not None:
            parser.error("--existing-safari cannot be combined with --hold-seconds")
        try:
            _request_existing_safari_card(args.case_number, args.card_url)
        except SearchStopped as exc:
            print(f"search_stopped: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.card_url and args.search_only:
        parser.error("--card-url opens a card and cannot be combined with --search-only")
    try:
        count = search_case(
            args.case_number,
            card_url=args.card_url,
            open_card=not args.search_only,
            hold_seconds=args.hold_seconds if args.hold_seconds is not None else 0,
            wait_for_enter=not args.search_only and sys.stdin.isatty(),
        )
    except SearchStopped as exc:
        print(f"search_stopped: {exc}", file=sys.stderr)
        return 2
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
