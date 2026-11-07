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
from contextlib import contextmanager
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


def _native_save_script(expected_url: str, staging_directory: str) -> str:
    """Build a JXA script that uses only Chrome's native Save Page As dialog."""
    expected_literal = json.dumps(expected_url)
    directory_literal = json.dumps(staging_directory)
    return f'''ObjC.import("Foundation");
var chrome = Application("com.google.Chrome");
var systemEvents = Application("System Events");
var process = systemEvents.processes["Google Chrome"];
var expectedURL = {expected_literal};
var stagingDirectory = {directory_literal};
var deadline = Date.now() + 26000;
var result = "NATIVE_SAVE_NOT_SAVED";
var saveClicked = false;
var openedByUs = false;
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
function walk(element, parent, path, depth, budget, matches) {{
  if (depth > 3 || !isNativeContainer(element)) return;
  budget.count++;
  if (budget.count > 48) throw new Error("accessibility bound");
  if (saveGroup(element)) matches.push({{element: element, parent: parent, path: path}});
  var children = items(element);
  for (var i = 0; i < children.length; i++) {{
    if (isNativeContainer(children[i])) walk(children[i], element, path + "/" + i, depth + 1, budget, matches);
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
        walk(children[j], wins[i], "w" + i + "/" + j, 0, budget, matches);
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
  var tab = chrome.windows[0].activeTab;
  return {{url: String(tab.url()), id: String(tab.id())}};
}}
function sameIdentity(expected) {{
  var now = currentIdentity();
  return now.url === expectedURL && now.url === expected.url && now.id === expected.id;
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
    if (!process.frontmost() || !sameIdentity(initialIdentity)) return false;
    systemEvents.keyCode(53);
    if (!waitFor(function () {{ return goGroups(parent).length === 0; }}, 1500)) return false;
    groups = lastSaveGroups = findSaveGroups();
    if (groups.length !== 1) return groups.length === 0;
  }}
  var children = items(groups[0].element), cancels = [];
  for (var i = 0; i < children.length; i++) if (isCancelButton(children[i])) cancels.push(children[i]);
  if (cancels.length !== 1 || !cancels[0].enabled() || !process.frontmost() || !sameIdentity(initialIdentity)) return false;
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

  var group = lastSaveGroups = findSaveGroups();
  if (group.length !== 1) throw new Error("Save panel ambiguous");
  var parentSheet = group[0].parent, parentPath = group[0].path.slice(0, group[0].path.lastIndexOf("/"));
  if (!parentSheet || !/^(AXSheet|AXDialog)$/.test(role(parentSheet)) || items(parentSheet).filter(saveGroup).length !== 1) throw new Error("Save panel structure");
  if (!process.frontmost() || !sameIdentity(initialIdentity)) throw new Error("focus drift");
  systemEvents.keyCode(5, {{using: ["command down", "shift down"]}});
  if (!waitFor(function () {{ return goGroups(parentSheet).length === 1; }}, 4000)) throw new Error("Go To Folder unavailable");
  var go = goGroups(parentSheet);
  if (go.length !== 1) throw new Error("Go To Folder ambiguous");
  go[0].field.value = stagingDirectory;
  if (!process.frontmost() || !sameIdentity(initialIdentity)) throw new Error("focus drift");
  systemEvents.keyCode(36);
  if (!waitFor(function () {{ return goGroups(parentSheet).length === 0; }}, 4000)) throw new Error("Go To Folder did not close");

  var reacquired = lastSaveGroups = findSaveGroups();
  if (reacquired.length !== 1 || reacquired[0].path.slice(0, reacquired[0].path.lastIndexOf("/")) !== parentPath) throw new Error("Save panel changed");
  if (!process.frontmost() || !sameIdentity(initialIdentity)) throw new Error("focus drift");
  var direct = items(reacquired[0].element), nameFields = [], saveButtons = [];
  for (var f = 0; f < direct.length; f++) {{
    if (role(direct[f]) === "AXTextField") nameFields.push(direct[f]);
    if (isSaveButton(direct[f])) saveButtons.push(direct[f]);
  }}
  if (nameFields.length !== 2 || saveButtons.length !== 1 || !saveButtons[0].enabled()) throw new Error("Save controls changed");
  nameFields[0].value = "document.pdf";
  if (!process.frontmost() || !sameIdentity(initialIdentity)) throw new Error("focus drift");
  saveClicked = true;
  saveButtons[0].click();
  if (!waitFor(function () {{ return saveGroupsAre(0); }}, 9000)) throw new Error("Save panel did not close");
  if (!process.frontmost() || !sameIdentity(initialIdentity)) throw new Error("tab or focus changed");
  result = "NATIVE_SAVE_COMPLETE";
}} catch (_) {{
  if (saveClicked) result = "NATIVE_SAVE_UNKNOWN";
  else if (!openedByUs || cancelOwnedDialog()) result = "NATIVE_SAVE_NOT_SAVED";
  else result = "NATIVE_SAVE_UNKNOWN";
}}
result;
'''


def _native_save_dialog(staging_path: Path, expected_url: str) -> None:
    """Save through the current Chrome session without accessing page data."""
    if sys.platform != "darwin":
        raise DownloadError("Native Chrome Save is available only on macOS.")
    script = _native_save_script(expected_url, str(staging_path.parent.resolve()))
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-"],
            input=script,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise SaveOutcomeUnknown("Native Save outcome is uncertain; private staging is retained for reconciliation.") from None
    except OSError:
        raise DownloadError("Could not launch native Chrome Save; no file was published.") from None
    except subprocess.SubprocessError:
        raise SaveOutcomeUnknown("Native Save outcome is uncertain; private staging is retained for reconciliation.") from None
    status = result.stdout.strip()
    if result.returncode == 0 and status == "NATIVE_SAVE_COMPLETE":
        return
    if result.returncode == 0 and status == "NATIVE_SAVE_NOT_SAVED":
        raise NativeSaveNotStarted("Native Save stopped before saving; no file was published.")
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
) -> Path:
    """Use the qualified native Save flow for the unchanged current Chrome PDF tab."""
    before = validate_pdf_url(current_chrome_url())
    if expected_url is not None and before != validate_pdf_url(expected_url):
        raise DownloadError("The current Chrome PDF tab changed; no file was saved.")
    if staging_path.exists() or staging_path.is_symlink():
        raise DownloadError("The private staging path is not empty; no file was saved.")
    driver = native_save_driver or _native_save_dialog
    try:
        driver(staging_path, before)
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


def save_and_publish(url: str, directory: Path, *, native_save_driver=None) -> DownloadResult:
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
            try:
                save_current_pdf(staged_file, expected_url=source_url, native_save_driver=native_save_driver)
            except NativeSaveNotStarted:
                _clear_confirmed_empty_stage(inbox, marker, pending, stage_dir)
                raise
            data = _snapshot_saved_pdf(staged_file)
            _validate_pdf_snapshot(data, stage_dir)
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
