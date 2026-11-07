#!/usr/bin/env python3
"""Ordinary KAD browser discovery. All case metadata remains private on this Mac."""
from __future__ import annotations

from collections import Counter
import calendar
import hashlib
import json
import math
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
from scripts.dev.quality.court_dataset import PrivateParser, private_root, write_json, verify_storage

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CASE = re.compile(rf"^https://kad\.arbitr\.ru/Card/({UUID})$", re.I)
CASE_NUMBER = re.compile(r"(?:[АA]\d{1,3}|СИП)[-–]\d+/\d{2,4}", re.I)
DEFAULT_ROOT = Path.home() / "Library/Application Support/SRO Robot/ocr-research/corpus-2026-11-07"
_BROWSER_TARGET = None
_OWNED_BROWSER_ROOT = None
OWNED_LEASE = "browser-lease.json"
FOCUS_RESTORE = "owner-focus-restore.json"


class BrowserNavigationError(Exception):
    def __init__(self, reason="navigation"):
        self.reason = reason if reason in {"timeout", "navigation", "identity", "unavailable", "unsupported"} else "navigation"
        super().__init__(self.reason)


class AccessChallenge(BrowserNavigationError):
    def __init__(self, reason="challenge", *, observed=False):
        self.reason = reason if reason in {"challenge", "rate_limited", "access_denied"} else "challenge"
        self.observed = observed
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
    except subprocess.CalledProcessError as error:
        # JXA's fixed throws are transport diagnostics, never browser content.
        # Preserve them without echoing stderr (which may contain private data).
        fixed = re.search(r"Error: (timeout|identity|unavailable)\s+\(-?\d+\)\s*$", error.stderr or "")
        raise BrowserNavigationError(fixed[1] if fixed else "navigation") from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise BrowserNavigationError("navigation") from None


def _owned_lease_path(root):
    return root / "discovery" / OWNED_LEASE


def _load_owned_lease(root):
    path = _owned_lease_path(root)
    if path.is_symlink() or not path.exists() or path.stat().st_size > 16 * 1024:
        raise BrowserNavigationError("identity")
    try:
        lease = json.loads(path.read_text(encoding="utf-8"))
        if (lease.get("schema") != 1 or not isinstance(lease.get("window"), str)
                or not isinstance(lease.get("tab"), str) or not isinstance(lease.get("url"), str)):
            raise ValueError
        valid_navigation(lease["url"])
        result = {key: lease[key] for key in ("window", "tab", "url")}
        if 'native_pid' in lease:
            if type(lease['native_pid']) is not int or lease['native_pid'] <= 0:
                raise ValueError
            result['native_pid'] = lease['native_pid']
        pending = lease.get("pending_navigation")
        if pending is not None:
            if not isinstance(pending, dict) or set(pending) != {"from", "to"} or pending["from"] != lease["url"]:
                raise ValueError
            valid_navigation(pending["to"])
            result["pending_navigation"] = pending
        return result
    except (OSError, ValueError, AttributeError, TypeError):
        raise BrowserNavigationError("identity") from None


def _save_owned_lease(root, target):
    from scripts.dev.quality import corpus_acquisition as acquisition
    path = _owned_lease_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    acquisition.write_json(path, {"schema": 1, **target})


def _check_source_title(title):
    title = str(title).lower()
    if re.search(r"captcha|проверка доступа|подтвердите.*не.*робот", title):
        raise AccessChallenge(observed=True)
    if re.search(r"too many requests|слишком много запросов|^429", title):
        raise AccessChallenge("rate_limited", observed=True)
    if re.search(r"access denied|доступ ограничен|^403", title):
        raise AccessChallenge("access_denied", observed=True)


def _read_owned_target(target, *, focus=False):
    script = '''var c=Application("com.google.Chrome");if(!c.running())throw Error("unavailable");
var target=TARGET,wins=c.windows().filter(function(w){return String(w.id())===target.window;});
if(wins.length!==1)throw Error("identity");var win=c.windows.byId(Number(target.window)),tabs=win.tabs(),index=-1;
for(var i=0;i<tabs.length;i++)if(String(tabs[i].id())===target.tab)index=i;
if(index<0)throw Error("identity");var tab=win.tabs.byId(Number(target.tab));
if(String(win.id())!==target.window||String(tab.id())!==target.tab)throw Error("identity");
FOCUS
win=c.windows.byId(Number(target.window));tab=win.tabs.byId(Number(target.tab));
if(String(win.id())!==target.window||String(tab.id())!==target.tab)throw Error("identity");
JSON.stringify({window:String(win.id()),tab:String(tab.id()),url:String(tab.url()),title:String(tab.title()),window_title:String(win.name()),bounds:win.bounds(),loading:tab.loading()});'''
    focus_script = 'if(String(tab.url())!==target.url)throw Error("identity");win.visible=true;win.minimized=false;win.activeTabIndex=index+1;win.index=1;c.activate();'
    result = _jxa(script.replace('var target=TARGET,', 'var target=' + json.dumps(target) + ',').replace('FOCUS', focus_script if focus else ''))
    if result.get('window') != target['window'] or result.get('tab') != target['tab']:
        raise BrowserNavigationError('identity')
    if 'bounds' in result:
        result['bounds'] = _window_bounds(result['bounds'])
    return result


def _window_bounds(value):
    if isinstance(value, dict) and all(key in value for key in ('x', 'y', 'width', 'height')):
        x, y, width, height = (value[key] for key in ('x', 'y', 'width', 'height'))
        if any(type(number) not in {int, float} for number in (x, y, width, height)):
            raise BrowserNavigationError('identity')
        value = [x, y, x + width, y + height]
    if (not isinstance(value, list) or len(value) != 4
            or any(type(number) not in {int, float} for number in value)
            or not all(math.isfinite(number) for number in value)
            or value[2] <= value[0] or value[3] <= value[1]):
        raise BrowserNavigationError('identity')
    return value


def _same_navigation(actual, expected):
    if actual == expected:
        return True
    try:
        return tuple(x.lower() for x in pdf_identity(actual)) == tuple(x.lower() for x in pdf_identity(expected))
    except native.DownloadError:
        return False


def reconcile_owned_browser(root):
    """Reconcile only a journaled transition in the exact privately owned tab."""
    global _BROWSER_TARGET
    target = _load_owned_lease(root)
    observed = _read_owned_target(target)
    valid_navigation(observed["url"])
    _check_source_title(observed.get("title", ""))
    pending = target.get("pending_navigation")
    if pending:
        if not (observed["url"] == target["url"] or _same_navigation(observed["url"], pending["to"])):
            raise BrowserNavigationError("identity")
        if observed.get("loading"):
            raise BrowserNavigationError("timeout")
        target = {key: observed[key] for key in ("window", "tab", "url")}
        _save_owned_lease(root, target)
    elif observed["url"] != target["url"]:
        raise BrowserNavigationError("identity")
    _BROWSER_TARGET = target
    return observed


def _reveal_owned_window_menu(target, title):
    """Reveal one exact-title window through its proven Chrome process menu."""
    pid = target.get('native_pid')
    if type(pid) is not int or pid <= 0 or not isinstance(title, str) or not title:
        raise BrowserNavigationError('identity')
    script = '''var se=Application("System Events"),target=TARGET,title=TITLE;
var ps=se.processes.whose({unixId:target.native_pid});
if(ps.length!==1||String(ps[0].bundleIdentifier())!=="com.google.Chrome")throw Error("identity");
var p=ps[0],bars=p.menuBars(),matches=[];
if(bars.length!==1)throw Error("identity");var tops=bars[0].menuBarItems();
for(var i=0;i<tops.length;i++){
if(["Window","Окно"].indexOf(String(tops[i].name()))<0)continue;
var menus=tops[i].menus();if(menus.length!==1)continue;var items=menus[0].menuItems();
for(var j=0;j<items.length;j++)if(String(items[j].name())===title)matches.push(items[j]);
}
if(matches.length!==1||!matches[0].enabled())throw Error("identity");
p.frontmost=true;matches[0].click();JSON.stringify(true);'''
    if _jxa(script.replace('target=TARGET,title=TITLE;', 'target=' + json.dumps(target) + ',title=' + json.dumps(title) + ';')) is not True:
        raise BrowserNavigationError('identity')


def focus_owned_browser(root, expected_url):
    _owned_browser_preflight(root, check_coordinator=False)
    observed = reconcile_owned_browser(root)
    if observed["url"] != expected_url:
        raise BrowserNavigationError("identity")
    previous = _remember_owner_focus(root, _BROWSER_TARGET, defer=True)
    prepared = _read_owned_target(_BROWSER_TARGET, focus=True)
    if prepared.get('url') != expected_url:
        raise BrowserNavigationError('identity')
    metadata = {"title": prepared.get("window_title"), "bounds": prepared.get("bounds")}
    if (not isinstance(metadata["title"], str) or len(metadata["title"]) > 4000
            or not isinstance(metadata["bounds"], list) or len(metadata["bounds"]) != 4):
        raise BrowserNavigationError("identity")
    if _BROWSER_TARGET.get('native_pid'):
        metadata['native_pid'] = _BROWSER_TARGET['native_pid']
    # Apple Events z-order is not macOS key-window identity. Raise and verify
    # the uniquely corresponding native window before any menu/AX action.
    try:
        result = subprocess.run([str(ax_binary()), "focus-owned", expected_url],
                                input=json.dumps(metadata).encode(), capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise BrowserNavigationError("unavailable") from None
    if result.returncode == 9 and _BROWSER_TARGET.get('native_pid'):
        # JXA index changes can leave a PDF window outside the active Space.
        # The Window menu addresses its exact title under the previously proven
        # native PID. Press once, then require the same strict key-window proof.
        _reveal_owned_window_menu(_BROWSER_TARGET, metadata['title'])
        refreshed = _read_owned_target(_BROWSER_TARGET)
        if refreshed['url'] != expected_url or refreshed.get('window_title') != metadata['title']:
            raise BrowserNavigationError('identity')
        metadata['bounds'] = refreshed.get('bounds')
        try:
            result = subprocess.run([str(ax_binary()), "focus-owned", expected_url],
                                    input=json.dumps(metadata).encode(), capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            raise BrowserNavigationError('unavailable') from None
    if result.returncode:
        raise BrowserNavigationError("identity")
    try:
        native_pid = json.loads(result.stdout)['native_pid']
    except (ValueError, TypeError, KeyError):
        raise BrowserNavigationError('identity') from None
    if (type(native_pid) is not int or native_pid <= 0
            or (_BROWSER_TARGET.get('native_pid') and native_pid != _BROWSER_TARGET['native_pid'])):
        raise BrowserNavigationError('identity')
    after = _read_owned_target(_BROWSER_TARGET)
    if after["url"] != expected_url or after.get("window_title") != metadata["title"] or after.get("bounds") != metadata["bounds"]:
        raise BrowserNavigationError("identity")
    _BROWSER_TARGET['native_pid'] = native_pid
    _save_owned_lease(root, _BROWSER_TARGET)
    _save_owner_focus(root, _BROWSER_TARGET, previous)
    return native_pid


def _record_creation_challenge(root, error):
    from scripts.dev.quality import corpus_acquisition as acquisition
    with acquisition.queue_lock(root):
        state = acquisition.load_queue(root)
        control = {"disabled": error.reason != "rate_limited", "reason": "browser_access_challenge"}
        if not control["disabled"]:
            control.update(reason="browser_rate_limited", cooldown_until=time.time() + 60)
        state.setdefault("source_controls", {})["kad-browser-pdf"] = control
        acquisition.write_json(root / acquisition.STATE, state)


def _save_owner_focus(root, target, previous):
    if not previous.get("bundle") or (previous.get("bundle") == "com.google.Chrome" and previous.get("window") == target["window"] and previous.get("tab") == target["tab"] and (not target.get('native_pid') or not previous.get('native_pid') or previous['native_pid'] == target['native_pid'])):
        return
    from scripts.dev.quality import corpus_acquisition as acquisition
    previous = {"bundle": previous.get("bundle"), "name": previous.get("name"),
                "native_pid": previous.get("native_pid"),
                "window": previous.get("window"), "tab": previous.get("tab"),
                "url_sha256": hashlib.sha256(previous.get("url", "").encode()).hexdigest() if previous.get("url") else ""}
    acquisition.write_json(root / "discovery" / FOCUS_RESTORE, {"schema": 1, **previous, "owned_window": target["window"]})


def _remember_owner_focus(root, target, *, defer=False):
    script = '''var c=Application("com.google.Chrome"),se=Application("System Events"),ps=se.processes.whose({frontmost:true}),previous={bundle:"",name:"",window:"",tab:"",url:"",native_pid:null};
if(ps.length){previous.bundle=String(ps[0].bundleIdentifier());previous.name=String(ps[0].name());previous.native_pid=Number(ps[0].unixId());}
if(previous.bundle==="com.google.Chrome"&&c.windows().length){var w=c.windows[0];previous.window=String(w.id());previous.tab=String(w.activeTab.id());previous.url=String(w.activeTab.url());}
JSON.stringify(previous);'''
    previous = _jxa(script)
    if not defer:
        _save_owner_focus(root, target, previous)
    return previous


def _owned_browser_preflight(root, year=None, category=None, *, check_coordinator=True, month=None):
    from scripts.dev.quality import corpus_acquisition as acquisition
    state = acquisition.load_queue(root)
    control = state.get("source_controls", {}).get("kad-browser-pdf", {})
    if control.get("disabled"):
        raise AccessChallenge("access_denied")
    if float(control.get("cooldown_until", 0) or 0) > time.time():
        raise AccessChallenge("rate_limited")
    from scripts.dev.quality.download_current_kad import _unresolved_save_exists
    if _unresolved_save_exists(root / "inbox"):
        raise AccessChallenge("challenge")
    key = hashlib.sha256((f"{year}:{category}" + (f":{month}" if month is not None else "")).encode()).hexdigest()[:24]
    states = ([root / "coordinator" / f"{key}.json"] if year is not None else list((root / "coordinator").glob("*.json"))) if check_coordinator else []
    for coordinator_state in states:
        if coordinator_state.is_symlink():
            raise AccessChallenge("challenge")
        if not coordinator_state.exists():
            continue
        if coordinator_state.stat().st_size > 64 * 1024:
            raise AccessChallenge("challenge")
        try:
            status = json.loads(coordinator_state.read_text(encoding="utf-8")).get("status")
        except Exception:
            raise AccessChallenge("challenge") from None
        if status in {"blocked", "unknown", "exhausted"}:
            raise AccessChallenge("challenge")


def claim_owned_browser(root, year=None, category=None, *, month=None):
    """Bind to a dedicated KAD tab without borrowing an arbitrary review tab."""
    global _BROWSER_TARGET, _OWNED_BROWSER_ROOT
    verify_storage(root)
    _owned_browser_preflight(root, year, category, month=month)
    _OWNED_BROWSER_ROOT = root
    lease_path = _owned_lease_path(root)
    if lease_path.exists() or lease_path.is_symlink():
        observed = reconcile_owned_browser(root)
        if observed.get("loading"):
            raise BrowserNavigationError("timeout")
        _remember_owner_focus(root, _BROWSER_TARGET)
        if urlsplit(observed["url"]).path == "/" or CASE.fullmatch(observed["url"]):
            try:
                snapshot(observed["url"])
            except AccessChallenge as error:
                _record_creation_challenge(root, error)
                raise
        return {"status": "reused"}
    script = '''ObjC.import("Foundation");
var c=Application("com.google.Chrome");if(!c.running())c.launch();
var se=Application("System Events"),ps=se.processes.whose({frontmost:true}),previous={bundle:"",name:"",window:"",tab:"",url:"",native_pid:null};
if(ps.length){var p=ps[0];previous.bundle=String(p.bundleIdentifier());previous.name=String(p.name());previous.native_pid=Number(p.unixId());}
if(previous.bundle==="com.google.Chrome"&&c.windows().length){var pw=c.windows[0];previous.window=String(pw.id());previous.tab=String(pw.activeTab.id());previous.url=String(pw.activeTab.url());}
var inheritedBounds=null;if(c.windows().length){var b=c.windows[0].bounds();if(b&&[b.x,b.y,b.width,b.height].every(function(v){return typeof v==="number"&&isFinite(v);})&&b.width>300&&b.height>200)inheritedBounds=b;}
c.activate();
var created=c.Window().make(),wid=String(created.id()),w=c.windows.byId(Number(wid));
if(inheritedBounds)w.bounds=inheritedBounds;w.visible=true;w.minimized=false;w.index=1;c.activate();w=c.windows.byId(Number(wid));var tid=String(w.activeTab.id()),t=w.tabs.byId(Number(tid));t.url="https://kad.arbitr.ru/";
var end=Date.now()+30000;while(t.loading()&&Date.now()<end)$.NSThread.sleepForTimeInterval(.1);
JSON.stringify({previous:previous,window:String(w.id()),tab:String(t.id()),url:String(t.url()),title:String(t.title()),loaded:!t.loading()});'''
    result = _jxa(script, timeout=40)
    target = {"window": result["window"], "tab": result["tab"], "url": result["url"]}
    valid_navigation(target["url"])
    _save_owned_lease(root, target)
    try:
        _check_source_title(result.get("title", ""))
    except AccessChallenge as error:
        _record_creation_challenge(root, error)
        raise
    previous = result.get("previous", {})
    _save_owner_focus(root, target, previous)
    _BROWSER_TARGET = target
    if not result.get("loaded"):
        raise BrowserNavigationError("timeout")
    try:
        snapshot(target["url"])
    except AccessChallenge as error:
        _record_creation_challenge(root, error)
        raise
    return {"status": "created"}


ensure_owned_browser = claim_owned_browser


def restore_owner_focus(root):
    """Restore only our captured owner window while our own window is front."""
    from scripts.dev.quality.download_current_kad import _unresolved_save_exists
    path = root / "discovery" / FOCUS_RESTORE
    if _unresolved_save_exists(root / "inbox") or path.is_symlink() or not path.is_file():
        return False
    try:
        if path.stat().st_size > 16 * 1024:
            return False
        previous = json.loads(path.read_text())
        target = _load_owned_lease(root)
        if previous.get("schema") != 1 or previous.get("owned_window") != target["window"]:
            return False
        # Bundle IDs are ambiguous when ordinary and managed Chrome coexist.
        # Only the proven source PID may surrender focus, and restoration targets
        # the captured owner PID, never a by-name process or another instance tab.
        source_pid, owner_pid = target.get('native_pid'), previous.get('native_pid')
        if type(source_pid) is not int or source_pid <= 0 or type(owner_pid) is not int or owner_pid <= 0:
            return False
        expected_previous_url = ""
        if previous.get("bundle") == "com.google.Chrome" and owner_pid == source_pid:
            old = _read_owned_target({"window": previous["window"], "tab": previous["tab"], "url": ""})
            if hashlib.sha256(old["url"].encode()).hexdigest() != previous.get("url_sha256"):
                return False
            expected_previous_url = old["url"]
        script = '''var c=Application("com.google.Chrome"),target=TARGET,previous=PREVIOUS;
var se=Application("System Events"),ps=se.processes.whose({frontmost:true});
if(ps.length!==1||Number(ps[0].unixId())!==target.native_pid||String(ps[0].bundleIdentifier())!=="com.google.Chrome")JSON.stringify(false);
else if(previous.native_pid!==target.native_pid){
var owners=se.processes.whose({unixId:previous.native_pid});
if(owners.length!==1||String(owners[0].bundleIdentifier())!==previous.bundle)JSON.stringify(false);
else{owners[0].frontmost=true;JSON.stringify(true);}
}else if(previous.bundle==="com.google.Chrome"){
var owned=c.windows.byId(Number(target.window));
if(!owned.exists()||String(owned.activeTab.id())!==target.tab||String(owned.activeTab.url())!==target.url)throw Error("identity");
var win=c.windows.byId(Number(previous.window)),tabs=win.tabs(),index=-1;
for(var i=0;i<tabs.length;i++)if(String(tabs[i].id())===previous.tab)index=i;
if(!win.exists()||index<0||String(tabs[index].url())!==PREVIOUS_URL)throw Error("identity");
win.activeTabIndex=index+1;win.index=1;c.activate();JSON.stringify(true);
}else JSON.stringify(false);'''
        return bool(_jxa(script.replace("target=TARGET,previous=PREVIOUS;", "target=" + json.dumps(target) + ",previous=" + json.dumps(previous) + ";").replace("PREVIOUS_URL", json.dumps(expected_previous_url))))
    except (BrowserNavigationError, OSError, ValueError, KeyError, TypeError):
        return False


def navigate(url, *, root=None):
    global _BROWSER_TARGET
    valid_navigation(url)
    if root is not None:
        reconcile_owned_browser(root)
    if _BROWSER_TARGET is None:
        target = _jxa('''var c=Application("com.google.Chrome");if(!c.running()||c.windows().length===0)throw Error("unavailable");var w=c.windows[0],t=w.activeTab;JSON.stringify({window:String(w.id()),tab:String(t.id()),url:String(t.url())});''')
        valid_navigation(target["url"])
        _BROWSER_TARGET = target
    reuse_pdf = False
    try:
        reuse_pdf = pdf_identity(url) == pdf_identity(_BROWSER_TARGET["url"])
    except native.DownloadError:
        pass
    script = '''ObjC.import("Foundation");
var c=Application("com.google.Chrome");
if(!c.running()||c.windows().length===0)throw Error("unavailable");
var target=TARGET;
var wins=c.windows().filter(function(w){return String(w.id())===target.window;});
if(wins.length!==1)throw Error("identity");
var win=c.windows.byId(Number(target.window)),tabs=win.tabs(),index=-1;
for(var i=0;i<tabs.length;i++)if(String(tabs[i].id())===target.tab)index=i;
if(index<0)throw Error("identity");var tab=win.tabs.byId(Number(target.tab));
if(String(win.id())!==target.window||String(tab.id())!==target.tab||String(tab.url())!==target.url)throw Error("identity");
win.activeTabIndex=index+1;win.index=1;
win=c.windows.byId(Number(target.window));tab=win.tabs.byId(Number(target.tab));var id=target.tab;
if(String(win.id())!==target.window||String(tab.id())!==id||String(tab.url())!==target.url)throw Error("identity");
tab.url=URL;
var end=Date.now()+25000;
while(tab.loading()&&Date.now()<end){$.NSThread.sleepForTimeInterval(.1);}
if(tab.loading())throw Error("timeout");
if(String(c.windows[0].activeTab.id())!==id)throw Error("identity");
JSON.stringify({url:String(tab.url()),title:String(tab.title())});'''.replace("var target=TARGET;", "var target=" + json.dumps(_BROWSER_TARGET) + ";").replace("tab.url=URL;", "" if reuse_pdf else "tab.url=" + json.dumps(url) + ";")
    if root is not None:
        # Journal the sole permitted transition before changing the browser.
        if not reuse_pdf:
            _BROWSER_TARGET["pending_navigation"] = {"from": _BROWSER_TARGET["url"], "to": url}
            _save_owned_lease(root, _BROWSER_TARGET)
        script = script.replace('win.activeTabIndex=index+1;win.index=1;', 'win.activeTabIndex=index+1;')
        script = script.replace('if(String(c.windows[0].activeTab.id())!==id)throw Error("identity");',
                                'if(String(win.id())!==target.window||String(tab.id())!==id)throw Error("identity");')
    try:
        result = _jxa(script)
    except BrowserNavigationError:
        if root is None:
            raise
        result = reconcile_owned_browser(root)
        if result.get("loading") or not _same_navigation(result["url"], url):
            raise
    _check_source_title(result.get("title", ""))
    valid_navigation(result["url"])
    _BROWSER_TARGET["url"] = result["url"]
    _BROWSER_TARGET.pop("pending_navigation", None)
    if root is not None:
        _save_owned_lease(root, _BROWSER_TARGET)
    return result["url"]


def pdf_identity(url):
    native.validate_pdf_url(url)
    return tuple(re.findall(UUID, urlsplit(url).path, re.I)[:2])


def navigate_pdf(url, *, root=None):
    global _OWNED_BROWSER_ROOT
    if root is not None:
        _OWNED_BROWSER_ROOT = root
    expected = pdf_identity(url)
    actual = navigate(url, root=_OWNED_BROWSER_ROOT) if _OWNED_BROWSER_ROOT is not None else navigate(url)
    try:
        if pdf_identity(actual) != expected:
            raise BrowserNavigationError("identity")
    except native.DownloadError:
        raise BrowserNavigationError("identity") from None
    if _OWNED_BROWSER_ROOT is not None:
        focus_owned_browser(_OWNED_BROWSER_ROOT, actual)
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
    if _OWNED_BROWSER_ROOT is not None:
        focus_owned_browser(_OWNED_BROWSER_ROOT, url)
    args = [str(ax_binary()), command, url]
    if path is not None:
        args.append(".".join(str(i) for i in path))
    if value is not None:
        args.append(value)
    if _OWNED_BROWSER_ROOT is not None:
        args.append("--focused")
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
                raise AccessChallenge(observed=True)
            if re.search(r"слишком много запросов|too many requests|ошибка 429|лимит запросов", challenge_text, re.I):
                raise AccessChallenge("rate_limited", observed=True)
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
    from scripts.dev.quality import corpus_acquisition as acquisition
    with acquisition.profile_stage(root, "discovery.case_load"):
        url = navigate(case["card_url"], root=root) if _OWNED_BROWSER_ROOT is not None else navigate(case["card_url"])
        time.sleep(.6)
        rows = snapshot(url)
    acts = control(rows, "Судебные акты")
    if acts is None:
        raise BrowserNavigationError("unsupported")
    with acquisition.profile_stage(root, "discovery.case_filter"):
        ax("press", url, acts["path"], "Судебные акты")
        time.sleep(.6)
        rows = snapshot(url)
    found = {}
    with acquisition.profile_stage(root, "discovery.candidate_select"):
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
    def caption_rank(row):
        rank = acquisition._cached_caption_rank(row.get("caption", ""))
        return 99 if rank is None else rank
    ranked = sorted(found.values(), key=caption_rank)
    write_json(root / "discovery" / (case["case_id"] + ".json"), dict(case=case, candidates=ranked))
    return ranked


def _search_dates(year, month=None):
    if type(year) is not int or not 1900 <= year <= 2200 or (month is not None and (type(month) is not int or not 1 <= month <= 12)):
        raise BrowserNavigationError("unsupported")
    return (f"01.{month:02d}.{year}", f"{calendar.monthrange(year, month)[1]:02d}.{month:02d}.{year}") if month is not None else (f"01.01.{year}", f"31.12.{year}")


def search(year, category, root=None, month=None):
    from contextlib import nullcontext
    from scripts.dev.quality import corpus_acquisition as acquisition
    with acquisition.profile_stage(root, "discovery.search_navigation") if root else nullcontext():
        url = navigate("https://kad.arbitr.ru/", root=root) if _OWNED_BROWSER_ROOT is not None else navigate("https://kad.arbitr.ru/")
        time.sleep(.5)
        rows = snapshot(url)
    category_control = control(rows, "Банкротные" if category == "bankruptcy" else "Гражданские")
    if category_control:
        with acquisition.profile_stage(root, "discovery.search_filter") if root else nullcontext():
            ax("press", url, category_control["path"], "Банкротные" if category == "bankruptcy" else "Гражданские")
            time.sleep(5)
            rows = snapshot(url)
    with acquisition.profile_stage(root, "discovery.search_results") if root else nullcontext():
        dates = [r for r in rows if r["role"] == "AXTextField" and r["placeholder"] == "дд.мм.гггг"]
        if len(dates) != 2:
            raise BrowserNavigationError("unsupported")
        start, end = _search_dates(year, month)
        ax("set", url, dates[0]["path"], start)
        ax("set", url, dates[1]["path"], end)
        rows = snapshot(url)
        find = control(rows, "Найти")
        if not find:
            raise BrowserNavigationError("unsupported")
        ax("press", url, find["path"], "Найти")
        time.sleep(5)
        rows = snapshot(url)
    return cases(rows)


FEED_FIELDS = {"source_id", "source_url", "channel", "authority", "case_id", "case_number", "court", "year", "act_type", "participant_kind"}


def official_case_year(case):
    """Year of the official case serial, independently of requested UI dates."""
    number = case.get('case_number')
    if not isinstance(number, str) or not CASE_NUMBER.fullmatch(number.strip()):
        return None
    suffix = number.strip().rsplit('/', 1)[-1]
    if len(suffix) not in {2, 4}:
        return None
    return int(suffix) + (2000 if len(suffix) == 2 else 0)


def qualify_candidates(case, candidates, *, case_year=None, max_caption_rank=None):
    if case_year is not None and official_case_year(case) != case_year:
        return [], {'skipped_case_year': 1, 'skipped_caption': 0}
    from scripts.dev.quality import corpus_acquisition as acquisition
    selected = []
    for item in candidates:
        rank = acquisition._cached_caption_rank(item.get('caption', ''))
        if max_caption_rank is not None and (rank is None or rank > max_caption_rank):
            continue
        selected.append(item)
    return selected, {'skipped_case_year': 0, 'skipped_caption': len(candidates) - len(selected)}


def enqueue_selected(root, candidates):
    """Do not re-enqueue unrelated historical entries from the durable feed."""
    import tempfile
    from scripts.dev.quality import corpus_acquisition as acquisition
    path = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=root / 'staging', delete=False) as stream:
            path = Path(stream.name)
            os.chmod(path, 0o600)
            for row in candidates:
                stream.write(json.dumps({key: value for key, value in row.items() if key in FEED_FIELDS}) + '\n')
            stream.flush(); os.fsync(stream.fileno())
        return acquisition.enqueue(root, path)
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


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
                    stream.write(json.dumps({key: value for key, value in row.items() if key in FEED_FIELDS}, ensure_ascii=False) + "\n")
                    known.add(row["source_id"])
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = PrivateParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--year", type=int, default=2024, help="Requested filing-date UI filter; owned mode also verifies the official case serial year")
    parser.add_argument("--case-year", type=int, choices=range(1900, 2201), help="Require this official case serial year (owned mode always requires --year)")
    parser.add_argument("--max-caption-rank", type=int, choices=range(4), help="Require a qualified bankruptcy caption rank; owned mode defaults to rank 0")
    parser.add_argument("--month", type=int, choices=range(1, 13))
    parser.add_argument("--category", choices=("bankruptcy", "civil"), default="bankruptcy")
    parser.add_argument("--cases", type=int, default=25)
    parser.add_argument("--acts-per-case", type=int, default=1)
    parser.add_argument("--current-results", action="store_true")
    parser.add_argument("--resume-cases", action="store_true")
    parser.add_argument("--refresh-existing", action="store_true", help="Inspect already downloaded cases for newly visible acts")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--owned-browser", action="store_true", help="Use the private dedicated KAD browser lease")
    try:
        args = parser.parse_args()
        if args.owned_browser and args.case_year is not None and args.case_year != args.year:
            parser.error('owned_case_year_must_match_stratum')
        required_case_year = args.year if args.owned_browser else args.case_year
        caption_rank = (0 if args.owned_browser else None) if args.max_caption_rank is None else args.max_caption_rank
        root = private_root(args.root)
        verify_storage(root)
        if args.owned_browser:
            global _OWNED_BROWSER_ROOT, _BROWSER_TARGET
            # The parent coordinator already owns its unknown child marker.
            # Child loading never creates/replaces a browser window.
            _owned_browser_preflight(root, check_coordinator=False)
            _OWNED_BROWSER_ROOT = root
            reconcile_owned_browser(root)
        if args.resume_cases:
            candidate_cases = json.loads((root / "discovery/cases.json").read_text())
        elif args.current_results:
            candidate_cases = cases(snapshot(_BROWSER_TARGET["url"] if args.owned_browser else native.current_chrome_url()))
        else:
            candidate_cases = search(args.year, args.category, root, month=args.month)
        write_json(root / "discovery/cases.json", candidate_cases)
        received = len(list((root / "inbox").glob("*.pdf")))
        discovered = failures = 0
        qualification = Counter(skipped_case_year=0, skipped_caption=0)
        completed_cases = Counter()
        if args.download:
            from scripts.dev.quality import corpus_acquisition as acquisition
            completed_cases.update(item["metadata"].get("case_id") for item in acquisition.load_queue(root)["items"] if item["status"] == "complete")
        for case_index, case in enumerate(candidate_cases[:args.cases], 1):
            if required_case_year is not None and official_case_year(case) != required_case_year:
                qualification['skipped_case_year'] += 1
                print(json.dumps(dict(cases_completed=case_index, discovered_documents=discovered, received_documents=received, failures=failures, **qualification)), flush=True)
                continue
            if not args.refresh_existing and completed_cases[case["case_id"]] >= args.acts_per_case:
                continue
            try:
                if args.download:
                    browser_control = acquisition.load_queue(root).get("source_controls", {}).get("kad-browser-pdf", {})
                    if browser_control.get("disabled") or browser_control.get("cooldown_until", 0) > time.time():
                        print(json.dumps(dict(status="source-paused", received_documents=received)), flush=True)
                        return 2
                candidates = card_candidates(case, root, {"year": str(args.year)})
                items, skipped = qualify_candidates(case, candidates, case_year=required_case_year, max_caption_rank=caption_rank)
                qualification.update(skipped)
                items = items[:args.acts_per_case]
                append_feed(root, items)
                discovered += len(items)
                if args.download and items:
                    from scripts.dev.quality import corpus_acquisition as acquisition
                    enqueue_selected(root, items)
                    outcome = acquisition.run(root, limit=len(items))
                    received = len(list((root / "inbox").glob("*.pdf")))
                    if outcome["paused"] or outcome["state"] in {"disabled", "cooldown"}:
                        print(json.dumps(dict(status="source-paused", received_documents=received)), flush=True)
                        return 2
                print(json.dumps(dict(cases_completed=case_index, discovered_documents=discovered, received_documents=received, failures=failures, **qualification)), flush=True)
                if args.owned_browser:
                    restore_owner_focus(root)
                if not items:
                    time.sleep(5)
            except AccessChallenge as error:
                if error.observed:
                    _record_creation_challenge(root, error)
                print(json.dumps(dict(status="source-paused", reason=error.reason, received_documents=received)), flush=True)
                return 2
            except BrowserNavigationError as error:
                failures += 1
                if error.reason in {"identity", "timeout", "unavailable"}:
                    print(json.dumps(dict(status="source-paused" if error.reason == "identity" else "discovery-failed", reason=error.reason, received_documents=received)), flush=True)
                    return 2 if error.reason == "identity" else 1
                print(json.dumps(dict(status="case-unavailable", reason=error.reason, received_documents=received)), flush=True)
                time.sleep(5)
        return 0
    except AccessChallenge as error:
        if error.observed and 'root' in locals():
            _record_creation_challenge(root, error)
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
    finally:
        if 'args' in locals() and 'root' in locals() and args.owned_browser:
            restore_owner_focus(root)


if __name__ == "__main__":
    raise SystemExit(main())
