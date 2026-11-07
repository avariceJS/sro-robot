#!/usr/bin/env python3
"""Bounded fresh-process coordinator for one KAD year/category stratum."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.dev.quality import corpus_acquisition as acquisition

DISCOVERY = ROOT / "scripts/dev/quality/kad_discovery.py"
MAX_RETRIES = 2
MAX_IDLE_SECONDS = 300
NAVIGATION_REASONS = {"timeout", "navigation", "unavailable"}
QUEUE_COOLDOWN_REASONS = {
    "browser_save_not_started", "browser_rate_limited", "browser_navigation_transient",
    "ordinary_browser_navigation_failed", "operator_confirmed_source_restored",
    "owned_save_cancel_reconciled",
}


def _atomic_state(path: Path, value: dict) -> None:
    acquisition.write_json(path, value)


@contextmanager
def _coordinator_lock(root: Path):
    path = root / ".coordinator.lock"
    if path.is_symlink():
        raise acquisition.QueueError("coordinator_lock_symlink")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise acquisition.QueueError("coordinator_already_running") from None
        yield
    finally:
        os.close(descriptor)


def _state_path(root: Path, year: int, category: str, month=None) -> Path:
    key = hashlib.sha256((f"{year}:{category}" + (f":{month}" if month is not None else "")).encode()).hexdigest()[:24]
    directory = root / "coordinator"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise acquisition.QueueError("coordinator_state_permissions")
    return directory / f"{key}.json"


def _load_state(path: Path, year: int, category: str, month=None) -> dict:
    if path.is_symlink():
        raise acquisition.QueueError("coordinator_state_symlink")
    if not path.exists():
        return {"schema": 1, "year": year, "category": category, "month": month, "retries": 0,
                "idle_seconds": 0, "status": "new", "resume_mode": "fresh"}
    if path.stat().st_size > 64 * 1024:
        raise acquisition.QueueError("coordinator_state_size")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != 1 or data.get("year") != year or data.get("category") != category or data.get("month") != month
            or (data.get("month") is not None and type(data["month"]) is not int)
            or type(data.get("retries")) is not int or not 0 <= data["retries"] <= MAX_RETRIES
            or type(data.get("idle_seconds")) is not int or not 0 <= data["idle_seconds"] <= MAX_IDLE_SECONDS
            or data.get("status") not in {"new", "ready", "cooldown", "complete", "blocked", "unknown", "exhausted"}
            or data.get("resume_mode") not in {"fresh", "resume-cases"}):
        raise acquisition.QueueError("coordinator_state_schema")
    retry_at = data.get("retry_at", 0)
    if (type(retry_at) not in {int, float} or not math.isfinite(retry_at) or retry_at < 0):
        raise acquisition.QueueError("coordinator_state_schema")
    return data


def _last_summary(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("status"), str):
            # Accept only the collector's bounded, sanitized summary vocabulary.
            status = value["status"]
            reason = value.get("reason")
            if status not in {"source-paused", "discovery-failed", "case-unavailable"}:
                continue
            if reason is not None and reason not in {"timeout", "navigation", "unavailable", "unsupported",
                                                      "identity", "internal", "challenge", "rate_limited",
                                                      "access_denied", "browser_rate_limited",
                                                      "browser_access_challenge", "browser_navigation_rejected",
                                                      "browser_target_unresolved"}:
                continue
            return {"status": status, "reason": reason}
    return {"status": "unknown", "reason": None}


def _has_unresolved_native_save(root: Path) -> bool:
    from scripts.dev.quality.download_current_kad import _unresolved_save_exists
    return _unresolved_save_exists(root / "inbox")


def _response_capture_config_present(root: Path) -> bool:
    path = root / "discovery" / "browser-response-capture.json"
    return path.exists() or path.is_symlink()


def _command(root: Path, year: int, category: str, cases: int, acts_per_case: int, resume: bool, owned_browser=False, month=None) -> list[str]:
    if owned_browser and _response_capture_config_present(root):
        raise acquisition.QueueError("response_discovery_unavailable")
    result = [sys.executable, str(DISCOVERY), "--root", str(root), "--year", str(year),
              "--category", category, "--cases", str(cases), "--acts-per-case", str(acts_per_case), "--download"]
    if resume:
        result.append("--resume-cases")
    if owned_browser:
        result.append("--owned-browser")
    if month is not None:
        result.extend(["--month", str(month)])
    return result


def coordinate(root: Path, *, year: int, category: str, cases: int = 25, acts_per_case: int = 1,
               runner=subprocess.run, now=time.time, sleep=time.sleep, process_timeout=1800, owned_browser=False, month=None) -> dict:
    if not 1900 <= year <= 2200 or category not in {"bankruptcy", "civil"}:
        raise acquisition.QueueError("invalid_stratum")
    if month is not None and (type(month) is not int or not 1 <= month <= 12):
        raise acquisition.QueueError("invalid_stratum_month")
    if not 1 <= cases <= 25 or not 1 <= acts_per_case <= 2:
        raise acquisition.QueueError("run_pulse_out_of_bounds")
    root = acquisition.private_root(root)
    state_path = _state_path(root, year, category, month)
    with _coordinator_lock(root):
        state = _load_state(state_path, year, category, month)
        if state.get("status") == "complete":
            state.update(retries=0, idle_seconds=0, status="new", resume_mode="fresh", cases_sha256=None)
        if state.get("status") in {"blocked", "unknown", "exhausted"}:
            return {"status": state["status"], "retries": state["retries"],
                    "idle_seconds": state["idle_seconds"]}
        if owned_browser and _response_capture_config_present(root):
            state.update(status="blocked", reason="response_discovery_unavailable")
            _atomic_state(state_path, state)
            return {"status": "blocked", "reason": "response_discovery_unavailable",
                    "retries": state["retries"], "idle_seconds": state["idle_seconds"]}
        control = acquisition.load_queue(root).get("source_controls", {}).get("kad-browser-pdf", {})
        if control.get("disabled"):
            state.update(status="blocked", reason="adapter_disabled")
            _atomic_state(state_path, state)
            return {"status": "blocked", "retries": state["retries"], "idle_seconds": state["idle_seconds"]}
        if control.get("reason") and control.get("reason") not in QUEUE_COOLDOWN_REASONS:
            state.update(status="blocked", reason="source_requires_inspection")
            _atomic_state(state_path, state)
            return {"status": "blocked", "retries": state["retries"], "idle_seconds": state["idle_seconds"]}
        active_cooldown = max(0.0, float(control.get("cooldown_until", 0) or 0) - now())
        if active_cooldown > 0 and state.get("status") != "cooldown":
            queue = acquisition.load_queue(root)
            last_request = float(queue.get("last_request_at", 0) or 0)
            wait = max(active_cooldown, last_request + acquisition.MIN_SPACING_SECONDS - now())
            charge = math.ceil(wait)
            if state["retries"] >= MAX_RETRIES or wait > MAX_IDLE_SECONDS or state["idle_seconds"] + charge > MAX_IDLE_SECONDS:
                state.update(status="exhausted", reason="cooldown_retry_budget_exhausted")
            else:
                state.update(status="cooldown", resume_mode="fresh", reason=control.get("reason"),
                             retries=state["retries"] + 1, idle_seconds=state["idle_seconds"] + charge,
                             retry_at=now() + wait)
            _atomic_state(state_path, state)
            if state["status"] == "exhausted":
                return {"status": "exhausted", "retries": state["retries"], "idle_seconds": state["idle_seconds"]}
        runs = 0
        while True:
            if state.get("status") == "cooldown":
                remaining = max(0.0, float(state.get("retry_at", 0) or 0) - now())
                while remaining > 0:
                    step = min(60.0, remaining)
                    sleep(step)
                    remaining = max(0.0, float(state.get("retry_at", 0) or 0) - now())
            queue = acquisition.load_queue(root)
            control = queue.get("source_controls", {}).get("kad-browser-pdf", {})
            if control.get("disabled"):
                state.update(status="blocked", reason="adapter_disabled")
                _atomic_state(state_path, state)
                return {"status": "blocked", "runs": runs, "retries": state["retries"],
                        "idle_seconds": state["idle_seconds"]}
            current_cooldown = max(0.0, float(control.get("cooldown_until", 0) or 0) - now())
            if current_cooldown > 0:
                extra = math.ceil(current_cooldown)
                if (control.get("reason") not in QUEUE_COOLDOWN_REASONS
                        or state["idle_seconds"] + extra > MAX_IDLE_SECONDS):
                    state.update(status="exhausted", reason="cooldown_retry_budget_exhausted")
                    _atomic_state(state_path, state)
                    return {"status": "exhausted", "runs": runs, "retries": state["retries"],
                            "idle_seconds": state["idle_seconds"]}
                state["idle_seconds"] += extra
                state["retry_at"] = max(float(state.get("retry_at", 0) or 0), float(control["cooldown_until"]))
                state["status"] = "cooldown"
                _atomic_state(state_path, state)
                continue
            state.update(status="ready", retry_at=0)
            _atomic_state(state_path, state)
            resume = state.get("resume_mode") == "resume-cases"
            cases_file = root / "discovery" / "cases.json"
            if resume:
                if (not state.get("cases_sha256") or not cases_file.is_file() or cases_file.is_symlink()
                        or hashlib.sha256(cases_file.read_bytes()).hexdigest() != state["cases_sha256"]):
                    state.update(status="blocked", reason="case_cache_identity_changed")
                    _atomic_state(state_path, state)
                    return {"status": "blocked", "reason": state["reason"], "runs": runs,
                            "retries": state["retries"], "idle_seconds": state["idle_seconds"]}
            if _has_unresolved_native_save(root):
                state.update(status="unknown", reason="native_save_custody_unresolved")
                _atomic_state(state_path, state)
                return {"status": "unknown", "runs": runs, "retries": state["retries"],
                        "idle_seconds": state["idle_seconds"]}
            if owned_browser:
                from scripts.dev.quality import kad_discovery
                try:
                    if month is None:
                        kad_discovery.ensure_owned_browser(root, year, category)
                    else:
                        kad_discovery.ensure_owned_browser(root, year, category, month=month)
                except (kad_discovery.BrowserNavigationError, OSError, ValueError):
                    state.update(status="blocked", reason="owned_browser_requires_inspection")
                    _atomic_state(state_path, state)
                    return {"status": "blocked", "runs": runs, "retries": state["retries"],
                            "idle_seconds": state["idle_seconds"]}
            state.update(status="unknown", resume_mode="resume-cases" if resume else "fresh")
            _atomic_state(state_path, state)
            command = _command(root, year, category, cases, acts_per_case, resume, owned_browser, month)
            try:
                result = runner(command, capture_output=True, text=True, timeout=process_timeout, check=False)
            except (OSError, subprocess.TimeoutExpired):
                state.update(status="unknown", reason="collector_process_outcome_unknown")
                _atomic_state(state_path, state)
                return {"status": "unknown", "runs": runs, "retries": state["retries"],
                        "idle_seconds": state["idle_seconds"]}
            runs += 1
            summary = _last_summary(result.stdout or "")
            queue = acquisition.load_queue(root)
            control = queue.get("source_controls", {}).get("kad-browser-pdf", {})
            if control.get("disabled"):
                state.update(status="blocked", reason="adapter_disabled")
                _atomic_state(state_path, state)
                return {"status": "blocked", "runs": runs, "retries": state["retries"],
                        "idle_seconds": state["idle_seconds"]}
            if _has_unresolved_native_save(root):
                state.update(status="unknown", reason="native_save_custody_unresolved")
                _atomic_state(state_path, state)
                return {"status": "unknown", "runs": runs, "retries": state["retries"],
                        "idle_seconds": state["idle_seconds"]}
            if result.returncode == 0:
                state.update(status="complete", retries=0, idle_seconds=0, resume_mode="fresh",
                             cases_sha256=None, reason=None)
                _atomic_state(state_path, state)
                return {"status": "complete", "runs": runs, "retries": 0, "idle_seconds": 0}

            retry_mode = None
            wait_seconds = 0.0
            reason = summary.get("reason")
            if result.returncode == 1 and summary.get("status") == "discovery-failed" and reason in NAVIGATION_REASONS:
                retry_mode, wait_seconds = "fresh", 60.0
            elif (result.returncode == 2 and summary.get("status") == "source-paused"
                  and summary.get("reason") is None
                  and not control.get("disabled") and control.get("reason") in QUEUE_COOLDOWN_REASONS):
                deadline = float(control.get("cooldown_until", 0) or 0)
                last_request = float(queue.get("last_request_at", 0) or 0)
                wait_seconds = max(0.0, deadline - now(), last_request + acquisition.MIN_SPACING_SECONDS - now())
                retry_mode = "resume-cases"
            if retry_mode is None:
                state.update(status="blocked", reason="non_retryable_collector_outcome")
                _atomic_state(state_path, state)
                return {"status": "blocked", "runs": runs, "retries": state["retries"],
                        "idle_seconds": state["idle_seconds"]}
            if retry_mode == "resume-cases":
                if not cases_file.is_file() or cases_file.is_symlink():
                    state.update(status="blocked", reason="case_cache_unavailable")
                    _atomic_state(state_path, state)
                    return {"status": "blocked", "runs": runs, "retries": state["retries"],
                            "idle_seconds": state["idle_seconds"]}
                cache_hash = hashlib.sha256(cases_file.read_bytes()).hexdigest()
                if state.get("cases_sha256") and state["cases_sha256"] != cache_hash:
                    state.update(status="blocked", reason="case_cache_identity_changed")
                    _atomic_state(state_path, state)
                    return {"status": "blocked", "runs": runs, "retries": state["retries"],
                            "idle_seconds": state["idle_seconds"]}
                state["cases_sha256"] = cache_hash
            charge = math.ceil(wait_seconds)
            if (state["retries"] >= MAX_RETRIES or wait_seconds < 0
                    or wait_seconds > MAX_IDLE_SECONDS
                    or state["idle_seconds"] + charge > MAX_IDLE_SECONDS):
                state.update(status="exhausted", reason="cooldown_retry_budget_exhausted")
                _atomic_state(state_path, state)
                return {"status": "exhausted", "runs": runs, "retries": state["retries"],
                        "idle_seconds": state["idle_seconds"]}
            state["retries"] += 1
            state["idle_seconds"] += charge
            state.update(status="cooldown", resume_mode=retry_mode, reason=reason, retry_at=now() + wait_seconds)
            _atomic_state(state_path, state)
            continue


def main(argv: list[str] | None = None) -> int:
    class SafeParser(argparse.ArgumentParser):
        def error(self, _message):
            self.exit(2, "Invalid coordinator arguments.\n")

    parser = SafeParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--month", type=int, choices=range(1, 13))
    parser.add_argument("--category", choices=("bankruptcy", "civil"), default="bankruptcy")
    parser.add_argument("--cases", type=int, default=25)
    parser.add_argument("--acts-per-case", type=int, default=1)
    parser.add_argument("--owned-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = coordinate(args.root, year=args.year, category=args.category,
                            cases=args.cases, acts_per_case=args.acts_per_case, owned_browser=args.owned_browser, month=args.month)
    except (acquisition.QueueError, OSError, ValueError):
        print(json.dumps({"status": "coordinator-failed"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
