#!/usr/bin/env python3
"""Ordinary KAD browser discovery. All case metadata remains private on this Mac."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
if __name__ == "__main__":
    # Queue adapters import this canonical name. One browser lease/URL cursor
    # must be shared with the CLI instead of constructing a second module.
    sys.modules["scripts.dev.quality.kad_discovery"] = sys.modules[__name__]
from scripts.dev.quality import download_current_kad as native
from scripts.dev.quality.court_dataset import private_root, write_json, verify_storage

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CASE = re.compile(rf"^https://kad\.arbitr\.ru/Card/({UUID})$", re.I)
CASE_NUMBER = re.compile(r"(?:[АA]\d{1,3}|СИП)[-–]\d+/\d{2,4}", re.I)
DEFAULT_ROOT = Path.home() / "Library/Application Support/SRO Robot/ocr-research/corpus-2026-11-07"
_BROWSER_TARGET = None


class BrowserNavigationError(Exception):
    def __init__(self, reason="navigation"):
        self.reason = reason if reason in {"timeout", "navigation", "identity", "unavailable", "unsupported"} else "navigation"
        super().__init__(self.reason)


class AccessChallenge(BrowserNavigationError):
    def __init__(self, reason="challenge"):
        self.reason = reason if reason in {"challenge", "rate_limited", "access_denied"} else "challenge"
        Exception.__init__(self, self.reason)


def valid_navigation(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "kad.arbitr.ru" or parsed.username or parsed.password:
        raise BrowserNavigationError("identity")
    if parsed.path != "/" and not CASE.fullmatch(url):
        try:
            native.validate_pdf_url(url)
        except native.DownloadError:
            raise BrowserNavigationError("identity") from None
    return url


def _jxa(script, timeout=35):
    try:
        result = subprocess.run(["osascript", "-l", "JavaScript", "-"], input=script, text=True,
                                capture_output=True, timeout=timeout, check=True)
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise BrowserNavigationError("timeout") from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise BrowserNavigationError("navigation") from None


def navigate(url):
    global _BROWSER_TARGET
    valid_navigation(url)
    if _BROWSER_TARGET is None:
        target = _jxa('''var c=Application("com.google.Chrome");if(!c.running()||c.windows().length===0)throw Error("unavailable");var w=c.windows[0],t=w.activeTab;JSON.stringify({window:String(w.id()),tab:String(t.id()),url:String(t.url())});''')
        valid_navigation(target["url"])
        _BROWSER_TARGET = target
    script = '''ObjC.import("Foundation");
var c=Application("com.google.Chrome");
if(!c.running()||c.windows().length===0)throw Error("unavailable");
var target=TARGET;
var wins=c.windows().filter(function(w){return String(w.id())===target.window;});
if(wins.length!==1)throw Error("identity");
var win=wins[0],tabs=win.tabs(),index=-1;
for(var i=0;i<tabs.length;i++)if(String(tabs[i].id())===target.tab)index=i;
if(index<0||String(tabs[index].url())!==target.url)throw Error("identity");
win.activeTabIndex=index+1;win.index=1;
var tab=tabs[index],id=target.tab;
tab.url=URL;
var end=Date.now()+25000;
while(tab.loading()&&Date.now()<end){$.NSThread.sleepForTimeInterval(.1);}
if(tab.loading())throw Error("timeout");
if(String(c.windows[0].activeTab.id())!==id)throw Error("identity");
JSON.stringify({url:String(tab.url()),title:String(tab.title())});'''.replace("var target=TARGET;", "var target=" + json.dumps(_BROWSER_TARGET) + ";").replace("tab.url=URL;", "tab.url=" + json.dumps(url) + ";")
    result = _jxa(script)
    title = result["title"].lower()
    if re.search(r"captcha|проверка доступа|подтвердите.*не.*робот", title):
        raise AccessChallenge()
    if re.search(r"too many requests|слишком много запросов|^429", title):
        raise AccessChallenge("rate_limited")
    if re.search(r"access denied|доступ ограничен|^403", title):
        raise AccessChallenge("access_denied")
    valid_navigation(result["url"])
    _BROWSER_TARGET["url"] = result["url"]
    return result["url"]


def pdf_identity(url):
    native.validate_pdf_url(url)
    return tuple(re.findall(UUID, urlsplit(url).path, re.I)[:2])


def navigate_pdf(url):
    expected = pdf_identity(url)
    actual = navigate(url)
    try:
        if pdf_identity(actual) != expected:
            raise BrowserNavigationError("identity")
    except native.DownloadError:
        raise BrowserNavigationError("identity") from None
    return actual


def ax_binary():
    source = ROOT / "scripts/dev/quality/kad_ax.swift"
    directory = ROOT / ".local-run/kad-discovery"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = directory / "kad-ax"
    pin = directory / "source.sha256"
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    if not binary.exists() or not pin.exists() or pin.read_text() != fingerprint:
        result = subprocess.run(["/usr/bin/swiftc", "-O", str(source), "-o", str(binary)],
                                capture_output=True, timeout=30)
        if result.returncode:
            raise BrowserNavigationError("unsupported")
        pin.write_text(fingerprint)
    return binary


def ax(command, url, path=None, value=None):
    valid_navigation(url)
    args = [str(ax_binary()), command, url]
    if path is not None:
        args.append(".".join(str(i) for i in path))
    if value is not None:
        args.append(value)
    try:
        result = subprocess.run(args, capture_output=True, timeout=10)
        if result.returncode:
            raise BrowserNavigationError("unavailable")
        return json.loads(result.stdout) if command == "snapshot" else None
    except subprocess.TimeoutExpired:
        raise BrowserNavigationError("timeout") from None
    except (OSError, ValueError):
        raise BrowserNavigationError("unsupported") from None


def snapshot(url):
    for attempt in range(4):
        try:
            rows = ax("snapshot", url)
            challenge_text = " ".join(label(x) for x in rows if x.get("role") in {"AXStaticText", "AXImage"})
            if re.search(r"captcha|капча|введите код с картинки|проверка,? что вы не робот|подтвердите,? что вы не робот|access denied|доступ (?:временно )?ограничен", challenge_text, re.I):
                raise AccessChallenge()
            if re.search(r"слишком много запросов|too many requests|ошибка 429|лимит запросов", challenge_text, re.I):
                raise AccessChallenge("rate_limited")
            return rows
        except AccessChallenge:
            raise
        except BrowserNavigationError:
            if attempt == 3:
                raise
            time.sleep(.4)


def label(row):
    return " ".join(str(row.get(k, "")) for k in ("name", "value", "description")).strip()


def control(rows, text):
    return next((r for r in rows if text in {str(r.get(k, "")).strip() for k in ("name", "value", "description")}), None)


def cases(rows):
    result = {}
    for row in rows:
        match = CASE.fullmatch(row.get("url", ""))
        if not match:
            continue
        number = CASE_NUMBER.search(label(row))
        item = dict(case_id=match[1].lower(), card_url=row["url"], case_number=number[0].replace("–", "-").upper() if number else "")
        if item["case_id"] not in result or item["case_number"]:
            result[item["case_id"]] = item
    return list(result.values())


def card_candidates(case, root, stratum=None):
    verify_storage(root)
    url = navigate(case["card_url"])
    time.sleep(.6)
    rows = snapshot(url)
    acts = control(rows, "Судебные акты")
    if acts is None:
        raise BrowserNavigationError("unsupported")
    ax("press", url, acts["path"], "Судебные акты")
    time.sleep(.6)
    rows = snapshot(url)
    found = {}
    for row in rows:
        candidate = row.get("url", "")
        try:
            native.validate_pdf_url(candidate)
        except native.DownloadError:
            continue
        if pdf_identity(candidate)[0].lower() != case["case_id"]:
            continue
        caption = label(row)
        kind = "decision" if re.search("Решени|признать.*банкрот|реализаци.*имуществ", caption, re.I) else "determination" if re.search("Определени|возвратить|оставить|принять|назначить|включить|прекратить", caption, re.I) else "court-act"
        found[candidate] = dict(source_id=hashlib.sha256(candidate.encode()).hexdigest(), source_url=candidate,
            channel="kad-browser-pdf", case_id=case["case_id"], case_number=case["case_number"],
            court="unknown", year=(stratum or {}).get("year", "unknown"), act_type=kind,
            participant_kind="unknown", authority="owner_assertion", caption=caption,
            provenance=dict(card_url=url, judicial_act_filter_requested=True, judicial_act_filter_confirmed=False, complete_case_history=False, selection_reason="research-stratified-visible-judicial-act"))
    write_json(root / "discovery" / (case["case_id"] + ".json"), dict(case=case, candidates=list(found.values())))
    return list(found.values())


def search(year, category):
    url = navigate("https://kad.arbitr.ru/")
    time.sleep(.5)
    rows = snapshot(url)
    category_control = control(rows, "Банкротные" if category == "bankruptcy" else "Гражданские")
    if category_control:
        ax("press", url, category_control["path"], "Банкротные" if category == "bankruptcy" else "Гражданские")
        time.sleep(5)
        rows = snapshot(url)
    dates = [r for r in rows if r["role"] == "AXTextField" and r["placeholder"] == "дд.мм.гггг"]
    if len(dates) != 2:
        raise BrowserNavigationError("unsupported")
    ax("set", url, dates[0]["path"], f"01.01.{year}")
    ax("set", url, dates[1]["path"], f"31.12.{year}")
    rows = snapshot(url)
    find = control(rows, "Найти")
    if not find:
        raise BrowserNavigationError("unsupported")
    ax("press", url, find["path"], "Найти")
    time.sleep(5)
    rows = snapshot(url)
    return cases(rows)


def append_feed(root, candidates):
    verify_storage(root)
    path = root / "feeds/kad.jsonl"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    known = {json.loads(line)["source_id"] for line in path.read_text().splitlines() if line.strip()} if path.exists() else set()
    import tempfile
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            if path.exists():
                stream.write(path.read_text())
            for row in candidates:
                if row["source_id"] not in known:
                    allowed = {"source_id", "source_url", "channel", "authority", "case_id", "case_number", "court", "year", "act_type", "participant_kind"}
                    stream.write(json.dumps({key: value for key, value in row.items() if key in allowed}, ensure_ascii=False) + "\n")
                    known.add(row["source_id"])
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--category", choices=("bankruptcy", "civil"), default="bankruptcy")
    parser.add_argument("--cases", type=int, default=25)
    parser.add_argument("--acts-per-case", type=int, default=1)
    parser.add_argument("--current-results", action="store_true")
    parser.add_argument("--resume-cases", action="store_true")
    parser.add_argument("--refresh-existing", action="store_true", help="Inspect already downloaded cases for newly visible acts")
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    try:
        root = private_root(args.root)
        verify_storage(root)
        if args.resume_cases:
            candidate_cases = json.loads((root / "discovery/cases.json").read_text())
        elif args.current_results:
            candidate_cases = cases(snapshot(native.current_chrome_url()))
        else:
            candidate_cases = search(args.year, args.category)
        write_json(root / "discovery/cases.json", candidate_cases)
        received = len(list((root / "inbox").glob("*.pdf")))
        discovered = failures = 0
        completed_cases = Counter()
        if args.download:
            from scripts.dev.quality import corpus_acquisition as acquisition
            completed_cases.update(item["metadata"].get("case_id") for item in acquisition.load_queue(root)["items"] if item["status"] == "complete")
        for case_index, case in enumerate(candidate_cases[:args.cases], 1):
            if not args.refresh_existing and completed_cases[case["case_id"]] >= args.acts_per_case:
                continue
            try:
                if args.download:
                    browser_control = acquisition.load_queue(root).get("source_controls", {}).get("kad-browser-pdf", {})
                    if browser_control.get("disabled") or browser_control.get("cooldown_until", 0) > time.time():
                        print(json.dumps(dict(status="source-paused", received_documents=received)), flush=True)
                        return 2
                items = card_candidates(case, root, {"year": str(args.year)})[:args.acts_per_case]
                append_feed(root, items)
                discovered += len(items)
                if args.download and items:
                    from scripts.dev.quality import corpus_acquisition as acquisition
                    acquisition.enqueue(root, root / "feeds/kad.jsonl")
                    outcome = acquisition.run(root, limit=len(items))
                    received = len(list((root / "inbox").glob("*.pdf")))
                    if outcome["paused"] or outcome["state"] in {"disabled", "cooldown"}:
                        print(json.dumps(dict(status="source-paused", received_documents=received)), flush=True)
                        return 2
                print(json.dumps(dict(cases_completed=case_index, discovered_documents=discovered, received_documents=received, failures=failures)), flush=True)
                time.sleep(5)
            except AccessChallenge as error:
                print(json.dumps(dict(status="source-paused", reason=error.reason, received_documents=received)), flush=True)
                return 2
            except BrowserNavigationError as error:
                failures += 1
                print(json.dumps(dict(status="case-unavailable", reason=error.reason, received_documents=received)), flush=True)
                time.sleep(5)
        return 0
    except AccessChallenge as error:
        print(json.dumps(dict(status="source-paused", reason=error.reason)), flush=True)
        return 2
    except BrowserNavigationError as error:
        print(json.dumps(dict(status="discovery-failed", reason=error.reason)), flush=True)
        return 1
    except Exception:
        # Never expose exception messages: paths, browser content or identifiers
        # may be embedded in underlying native/library errors.
        print(json.dumps(dict(status="discovery-failed", reason="internal")), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
