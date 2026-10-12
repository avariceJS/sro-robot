#!/usr/bin/env python3
"""Read-only KAD case search in visible Safari, for operator diagnostics only.

This is not part of SRO Robot's product workflow. It uses Safari's built-in
WebDriver and the public search form. It does not open documents or print party
data, and it stops if KAD presents an access challenge.

Prerequisite: in Safari's Develop menu, open Developer Settings and enable
Allow Remote Automation, or run `safaridriver --enable` yourself once. This
script never changes Safari's security settings. See
`python scripts/dev/kad_case_search.py --help`.
"""

from __future__ import annotations

import argparse
import json
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
CASE_RE = re.compile(r"^А\d+-\d+/\d{4}$", re.IGNORECASE)
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


def search_case(case_number: str) -> int:
    """Search KAD's visible public page in Safari and report exact result rows."""
    if not CASE_RE.fullmatch(case_number):
        raise SearchStopped("Use a case number such as А41-61776/2026 (Cyrillic А preferred)")

    driver = SafariWebDriver(port=0)
    try:
        driver.start()
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
        print("case_entered=true", flush=True)
        scale = _pointer_scale(driver)
        case_add = _element(driver, "css selector", "#sug-cases .tag > i.b-icon.add")
        _click_visible_element(
            driver, case_add, pointer_id="kad-case-filter-add-mouse", scale=scale
        )
        selected_filter = driver.command("POST", "/execute/sync", {
            "script": "const e=document.querySelector('#sug-cases'); return {inputValues:Array.from(e?.querySelectorAll('input')||[]).map(i=>i.value),addedTags:e?.querySelectorAll('.tag.added').length||0};",
            "args": [],
        }).get("value", {})
        if case_number not in selected_filter.get("inputValues", []) or not selected_filter.get("addedTags"):
            raise SearchStopped("KAD did not add the case number as an active search criterion")
        print("case_filter_added=true", flush=True)

        button = _element(driver, "css selector", 'button[type="submit"]')
        driver.command("POST", "/execute/sync", {
            "script": "window.__kadResourcesBeforeSearch=new Set(performance.getEntriesByType('resource').map(e=>e.name)); window.__kadRuntimeErrors=[]; addEventListener('error',e=>window.__kadRuntimeErrors.push({message:String(e.message||'error').slice(0,180),file:String(e.filename||'').split('/').slice(-1)[0],line:e.lineno,column:e.colno,stack:String(e.error?.stack||'').slice(0,600)})); addEventListener('unhandledrejection',e=>window.__kadRuntimeErrors.push({message:String(e.reason?.message||e.reason||'rejection').slice(0,180),stack:String(e.reason?.stack||'').slice(0,600)})); return true;",
            "args": [],
        })
        button_state, click_audit = _click_visible_element(
            driver, button, pointer_id="kad-search-mouse", scale=scale
        )
        print(f"search_button_state={json.dumps(button_state, ensure_ascii=False)}", flush=True)
        print(f"search_button_pointer_events={json.dumps(click_audit, ensure_ascii=False)}", flush=True)

        script = """const q = arguments[0];
            return Array.from(document.querySelectorAll('tr, [role="row"]')).filter(row => {
              const text = (row.innerText || '').replace(/\\s+/g, ' ').trim();
              return text.includes(q);
            }).length;"""
        deadline = time.monotonic() + 20
        matches = 0
        body = ""
        while time.monotonic() < deadline:
            body = _check_page(driver)
            runtime_errors = driver.command("POST", "/execute/sync", {
                "script": "return window.__kadRuntimeErrors||[];",
                "args": [],
            }).get("value", [])
            if runtime_errors:
                print(f"kad_runtime_errors={json.dumps(runtime_errors, ensure_ascii=False)}", flush=True)
                raise SearchStopped(
                    "KAD's Safari WebAssembly runtime failed before the case-search request: "
                    + str(runtime_errors[0].get("message", "unknown runtime error"))
                )
            matches = driver.command("POST", "/execute/sync", {
                "script": script,
                "args": [case_number],
            }).get("value", 0)
            if matches:
                break
            time.sleep(0.25)

        print(f"browser=Safari")
        print(f"query={case_number}")
        print(f"exact_case_rows={matches}")
        result_label = re.search(r"Найдено\s+(\d+)\s+дел", body, re.IGNORECASE)
        print(f"kad_result_count={result_label.group(1) if result_label else 'not_shown'}")
        print(f"search_form_prompt_still_visible={'Укажите атрибуты дела для поиска' in body}")
        return matches
    finally:
        driver.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_number", help="Case number to search in the public KAD UI")
    args = parser.parse_args()
    try:
        count = search_case(args.case_number)
    except SearchStopped as exc:
        print(f"search_stopped: {exc}", file=sys.stderr)
        return 2
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
