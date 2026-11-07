"""Save one explicitly selected KAD PDF from the current Chrome tab."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


MAX_BYTES = 64 * 1024 * 1024
PDFINFO_TIMEOUT_SECONDS = 10
CHROME_URL_SCRIPT = (
    'var chrome = Application("com.google.Chrome");\n'
    'if (!chrome.running() || chrome.windows().length === 0) throw new Error("no window");\n'
    'chrome.windows[0].activeTab.url();'
)
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PDF_PATH = re.compile(
    rf"^/(?:Kad/PdfDocument|Document/Pdf)/{UUID}/{UUID}/[A-Za-z0-9._-]+\.pdf$",
    re.IGNORECASE,
)
NATIVE_PHASES = {
    "preflight_complete", "save_panel_open", "folder_dialog_open", "folder_selected",
    "save_controls_ready", "save_clicked", "save_panel_closed", "complete",
    "failed_before_save_click", "failed_after_save_click",
}
NATIVE_DIAGNOSTIC_STATUSES = {
    "complete", "not_saved", "unknown", "process_timeout", "launch_error", "process_error",
}


class DownloadError(Exception):
    """Safe, fixed-message error for callers and CLI output."""


class SaveOutcomeUnknown(DownloadError):
    """A native Save may have completed; keep local evidence for reconciliation."""


class NativeSaveNotStarted(DownloadError):
    """The native Save driver returned its explicit, successful no-save status."""


@dataclass(frozen=True)
class DownloadResult:
    digest: str
    size: int
    duplicate: bool
    path: Path


def validate_pdf_url(raw: str) -> str:
    """Accept only the reviewed KAD PDF routes; never return the URL to logs."""
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        raise DownloadError("Rejected source URL. Use an approved KAD PDF tab.")
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in raw) or "\\" in raw:
        raise DownloadError("Rejected source URL. Use an approved KAD PDF tab.")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise DownloadError("Rejected source URL. Use an approved KAD PDF tab.") from None
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != "kad.arbitr.ru"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or not PDF_PATH.fullmatch(parsed.path)
    ):
        raise DownloadError("Rejected source URL. Use an approved KAD PDF tab.")
    expected_query = "isAddStamp=True" if parsed.path.lower().startswith("/document/pdf/") else ""
    if parsed.query not in ("", expected_query):
        raise DownloadError("Rejected source URL. Use an approved KAD PDF tab.")
    return raw


def current_chrome_url() -> str:
    """Read only the current Chrome tab URL through JXA."""
    if sys.platform != "darwin":
        raise DownloadError("Current Chrome-tab capture is available only on macOS.")
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", CHROME_URL_SCRIPT],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise DownloadError("Could not read the active Chrome tab URL.") from None
    return result.stdout.strip()


def _private_directory(directory: Path) -> Path:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise OSError("not a private directory")
        os.chmod(directory, 0o700)
        return directory
    except OSError:
        raise DownloadError("Could not prepare the private research inbox.") from None


def _check_no_symlink(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError:
        raise DownloadError("Could not verify the private research inbox path.") from None
    if stat.S_ISLNK(mode):
        raise DownloadError("The private research inbox path contains a symlink; stopping.")


def _default_inbox_for(repository: Path) -> Path:
    storage = repository / "storage"
    inbox = storage / "ocr-research-inbox"
    _check_no_symlink(storage)
    _check_no_symlink(inbox)
    return inbox


def _atomic_create(path: Path, writer) -> bool:
    """Create a private file atomically without replacing any existing file."""
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".kad-save-", dir=path.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError:
            return False
        return True
    except DownloadError:
        raise
    except OSError:
        raise DownloadError("Could not publish the PDF safely.") from None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _verify_existing_pdf(path: Path, digest: str, expected_size: int) -> None:
    """Verify a content-addressed file without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size or info.st_size > MAX_BYTES:
                raise DownloadError("Existing content-addressed file is invalid; it was not replaced.")
            calculated = hashlib.sha256()
            size = 0
            while size <= expected_size:
                chunk = stream.read(min(1024 * 1024, expected_size + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                calculated.update(chunk)
            if size != expected_size or calculated.hexdigest() != digest:
                raise DownloadError("Existing content-addressed file is invalid; it was not replaced.")
    except DownloadError:
        raise
    except OSError:
        raise DownloadError("Existing content-addressed file is invalid; it was not replaced.") from None


def _verify_existing_receipt(path: Path, digest: str, source_url: str, expected_size: int) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            value = json.loads(stream.read(16 * 1024).decode("utf-8"))
        if (
            value.get("sha256") != digest
            or value.get("source_url") != source_url
            or value.get("bytes") != expected_size
        ):
            raise ValueError
        validate_pdf_url(source_url)
    except (OSError, ValueError, AttributeError, DownloadError):
        raise DownloadError("PDF is saved but its private receipt is invalid; inspect the inbox before retrying.") from None


def _native_save_script(expected_url: str, staging_directory: str, native_process_id: int | None = None) -> str:
    """Build a JXA script that uses only Chrome's native Save Page As dialog."""
    if native_process_id is not None and (type(native_process_id) is not int or native_process_id <= 0):
        raise DownloadError("Owned Chrome process identity is invalid.")
    expected_literal = json.dumps(expected_url)
    directory_literal = json.dumps(staging_directory)
    process_literal = "null" if native_process_id is None else str(native_process_id)
    return f'''ObjC.import("Foundation");
var chrome = Application("com.google.Chrome");
var systemEvents = Application("System Events");
var nativePID = {process_literal};
var processMatches = nativePID === null ? [systemEvents.processes["Google Chrome"]] :
  systemEvents.processes.whose({{unixId: nativePID}});
if (processMatches.length !== 1) throw new Error("process identity");
var process = processMatches[0];
if (String(process.bundleIdentifier()) !== "com.google.Chrome" ||
    (nativePID !== null && Number(process.unixId()) !== nativePID)) throw new Error("process identity");
var expectedURL = {expected_literal};
var stagingDirectory = {directory_literal};
var deadline = Date.now() + 26000;
var result = "NATIVE_SAVE_NOT_SAVED";
var saveClicked = false;
var openedByUs = false;
var phaseStart = Date.now();
function markPhase(name) {{
  try {{
    var line = "NATIVE_PHASE " + name + " " + String(Date.now() - phaseStart) + "\\n";
    var data = $.NSString.alloc.initWithUTF8String(line).dataUsingEncoding($.NSUTF8StringEncoding);
    $.NSFileHandle.fileHandleWithStandardError.writeData(data);
  }} catch (_) {{}}
}}
function nap(seconds) {{ $.NSThread.sleepForTimeInterval(seconds); }}
function collection(element, method) {{
  try {{ var values = element[method](); var result = []; for (var i = 0; i < values.length; i++) result.push(values[i]); return result; }}
  catch (_) {{ return []; }}
}}
function items(element) {{ return collection(element, "uiElements"); }}
function role(element) {{ try {{ return String(element.role()); }} catch (_) {{ return ""; }} }}
function label(element) {{
  try {{ var name = String(element.name()); if (name && name !== "undefined" && name !== "null") return name; }} catch (_) {{}}
  try {{ var title = String(element.title()); if (title && title !== "undefined" && title !== "null") return title; }} catch (_) {{}}
  return "";
}}
function isSaveButton(element) {{ return role(element) === "AXButton" && /^(Сохранить|Save)$/.test(label(element)); }}
function isCancelButton(element) {{ return role(element) === "AXButton" && /^(Отменить|Cancel)$/.test(label(element)); }}
function isNativeContainer(element) {{
  var r = role(element);
  return r === "AXWindow" || r === "AXSheet" || r === "AXDialog" || r === "AXGroup" ||
    r === "AXPopover" || r === "AXSplitGroup" || r === "AXContainer";
}}
function saveGroup(element) {{
  if (!isNativeContainer(element)) return false;
  var children = items(element), fields = 0, save = 0, cancel = 0;
  for (var i = 0; i < children.length; i++) {{
    var r = role(children[i]);
    if (r === "AXTextField") fields++;
    if (isSaveButton(children[i])) save++;
    if (isCancelButton(children[i])) cancel++;
  }}
  return fields === 2 && save === 1 && cancel === 1;
}}
function walk(element, parent, rootWindow, path, depth, budget, matches) {{
  if (depth > 3 || !isNativeContainer(element)) return;
  budget.count++;
  if (budget.count > 48) throw new Error("accessibility bound");
  if (saveGroup(element)) matches.push({{element: element, parent: parent, rootWindow: rootWindow, path: path}});
  var children = items(element);
  for (var i = 0; i < children.length; i++) {{
    if (isNativeContainer(children[i])) walk(children[i], element, rootWindow, path + "/" + i, depth + 1, budget, matches);
  }}
}}
function findSaveGroups() {{
  var matches = [], wins = process.windows(), budget = {{count: 0}};
  // Chrome's native Save panel is an AXSheet/AXDialog directly under its
  // browser window. Never walk the browser's web area or unrelated AXGroups.
  for (var i = 0; i < wins.length; i++) {{
    var children = items(wins[i]);
    for (var j = 0; j < children.length; j++) {{
      var childRole = role(children[j]);
      if (childRole === "AXSheet" || childRole === "AXDialog")
        walk(children[j], wins[i], wins[i], "w" + i + "/" + j, 0, budget, matches);
    }}
  }}
  return matches;
}}
var lastSaveGroups = [];
function saveGroupsAre(count) {{
  lastSaveGroups = findSaveGroups();
  return lastSaveGroups.length === count;
}}
function goGroups(parent) {{
  if (!parent || !/^(AXSheet|AXDialog)$/.test(role(parent))) return [];
  var candidates = [], siblings = items(parent);
  for (var i = 0; i < siblings.length; i++) {{
    if (!isNativeContainer(siblings[i])) continue;
    if (saveGroup(siblings[i])) continue;
    var children = items(siblings[i]), fields = [], save = 0, cancel = 0;
    for (var j = 0; j < children.length; j++) {{
      if (role(children[j]) === "AXTextField") fields.push(children[j]);
      if (isSaveButton(children[j])) save++;
      if (isCancelButton(children[j])) cancel++;
    }}
    if (fields.length === 1 && save === 0 && cancel === 0) candidates.push({{group: siblings[i], field: fields[0]}});
  }}
  return candidates;
}}
function currentIdentity() {{
  if (!chrome.running() || chrome.windows().length === 0) throw new Error("Chrome unavailable");
  var win = chrome.windows[0], tab = win.activeTab, bounds = win.bounds();
  return {{url: String(tab.url()), id: String(tab.id()), window: String(win.id()),
    title: String(win.name()), bounds: bounds}};
}}
function coordinatePair(value, first, second) {{
  try {{
    var x = Array.isArray(value) ? Number(value[0]) : Number(value[first]);
    var y = Array.isArray(value) ? Number(value[1]) : Number(value[second]);
    if (isFinite(x) && isFinite(y)) return [x, y];
  }} catch (_) {{}}
  return null;
}}
function normalizedRect(value) {{
  try {{
    if (Array.isArray(value) && value.length === 4) {{
      var a = value.map(Number);
      if (a.every(isFinite)) return a;
    }}
    var x = Number(value.x), y = Number(value.y), width = Number(value.width), height = Number(value.height);
    if (isFinite(x) && isFinite(y) && isFinite(width) && isFinite(height) && width > 0 && height > 0)
      return [x, y, x + width, y + height];
  }} catch (_) {{}}
  return null;
}}
function windowTitleMatches(actual, expected) {{
  if (actual === expected) return true;
  return [" - Google Chrome", " – Google Chrome", " — Google Chrome"]
    .some(suffix => actual === expected + suffix);
}}
function axGeometry(element) {{
  try {{
    var position = coordinatePair(element.position(), "x", "y");
    var size = coordinatePair(element.size(), "width", "height");
    if (position && size && size[0] > 0 && size[1] > 0)
      return {{x: position[0], y: position[1], width: size[0], height: size[1]}};
  }} catch (_) {{}}
  return null;
}}
function sameGeometry(a, b) {{
  return Boolean(a && b && Math.abs(a.x - b.x) <= 2 && Math.abs(a.y - b.y) <= 2 &&
    Math.abs(a.width - b.width) <= 2 && Math.abs(a.height - b.height) <= 2);
}}
function sourceWindowMatches(candidate, expected) {{
  try {{
    if (role(candidate) !== "AXWindow" || !windowTitleMatches(String(candidate.name()), expected.title)) return false;
    var actual = axGeometry(candidate), rect = normalizedRect(expected.bounds);
    return Boolean(actual && rect && sameGeometry(actual, {{x: rect[0], y: rect[1], width: rect[2] - rect[0], height: rect[3] - rect[1]}}));
  }} catch (_) {{ return false; }}
}}
function sourceWindowCount(expected) {{
  var count = 0, windows = process.windows();
  for (var i = 0; i < windows.length; i++) if (sourceWindowMatches(windows[i], expected)) count++;
  return count;
}}
function focusedDocumentWindowMatches(expected) {{
  try {{
    var focused = process.attributes.byName("AXFocusedWindow").value();
    return Boolean(focused && sourceWindowMatches(focused, expected) && sourceWindowCount(expected) === 1);
  }} catch (_) {{ return false; }}
}}
function focusedOwnedSaveSheetMatches(expected, groups) {{
  try {{
    if (!openedByUs || groups.length !== 1 || role(groups[0].parent) !== "AXSheet" ||
        sourceWindowCount(expected) !== 1 || !sourceWindowMatches(groups[0].rootWindow, expected)) return false;
    var sheet = groups[0].parent, directSheets = items(groups[0].rootWindow).filter(function (child) {{ return role(child) === "AXSheet"; }});
    if (directSheets.length !== 1 || String(directSheets[0].name()) !== String(sheet.name()) ||
        !sameGeometry(axGeometry(directSheets[0]), axGeometry(sheet))) return false;
    var focused = process.attributes.byName("AXFocusedWindow").value();
    return Boolean(focused && role(focused) === "AXSheet" &&
      String(focused.name()) === String(sheet.name()) && sameGeometry(axGeometry(focused), axGeometry(sheet)));
  }} catch (_) {{ return false; }}
}}
function sameIdentity(expected, allowOwnedSaveSheet) {{
  var now = currentIdentity();
  if (now.url !== expectedURL || now.url !== expected.url || now.id !== expected.id || now.window !== expected.window) return false;
  if (!allowOwnedSaveSheet || !openedByUs) return focusedDocumentWindowMatches(expected);
  var groups = lastSaveGroups = findSaveGroups();
  if (groups.length === 0) return focusedDocumentWindowMatches(expected);
  return focusedOwnedSaveSheetMatches(expected, groups);
}}
function waitFor(test, maxMilliseconds) {{
  var end = Math.min(deadline, Date.now() + maxMilliseconds);
  while (Date.now() < end) {{ if (test()) return true; nap(0.35); }}
  return test();
}}
function cancelOwnedDialog() {{
  var groups = lastSaveGroups = findSaveGroups();
  if (groups.length === 0) return true;
  if (groups.length !== 1 || !process.frontmost()) return false;
  var parent = groups[0].parent;
  if (goGroups(parent).length === 1) {{
    if (!process.frontmost() || !sameIdentity(initialIdentity, true)) return false;
    systemEvents.keyCode(53);
    if (!waitFor(function () {{ return goGroups(parent).length === 0; }}, 1500)) return false;
    groups = lastSaveGroups = findSaveGroups();
    if (groups.length !== 1) return groups.length === 0;
  }}
  var children = items(groups[0].element), cancels = [];
  for (var i = 0; i < children.length; i++) if (isCancelButton(children[i])) cancels.push(children[i]);
  if (cancels.length !== 1 || !cancels[0].enabled() || !process.frontmost() || !sameIdentity(initialIdentity, true)) return false;
  cancels[0].click();
  return waitFor(function () {{ return saveGroupsAre(0); }}, 1500);
}}
try {{
  var initialIdentity = currentIdentity();
  if (initialIdentity.url !== expectedURL) throw new Error("tab");
  if (!saveGroupsAre(0)) throw new Error("modal");
  process.frontmost = true;
  if (!waitFor(function () {{ return Boolean(process.frontmost()); }}, 3000)) throw new Error("focus");
  if (!sameIdentity(initialIdentity) || !saveGroupsAre(0)) throw new Error("preflight");
  markPhase("preflight_complete");

  var menuBars = collection(process, "menuBars");
  if (menuBars.length !== 1) throw new Error("menu bar");
  var barItems = collection(menuBars[0], "menuBarItems");
  var fileItems = barItems.filter(function (item) {{ return role(item) === "AXMenuBarItem" && /^(Файл|File)$/.test(label(item)); }});
  if (fileItems.length !== 1 || !fileItems[0].enabled()) throw new Error("File menu");
  fileItems[0].click();
  var fileMenus = collection(fileItems[0], "menus");
  if (fileMenus.length !== 1) throw new Error("File menu unavailable");
  var menuItems = collection(fileMenus[0], "menuItems");
  var saveAsItems = menuItems.filter(function (item) {{ return role(item) === "AXMenuItem" && /^((Сохранить страницу как)|(Save Page As))/.test(label(item)); }});
  if (saveAsItems.length !== 1 || !saveAsItems[0].enabled()) throw new Error("Save Page As unavailable");
  openedByUs = true;
  saveAsItems[0].click();
  if (!waitFor(function () {{ return saveGroupsAre(1); }}, 7000)) throw new Error("Save panel unavailable");
  markPhase("save_panel_open");

  var group = lastSaveGroups = findSaveGroups();
  if (group.length !== 1) throw new Error("Save panel ambiguous");
  var parentSheet = group[0].parent, parentPath = group[0].path.slice(0, group[0].path.lastIndexOf("/"));
  if (!parentSheet || !/^(AXSheet|AXDialog)$/.test(role(parentSheet)) || items(parentSheet).filter(saveGroup).length !== 1) throw new Error("Save panel structure");
  if (!process.frontmost() || !sameIdentity(initialIdentity, true)) throw new Error("focus drift");
  systemEvents.keyCode(5, {{using: ["command down", "shift down"]}});
  if (!waitFor(function () {{ return goGroups(parentSheet).length === 1; }}, 4000)) throw new Error("Go To Folder unavailable");
  markPhase("folder_dialog_open");
  var go = goGroups(parentSheet);
  if (go.length !== 1) throw new Error("Go To Folder ambiguous");
  go[0].field.value = stagingDirectory;
  if (!process.frontmost() || !sameIdentity(initialIdentity, true)) throw new Error("focus drift");
  systemEvents.keyCode(36);
  if (!waitFor(function () {{ return goGroups(parentSheet).length === 0; }}, 4000)) throw new Error("Go To Folder did not close");
  markPhase("folder_selected");

  var reacquired = lastSaveGroups = findSaveGroups();
  if (reacquired.length !== 1 || reacquired[0].path.slice(0, reacquired[0].path.lastIndexOf("/")) !== parentPath) throw new Error("Save panel changed");
  if (!process.frontmost() || !sameIdentity(initialIdentity, true)) throw new Error("focus drift");
  var direct = items(reacquired[0].element), nameFields = [], saveButtons = [];
  for (var f = 0; f < direct.length; f++) {{
    if (role(direct[f]) === "AXTextField") nameFields.push(direct[f]);
    if (isSaveButton(direct[f])) saveButtons.push(direct[f]);
  }}
  if (nameFields.length !== 2 || saveButtons.length !== 1 || !saveButtons[0].enabled()) throw new Error("Save controls changed");
  markPhase("save_controls_ready");
  nameFields[0].value = "document.pdf";
  if (!process.frontmost() || !sameIdentity(initialIdentity, true)) throw new Error("focus drift");
  saveClicked = true;
  saveButtons[0].click();
  markPhase("save_clicked");
  if (!waitFor(function () {{ return saveGroupsAre(0); }}, 9000)) throw new Error("Save panel did not close");
  markPhase("save_panel_closed");
  if (!process.frontmost() || !sameIdentity(initialIdentity)) throw new Error("tab or focus changed");
  result = "NATIVE_SAVE_COMPLETE";
}} catch (_) {{
  markPhase(saveClicked ? "failed_after_save_click" : "failed_before_save_click");
  if (saveClicked) result = "NATIVE_SAVE_UNKNOWN";
  else if (!openedByUs || cancelOwnedDialog()) result = "NATIVE_SAVE_NOT_SAVED";
  else result = "NATIVE_SAVE_UNKNOWN";
}}
if (result === "NATIVE_SAVE_COMPLETE") markPhase("complete");
result;
'''


def _parse_native_phases(stderr: str | bytes | None) -> list[dict]:
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="ignore")
    phases = []
    for line in (stderr or "").splitlines():
        match = re.fullmatch(r"NATIVE_PHASE ([a-z_]+) (\d{1,10})", line.strip())
        if not match or match[1] not in NATIVE_PHASES:
            continue
        phases.append({"phase": match[1], "elapsed_ms": int(match[2])})
    return phases


def _write_native_diagnostic(inbox: Path, status: str, phases: list[dict], elapsed_ms: int | None = None) -> None:
    """Best-effort private fixed-vocabulary telemetry, outside native staging."""
    if status not in NATIVE_DIAGNOSTIC_STATUSES:
        return
    path = inbox / ".native-save-diagnostics.json"
    if path.is_symlink():
        return
    descriptor = None
    temporary = None
    try:
        attempts = []
        if path.exists():
            if path.stat().st_size > 64 * 1024 or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
                return
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("schema") != 1 or not isinstance(existing.get("attempts"), list):
                return
            attempts = []
            for old in existing["attempts"]:
                if not isinstance(old, dict) or old.get("status") not in NATIVE_DIAGNOSTIC_STATUSES:
                    continue
                old_phases = old.get("phases")
                if not isinstance(old_phases, list):
                    continue
                old_row = {
                    "status": old["status"],
                    "phases": [
                        {"phase": phase["phase"], "elapsed_ms": phase["elapsed_ms"]}
                        for phase in old_phases
                        if isinstance(phase, dict) and phase.get("phase") in NATIVE_PHASES
                        and type(phase.get("elapsed_ms")) is int and 0 <= phase["elapsed_ms"] <= 3_600_000
                    ][-16:],
                }
                timestamp = old.get("recorded_at_utc")
                if isinstance(timestamp, str) and len(timestamp) <= 40 and re.fullmatch(r"[0-9T:.+Z-]+", timestamp):
                    old_row["recorded_at_utc"] = timestamp
                old_elapsed = old.get("elapsed_ms")
                if type(old_elapsed) is int and 0 <= old_elapsed <= 3_600_000:
                    old_row["elapsed_ms"] = old_elapsed
                attempts.append(old_row)
        safe_phases = [{"phase": row["phase"], "elapsed_ms": row["elapsed_ms"]}
                       for row in phases if row.get("phase") in NATIVE_PHASES
                       and type(row.get("elapsed_ms")) is int and row["elapsed_ms"] >= 0]
        row = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
               "status": status, "phases": safe_phases[-16:]}
        if type(elapsed_ms) is int and elapsed_ms >= 0:
            row["elapsed_ms"] = min(elapsed_ms, 3_600_000)
        attempts.append(row)
        value = {"schema": 1, "attempts": attempts[-20:]}
        fd, name = tempfile.mkstemp(prefix=".native-diagnostic-", dir=inbox)
        temporary = Path(name)
        descriptor = fd
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        return
    finally:
        if descriptor is not None:
            try: os.close(descriptor)
            except OSError: pass
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _native_save_dialog(staging_path: Path, expected_url: str, native_process_id: int | None = None) -> None:
    """Save through the current Chrome session without accessing page data."""
    if sys.platform != "darwin":
        raise DownloadError("Native Chrome Save is available only on macOS.")
    script = _native_save_script(expected_url, str(staging_path.parent.resolve()), native_process_id)
    started = time.monotonic()
    def record(status: str, stderr: str | bytes | None = None) -> None:
        try:
            stage_dir = staging_path.parent
            if not stage_dir.name.startswith(".native-kad-save-"):
                return
            _write_native_diagnostic(
                stage_dir.parent, status, _parse_native_phases(stderr),
                max(0, round((time.monotonic() - started) * 1000)),
            )
        except Exception:
            pass
    stderr = ""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-"],
            input=script,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        stderr = getattr(result, "stderr", "")
    except subprocess.TimeoutExpired as error:
        record("process_timeout", error.stderr)
        raise SaveOutcomeUnknown("Native Save outcome is uncertain; private staging is retained for reconciliation.") from None
    except OSError:
        record("launch_error")
        raise DownloadError("Could not launch native Chrome Save; no file was published.") from None
    except subprocess.SubprocessError:
        record("process_error")
        raise SaveOutcomeUnknown("Native Save outcome is uncertain; private staging is retained for reconciliation.") from None
    status = result.stdout.strip()
    if result.returncode == 0 and status == "NATIVE_SAVE_COMPLETE":
        record("complete", stderr)
        return
    if result.returncode == 0 and status == "NATIVE_SAVE_NOT_SAVED":
        record("not_saved", stderr)
        raise NativeSaveNotStarted("Native Save stopped before saving; no file was published.")
    record("unknown", stderr)
    raise SaveOutcomeUnknown("Native Save outcome is uncertain; private staging is retained for reconciliation.")


def _snapshot_saved_pdf(path: Path) -> bytes:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > MAX_BYTES:
                raise DownloadError("Native Save did not produce a bounded regular PDF file.")
            data = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            stable = (
                before.st_dev == after.st_dev
                and before.st_ino == after.st_ino
                and before.st_size == after.st_size == len(data)
                and before.st_mtime_ns == after.st_mtime_ns
                and before.st_ctime_ns == after.st_ctime_ns
            )
            if not stable or len(data) > MAX_BYTES or not data.startswith(b"%PDF-"):
                raise DownloadError("Native Save did not produce a stable PDF snapshot.")
    except DownloadError:
        raise
    except OSError:
        raise DownloadError("Native Save did not produce a readable PDF file.") from None
    return data


def _validate_pdf_snapshot(data: bytes, staging_directory: Path) -> None:
    verify_path = None
    try:
        fd, verify_path = tempfile.mkstemp(prefix=".pdf-verify-", dir=staging_directory)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise DownloadError("Could not prepare a private PDF completeness check.") from None
    pdfinfo = shutil.which("pdfinfo")
    if not pdfinfo:
        os.unlink(verify_path)
        raise DownloadError("Local PDF completeness check is unavailable; no file was published.")
    try:
        result = subprocess.run(
            [pdfinfo, verify_path],
            check=False,
            capture_output=True,
            text=True,
            timeout=PDFINFO_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        raise DownloadError("Local PDF completeness check failed; no file was published.") from None
    finally:
        try:
            os.unlink(verify_path)
        except OSError:
            pass
    if result.returncode != 0:
        raise DownloadError("Local PDF completeness check failed; no file was published.")


def save_current_pdf(
    staging_path: Path,
    *,
    expected_url: str | None = None,
    native_save_driver=None,
    native_process_id: int | None = None,
) -> Path:
    """Use the qualified native Save flow for the unchanged current Chrome PDF tab."""
    before = validate_pdf_url(current_chrome_url())
    if expected_url is not None and before != validate_pdf_url(expected_url):
        raise DownloadError("The current Chrome PDF tab changed; no file was saved.")
    if staging_path.exists() or staging_path.is_symlink():
        raise DownloadError("The private staging path is not empty; no file was saved.")
    try:
        if native_save_driver is None:
            _native_save_dialog(staging_path, before, native_process_id)
        else:
            native_save_driver(staging_path, before)
    except SaveOutcomeUnknown:
        raise
    except DownloadError:
        raise
    except Exception:
        raise SaveOutcomeUnknown("Native Save outcome is uncertain; private staging is retained for reconciliation.") from None
    try:
        after = validate_pdf_url(current_chrome_url())
        if after != before:
            raise SaveOutcomeUnknown("The current Chrome tab changed; private staging is retained for reconciliation.")
        info = staging_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > MAX_BYTES:
            raise SaveOutcomeUnknown("Native Save produced an unverified local file; keep staging for reconciliation.")
        return staging_path
    except SaveOutcomeUnknown:
        raise
    except Exception:
        raise SaveOutcomeUnknown("Native Save produced an unverified local file; keep staging for reconciliation.") from None


@contextmanager
def _inbox_lock(inbox: Path):
    lock_path = inbox / ".native-save.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = None
    try:
        fd = os.open(lock_path, flags, 0o600)
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fd is not None:
            os.close(fd)
        raise DownloadError("Another local Save operation is active; wait and inspect before retrying.") from None
    except OSError:
        if fd is not None:
            os.close(fd)
        raise DownloadError("Could not lock the private research inbox.") from None
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _unresolved_save_exists(inbox: Path) -> bool:
    marker = inbox / ".native-save-pending.json"
    if marker.exists() or marker.is_symlink():
        return True
    return any(path.is_dir() or path.is_symlink() for path in inbox.glob(".native-kad-save-*"))


def _remove_pending_marker(marker: Path, expected: dict) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(marker, flags)
        with os.fdopen(fd, "rb") as stream:
            current = json.loads(stream.read(16 * 1024).decode("utf-8"))
        if current != expected:
            raise ValueError
        marker.unlink()
    except (OSError, ValueError, TypeError):
        raise DownloadError("Published PDF is verified but its pending marker remains; reconcile before retrying.") from None


def _pending_marker_matches(marker: Path, expected: dict) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(marker, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                return False
            raw = stream.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            return False
        return json.loads(raw.decode("utf-8")) == expected
    except (OSError, ValueError, TypeError):
        return False


def _clear_confirmed_empty_stage(inbox: Path, marker: Path, expected: dict, stage_dir: Path) -> None:
    """Clear only the exact, owned, empty stage for an explicit no-save result."""
    try:
        if (stage_dir.parent.resolve() != inbox.resolve() or stage_dir.name != expected.get("stage_directory")
                or not stage_dir.name.startswith(".native-kad-save-") or not _pending_marker_matches(marker, expected)):
            raise ValueError
        info = stage_dir.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or next(stage_dir.iterdir(), None) is not None):
            raise ValueError
        stage_dir.rmdir()
        if not _pending_marker_matches(marker, expected):
            raise ValueError
        marker.unlink()
    except (OSError, ValueError, TypeError):
        raise DownloadError("Native Save reported no file, but private staging did not match an empty owned stage; reconcile before retrying.") from None


def save_and_publish(url: str, directory: Path, *, native_save_driver=None,
                     native_process_id: int | None = None) -> DownloadResult:
    """Save one current-tab PDF natively, verify it locally, then publish by hash."""
    source_url = validate_pdf_url(url)
    inbox = _private_directory(Path(directory))
    with _inbox_lock(inbox):
        if _unresolved_save_exists(inbox):
            raise DownloadError("A prior native Save is unresolved; reconcile private staging before retrying.")
        try:
            stage_dir = Path(tempfile.mkdtemp(prefix=".native-kad-save-", dir=inbox))
            os.chmod(stage_dir, 0o700)
        except OSError:
            raise DownloadError("Could not prepare private PDF staging.") from None
        staged_file = stage_dir / "document.pdf"
        marker = inbox / ".native-save-pending.json"
        pending = {
            "source_url": source_url,
            "stage_directory": stage_dir.name,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        try:
            marker_created = _atomic_create(
                marker,
                lambda output: output.write(json.dumps(pending, sort_keys=True).encode("utf-8")),
            )
            if not marker_created:
                raise DownloadError("A prior native Save is unresolved; reconcile private staging before retrying.")
            from scripts.dev.quality import corpus_acquisition as acquisition
            repository = Path(__file__).resolve().parents[3]
            profile_root = inbox.parent if not inbox.parent.is_relative_to(repository) else None
            try:
                with acquisition.profile_stage(profile_root, "source.native_save") if profile_root else nullcontext():
                    save_current_pdf(staged_file, expected_url=source_url,
                                     native_save_driver=native_save_driver,
                                     native_process_id=native_process_id)
            except NativeSaveNotStarted:
                _clear_confirmed_empty_stage(inbox, marker, pending, stage_dir)
                raise
            with acquisition.profile_stage(profile_root, "source.verify_pdf") if profile_root else nullcontext():
                data = _snapshot_saved_pdf(staged_file)
                _validate_pdf_snapshot(data, stage_dir)
            with acquisition.profile_stage(profile_root, "source.verify_publish") if profile_root else nullcontext():
                digest = hashlib.sha256(data).hexdigest()
                size = len(data)
                pdf_path = inbox / f"{digest}.pdf"
                created = _atomic_create(pdf_path, lambda output: output.write(data))
                _verify_existing_pdf(pdf_path, digest, size)
                source_key = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
                receipt_path = inbox / f"{digest}.{source_key}.receipt.json"
                receipt = {
                    "source_url": source_url,
                    "sha256": digest,
                    "saved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "bytes": size,
                }
                receipt_created = _atomic_create(
                    receipt_path,
                    lambda output: output.write(json.dumps(receipt, sort_keys=True).encode("utf-8")),
                )
                if not receipt_created:
                    _verify_existing_receipt(receipt_path, digest, source_url, size)
                else:
                    _verify_existing_receipt(receipt_path, digest, source_url, size)
            _remove_pending_marker(marker, pending)
            try:
                shutil.rmtree(stage_dir)
            except OSError:
                pass
            return DownloadResult(digest=digest, size=size, duplicate=not created, path=pdf_path)
        except NativeSaveNotStarted:
            raise NativeSaveNotStarted("Native Save stopped before saving; no file was published.") from None
        except SaveOutcomeUnknown:
            raise DownloadError("Native Save outcome is unresolved; private staging is retained. Reconcile before retrying.") from None
        except DownloadError:
            raise DownloadError("Native Save or publication did not complete; private staging is retained. Reconcile before retrying.") from None
        except Exception:
            raise DownloadError("Native Save or publication is unresolved; private staging is retained. Reconcile before retrying.") from None


def default_inbox() -> Path:
    repository = Path(__file__).resolve().parents[3]
    return _default_inbox_for(repository)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.exit(2, "Invalid command-line arguments.\n")


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description=__doc__)
    args = parser.parse_args(argv)
    try:
        source_url = current_chrome_url()
        result = save_and_publish(source_url, default_inbox())
    except DownloadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    outcome = "duplicate" if result.duplicate else "saved"
    print(f"{outcome} one PDF ({result.size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
