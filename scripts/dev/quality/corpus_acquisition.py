#!/usr/bin/env python3
"""Private, resumable acquisition queue for explicitly selected court PDFs.

Output is aggregate-only. Queue state, URLs, source identifiers, receipts, and
PDF bytes stay in a private directory outside the repository.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import email.utils
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[3]
STATE = "queue.json"
MIN_SPACING_SECONDS = 5.0
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_IMPORT_BYTES = 16 * 1024 * 1024
PDF_MAGIC = b"%PDF-"
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
KAD_PATH = re.compile(rf"^/Kad/PdfDocument/{UUID}/{UUID}/[A-Za-z0-9._-]+\.pdf$", re.I)
DIRECT_PDF_PATH = re.compile(rf"^/Document/Pdf/{UUID}/{UUID}/[A-Za-z0-9._-]+\.pdf$", re.I)
KAD_PDF_ORIGIN = "https://kad.arbitr.ru"
CHANNELS = {"kad-direct-pdf", "kad-browser-pdf", "internal-export", "browser-selected-pdf"}
METADATA_FIELDS = ("case_id", "case_number", "court", "year", "act_type", "participant_kind")


class QueueError(ValueError):
    pass


class SourcePause(Exception):
    def __init__(self, code: str, *, cooldown_seconds: float = 0, disable: bool = False):
        super().__init__(code)
        self.code = code
        self.cooldown_seconds = cooldown_seconds
        self.disable = disable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity(value) -> str:
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def private_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise QueueError("private_root_symlink")
    path = raw.resolve()
    if path.is_relative_to(ROOT) or ROOT.is_relative_to(path):
        raise QueueError("private_root_inside_repository")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise QueueError("private_root_permissions")
    for name in ("inbox", "receipts", "staging"):
        child = path / name
        child.mkdir(mode=0o700, exist_ok=True)
        if child.is_symlink() or child.stat().st_mode & 0o077:
            raise QueueError("private_storage_permissions")
    return path


def write_json(path: Path, value) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def load_queue(root: Path) -> dict:
    path = root / STATE
    if path.is_symlink():
        raise QueueError("queue_state_symlink")
    if not path.exists():
        return {"schema": 1, "created_at": utc_now(), "last_request_at": 0.0,
                "source_controls": {}, "items": []}
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise QueueError("queue_state_size")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("items"), list):
        raise QueueError("queue_state_schema")
    return data


@contextmanager
def queue_lock(root: Path):
    lock = root / ".queue.lock"
    if lock.is_symlink():
        raise QueueError("queue_lock_symlink")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise QueueError("queue_already_running") from None
        yield
    finally:
        os.close(descriptor)


def validate_kad_url(raw: str) -> str:
    if not isinstance(raw, str) or len(raw) > 2048:
        raise QueueError("invalid_source_url")
    parsed = urlsplit(raw)
    query_ok = parsed.query == "" if KAD_PATH.fullmatch(parsed.path) else parsed.query == "isAddStamp=True"
    if (parsed.scheme != "https" or parsed.netloc.lower() != "kad.arbitr.ru" or parsed.username
            or parsed.password or parsed.fragment or not (KAD_PATH.fullmatch(parsed.path) or DIRECT_PDF_PATH.fullmatch(parsed.path))
            or not query_ok):
        raise QueueError("invalid_source_url")
    return raw


def _local_pdf_path(raw: str, export_root: Path) -> Path:
    supplied = Path(raw)
    if supplied.is_symlink():
        raise QueueError("local_export_symlink")
    candidate = supplied.resolve(strict=True)
    base = export_root.resolve(strict=True)
    if (base.is_relative_to(ROOT) or ROOT.is_relative_to(base) or candidate == base
            or not candidate.is_relative_to(base) or not candidate.is_file()):
        raise QueueError("local_export_outside_root")
    if candidate.suffix.lower() != ".pdf":
        raise QueueError("local_export_not_pdf")
    return candidate


def parse_import(manifest: Path, export_root: Path | None = None) -> list[dict]:
    manifest = manifest.expanduser()
    if manifest.is_symlink() or manifest.resolve().is_relative_to(ROOT) or manifest.stat().st_size > MAX_IMPORT_BYTES:
        raise QueueError("import_manifest_size_or_symlink")
    rows = []
    seen = set()
    for line_no, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except Exception:
            raise QueueError(f"invalid_import_row_{line_no}") from None
        allowed_fields = {"source_id", "source_url", "channel", "local_path", "authority", *METADATA_FIELDS}
        if not isinstance(raw, dict) or set(raw) - allowed_fields:
            raise QueueError(f"invalid_import_row_{line_no}")
        source_id = raw.get("source_id")
        channel = raw.get("channel")
        if not isinstance(source_id, str) or not source_id or len(source_id) > 2000 or channel not in CHANNELS:
            raise QueueError(f"invalid_import_row_{line_no}")
        authority = raw.get("authority")
        if authority not in {None, "owner_assertion", "internal_export_terms"}:
            raise QueueError(f"invalid_import_row_{line_no}")
        metadata = {name: raw[name] for name in METADATA_FIELDS if name in raw}
        if type(metadata.get("year")) is int:
            metadata["year"] = str(metadata["year"])
        if any(not isinstance(value, str) or len(value) > 500 for value in metadata.values()):
            raise QueueError(f"invalid_import_row_{line_no}")
        item = {"source_id": source_id, "channel": channel, "status": "pending", "attempts": 0,
                "metadata": metadata, "authority": authority}
        if channel in {"kad-direct-pdf", "kad-browser-pdf"}:
            if not {"source_id", "source_url", "channel"}.issubset(raw) or "local_path" in raw:
                raise QueueError(f"invalid_import_row_{line_no}")
            item["source_url"] = validate_kad_url(raw.get("source_url"))
        else:
            if not {"source_id", "channel", "local_path"}.issubset(raw) or export_root is None:
                raise QueueError(f"invalid_import_row_{line_no}")
            item["local_path"] = str(_local_pdf_path(raw["local_path"], export_root))
            item["export_root"] = str(Path(export_root).resolve(strict=True))
            if raw.get("source_url"):
                item["source_url"] = validate_kad_url(raw["source_url"])
        item["source_key"] = identity([channel, source_id, item.get("source_url", ""), item.get("local_path", ""), metadata])
        if item["source_key"] in seen:
            continue
        seen.add(item["source_key"])
        rows.append(item)
    return rows


class _GuardedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute = urljoin(req.full_url, newurl)
        parsed = urlsplit(absolute)
        allowed_path = bool(KAD_PATH.fullmatch(parsed.path)) or bool(DIRECT_PDF_PATH.fullmatch(parsed.path))
        allowed_query = parsed.query in {"", "isAddStamp=True", "isAddStamp=false"}
        if (parsed.scheme != "https" or parsed.netloc.lower() != "kad.arbitr.ru" or parsed.username
                or parsed.password or parsed.fragment or not allowed_path or not allowed_query):
            raise SourcePause("redirect_outside_approved_pdf_route", disable=True)
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def _retry_after(headers) -> float:
    value = headers.get("Retry-After") if headers else None
    if not value:
        return 60.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            return 60.0


def download_kad(url: str, destination: Path, max_bytes: int = MAX_DOCUMENT_BYTES) -> tuple[bytes, str]:
    """Fetch one explicitly queued original URL without browser state or retries."""
    validate_kad_url(url)
    request = Request(url, headers={"Accept": "application/pdf"}, method="GET")
    opener = build_opener(_GuardedRedirect())
    try:
        response = opener.open(request, timeout=30)
    except HTTPError as error:
        if error.code in {401, 403}:
            raise SourcePause("http_access_denied", disable=True) from None
        if error.code == 429:
            raise SourcePause("http_rate_limited", cooldown_seconds=_retry_after(error.headers)) from None
        if 500 <= error.code < 600:
            raise SourcePause("http_server_error", cooldown_seconds=_retry_after(error.headers)) from None
        raise SourcePause("http_source_error", disable=400 <= error.code < 500) from None
    except URLError:
        raise SourcePause("source_unavailable", cooldown_seconds=60) from None
    with response:
        status = getattr(response, "status", 200)
        final = response.geturl()
        parsed = urlsplit(final)
        allowed = (parsed.scheme == "https" and parsed.netloc.lower() == "kad.arbitr.ru"
                   and (KAD_PATH.fullmatch(parsed.path) or DIRECT_PDF_PATH.fullmatch(parsed.path))
                   and parsed.query in {"", "isAddStamp=True", "isAddStamp=false"} and not parsed.fragment)
        if not allowed:
            raise SourcePause("final_url_outside_approved_pdf_route", disable=True)
        if _document_identity(final) != _document_identity(url):
            raise SourcePause("redirect_document_identity_changed", disable=True)
        if status == 429:
            raise SourcePause("http_rate_limited", cooldown_seconds=_retry_after(response.headers))
        if status in {401, 403}:
            raise SourcePause("http_access_denied", disable=True)
        content_type = (response.headers.get("Content-Type") or "").lower()
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > max_bytes:
                    raise SourcePause("document_size_limit")
            except ValueError:
                raise SourcePause("invalid_content_length") from None
        chunks = bytearray()
        while len(chunks) <= max_bytes:
            block = response.read(min(65536, max_bytes + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        data = bytes(chunks)
    if len(data) > max_bytes:
        raise SourcePause("document_size_limit", disable=True)
    head = data[:4096].lower()
    if any(marker in head for marker in (b"captcha", b"challenge", b"access denied", b"request blocked", b"forbidden")):
        raise SourcePause("access_challenge", disable=True)
    if not data.startswith(PDF_MAGIC) or "pdf" not in content_type:
        raise SourcePause("response_not_pdf", disable=True)
    if not data.endswith((b"%%EOF", b"%%EOF\n", b"%%EOF\r\n")):
        raise SourcePause("incomplete_pdf", cooldown_seconds=60)
    return data, final


def read_export(item: dict, max_bytes: int = MAX_DOCUMENT_BYTES) -> tuple[bytes, str]:
    path = Path(item["local_path"])
    export_root = Path(item["export_root"])
    if path.is_symlink() or not path.is_file() or _local_pdf_path(str(path), export_root) != path:
        raise QueueError("local_export_changed")
    if path.stat().st_size > max_bytes:
        raise QueueError("document_size_limit")
    data = path.read_bytes()
    if not data.startswith(PDF_MAGIC) or not data.endswith((b"%%EOF", b"%%EOF\n", b"%%EOF\r\n")):
        raise QueueError("local_export_not_complete_pdf")
    return data, path.name


def download_browser(item: dict, root: Path):
    """Navigate normally, then reuse the guarded native Chrome Save path."""
    url = validate_kad_url(item["source_url"])
    try:
        from scripts.dev.quality.kad_discovery import AccessChallenge, BrowserNavigationError, navigate_pdf
    except ImportError:
        raise SourcePause("ordinary_browser_adapter_unavailable", cooldown_seconds=60) from None
    try:
        active_url = navigate_pdf(url)
        validate_kad_url(active_url)
        if _document_identity(active_url) != _document_identity(url):
            raise SourcePause("browser_navigation_identity_changed", disable=True)
        from scripts.dev.quality.download_current_kad import NativeSaveNotStarted, save_and_publish
        try:
            result = save_and_publish(active_url, root / "inbox")
        except NativeSaveNotStarted:
            raise SourcePause("browser_save_not_started", cooldown_seconds=60) from None
        data = result.path.read_bytes()
        if digest(data) != result.digest or len(data) != result.size:
            raise SourcePause("browser_save_readback_mismatch", disable=True)
        return data, active_url, result.duplicate
    except AccessChallenge as error:
        reason = getattr(error, "reason", "challenge")
        if reason == "rate_limited":
            raise SourcePause("browser_rate_limited", cooldown_seconds=60) from None
        raise SourcePause("browser_access_challenge", disable=True) from None
    except BrowserNavigationError as error:
        reason = getattr(error, "reason", "navigation")
        if reason in {"identity", "unsupported"}:
            raise SourcePause("browser_navigation_rejected", disable=True) from None
        delay = min(60 * (2 ** max(0, int(item.get("attempts", 1)) - 1)), 3600)
        raise SourcePause("browser_navigation_transient", cooldown_seconds=delay) from None
    except SourcePause:
        raise
    except Exception:
        # Existing native helper leaves a private pending marker/staging folder
        # after an uncertain outcome. Never retry against that shared browser tab.
        inbox = root / "inbox"
        unresolved = (inbox / ".native-save-pending.json").exists() or any(
            inbox.glob(".native-kad-save-*"))
        if unresolved:
            raise SourcePause("browser_save_unresolved", disable=True) from None
        raise SourcePause("ordinary_browser_navigation_failed", cooldown_seconds=60) from None


def _document_identity(raw: str) -> tuple[str, str]:
    parsed = urlsplit(raw)
    match = KAD_PATH.fullmatch(parsed.path) or DIRECT_PDF_PATH.fullmatch(parsed.path)
    if not match:
        raise QueueError("invalid_source_url")
    parts = parsed.path.split("/")
    return parts[3].lower(), parts[4].lower()


def publish(root: Path, item: dict, data: bytes, final_url: str) -> tuple[str, bool]:
    if not data.startswith(PDF_MAGIC) or len(data) > MAX_DOCUMENT_BYTES:
        raise QueueError("document_validation")
    sha = digest(data)
    target = root / "inbox" / f"{sha}.pdf"
    duplicate = target.exists()
    if duplicate:
        if target.is_symlink() or digest(target.read_bytes()) != sha:
            raise QueueError("existing_object_integrity")
    else:
        fd, name = tempfile.mkstemp(prefix=".pdf-", dir=root / "staging")
        temporary = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or digest(target.read_bytes()) != sha:
                    raise QueueError("concurrent_object_integrity")
                duplicate = True
        finally:
            temporary.unlink(missing_ok=True)
    receipt = {"schema": 1, "queue_item": item["id"], "source_id": item["source_id"],
               "channel": item["channel"], "source_url": item.get("source_url"),
               "resolved_url": final_url if item["channel"] in {"kad-direct-pdf", "kad-browser-pdf"} else None,
               "candidate_metadata": item.get("metadata", {}), "authority": item.get("authority"),
               "sha256": sha, "size": len(data), "captured_at": utc_now()}
    write_json(root / "receipts" / f"{item['id']}.json", receipt)
    return sha, duplicate


def init(root: Path) -> dict:
    root = private_root(root)
    with queue_lock(root):
        state = load_queue(root)
        write_json(root / STATE, state)
    return {"initialized": True, "queued": len(state["items"])}


def enqueue(root: Path, manifest: Path, export_root: Path | None = None) -> dict:
    root = private_root(root)
    rows = parse_import(manifest, export_root)
    with queue_lock(root):
        state = load_queue(root)
        known = {item["source_key"] for item in state["items"]}
        added = 0
        for item in rows:
            if item["source_key"] in known:
                continue
            item["id"] = identity([item["source_key"], len(state["items"])])
            state["items"].append(item)
            known.add(item["source_key"])
            added += 1
        write_json(root / STATE, state)
    return {"added": added, "queued": sum(i["status"] == "pending" for i in state["items"])}


def run(root: Path, *, limit: int | None = None, max_run_bytes: int = 512 * 1024 * 1024,
        min_spacing_seconds: float = MIN_SPACING_SECONDS, now=time.time, sleep=time.sleep,
        fetch=download_kad, read_local=read_export) -> dict:
    if limit is not None and limit < 1:
        raise QueueError("invalid_run_limit")
    if max_run_bytes < 1 or min_spacing_seconds < MIN_SPACING_SECONDS:
        raise QueueError("invalid_run_budget_or_spacing")
    root = private_root(root)
    counts = {"downloaded": 0, "duplicates": 0, "failed": 0, "paused": 0, "bytes": 0}
    with queue_lock(root):
        state = load_queue(root)
        controls = state.setdefault("source_controls", {})
        pending = [item for item in state["items"] if item["status"] == "pending"]
        if not pending:
            return {**counts, "pending": 0, "state": "empty"}
        eligible = []
        for item in pending:
            control = controls.get(item["channel"], {})
            if not control.get("disabled") and float(control.get("cooldown_until", 0) or 0) <= now():
                eligible.append(item)
        if not eligible:
            disabled = next((controls.get(item["channel"], {}) for item in pending
                             if controls.get(item["channel"], {}).get("disabled")), None)
            if disabled:
                return {**counts, "pending": len(pending), "state": "disabled", "reason": disabled.get("reason", "adapter_disabled")}
            until = max(float(controls.get(item["channel"], {}).get("cooldown_until", 0) or 0) for item in pending)
            return {**counts, "pending": len(pending), "state": "cooldown", "retry_after_seconds": int(until - now()) + 1}
        selected = 0
        blocked_channels = set()
        for item in eligible:
            if limit is not None and selected >= limit:
                break
            if counts["bytes"] >= max_run_bytes:
                break
            channel = item["channel"]
            if channel in blocked_channels:
                continue
            control = controls.get(channel, {})
            if control.get("disabled"):
                break
            until = float(control.get("cooldown_until", 0) or 0)
            if until > now():
                break
            if channel in {"kad-direct-pdf", "kad-browser-pdf"}:
                last = float(state.get("last_request_at", 0) or 0)
                wait = last + min_spacing_seconds - now()
                if last and wait > 0:
                    sleep(wait)
                state["last_request_at"] = now()
                item["attempts"] += 1
                write_json(root / STATE, state)
            try:
                browser_duplicate = None
                if channel == "kad-direct-pdf":
                    data, final_url = fetch(item["source_url"], root / "staging")
                elif channel == "kad-browser-pdf":
                    data, final_url, browser_duplicate = download_browser(item, root)
                else:
                    data, final_url = read_local(item)
                if len(data) > max_run_bytes - counts["bytes"]:
                    counts["paused"] += 1
                    break
                sha, duplicate = publish(root, item, data, final_url)
                if browser_duplicate is not None:
                    duplicate = browser_duplicate
                item.update(status="complete", sha256=sha, size=len(data), completed_at=utc_now(), duplicate=duplicate)
                counts["bytes"] += len(data)
                counts["downloaded"] += 1
                counts["duplicates"] += int(duplicate)
                selected += 1
                write_json(root / STATE, state)
            except SourcePause as error:
                counts["failed"] += 1
                counts["paused"] += 1
                if error.disable:
                    controls[channel] = {"disabled": True, "reason": error.code, "updated_at": utc_now()}
                else:
                    controls[channel] = {"disabled": False, "reason": error.code,
                                         "cooldown_until": now() + max(error.cooldown_seconds, 1), "updated_at": utc_now()}
                write_json(root / STATE, state)
                blocked_channels.add(channel)
                continue
            except QueueError as error:
                item.update(status="failed", error=str(error), failed_at=utc_now())
                counts["failed"] += 1
                selected += 1
                write_json(root / STATE, state)
        remaining = sum(item["status"] == "pending" for item in state["items"])
        return {**counts, "pending": remaining, "state": "complete" if remaining == 0 else "paused" if counts["paused"] else "partial"}


def status(root: Path) -> dict:
    root = private_root(root)
    state = load_queue(root)
    counts = {name: sum(item["status"] == name for item in state["items"])
              for name in ("pending", "complete", "failed")}
    bytes_used = sum(p.stat().st_size for p in (root / "inbox").glob("*.pdf") if p.is_file() and not p.is_symlink())
    return {**counts, "bytes": bytes_used, "disabled_adapters": sum(bool(x.get("disabled")) for x in state.get("source_controls", {}).values()),
            "cooling_adapters": sum(float(x.get("cooldown_until", 0) or 0) > time.time()
                                    for x in state.get("source_controls", {}).values())}


def reset_adapter(root: Path, channel: str, *, confirm_source_restored: bool) -> dict:
    if channel not in CHANNELS or not confirm_source_restored:
        raise QueueError("explicit_source_restoration_confirmation_required")
    root = private_root(root)
    with queue_lock(root):
        state = load_queue(root)
        previous = state.setdefault("source_controls", {}).get(channel, {})
        state["source_controls"][channel] = {"disabled": False, "cooldown_until": 0,
                                             "reason": "operator_confirmed_source_restored",
                                             "updated_at": utc_now(),
                                             "previous_reason": previous.get("reason", "none")}
        write_json(root / STATE, state)
    return {"adapter_enabled": True, "queued": sum(item["status"] == "pending" and item["channel"] == channel
                                                      for item in state["items"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status"):
        p = commands.add_parser(name)
        p.add_argument("--root", required=True, type=Path)
    add = commands.add_parser("enqueue")
    add.add_argument("--root", required=True, type=Path)
    add.add_argument("--manifest", required=True, type=Path)
    add.add_argument("--export-root", type=Path)
    work = commands.add_parser("run")
    work.add_argument("--root", required=True, type=Path)
    work.add_argument("--limit", type=int)
    work.add_argument("--max-run-bytes", type=int, default=512 * 1024 * 1024)
    work.add_argument("--min-spacing-seconds", type=float, default=MIN_SPACING_SECONDS)
    reset = commands.add_parser("reset-adapter")
    reset.add_argument("--root", required=True, type=Path)
    reset.add_argument("--channel", required=True, choices=sorted(CHANNELS))
    reset.add_argument("--confirm-source-restored", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = (init(args.root) if args.command == "init" else status(args.root) if args.command == "status"
                  else enqueue(args.root, args.manifest, args.export_root) if args.command == "enqueue"
                  else run(args.root, limit=args.limit, max_run_bytes=args.max_run_bytes,
                           min_spacing_seconds=args.min_spacing_seconds) if args.command == "run"
                  else reset_adapter(args.root, args.channel,
                                     confirm_source_restored=args.confirm_source_restored))
    except (QueueError, OSError, ValueError):
        print("acquisition operation stopped; inspect private queue state", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
