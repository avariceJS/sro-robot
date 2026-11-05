#!/usr/bin/env python3
"""Prove a manual internal card and assignment survive an app-process restart.

This browser check only starts and stops its own loopback Uvicorn child. It
requires an empty benchmark database/queue and the private empty SRO env file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal
from app.db.models import AssignmentDraft, DebtorCardDraft
from scripts.dev.bulk_benchmark import make_signed_cookie, require_empty_queue, require_safe_runtime
from scripts.dev.manual_card_assignment_browser_e2e import allow_loopback, assert_empty_benchmark


def start_app(base: str, log_path: Path) -> subprocess.Popen:
    target = urlsplit(base)
    with socket.socket() as probe:
        probe.settimeout(0.2)
        if probe.connect_ex((target.hostname or "", target.port or 0)) == 0:
            raise SystemExit("Refusing to restart an app that this check did not start")
    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(target.port)],
            cwd=ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Owned Uvicorn process exited with code {process.returncode}")
        try:
            with urlopen(base + "/readyz", timeout=1) as response:
                if response.status == 200:
                    return process
        except (OSError, URLError):
            time.sleep(0.25)
    stop_app(process)
    raise TimeoutError("Owned Uvicorn process did not become ready")


def stop_app(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def create_internal_work(base: str, cookie: str) -> tuple[int, int, str, str]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", allow_loopback)
        context.add_cookies([{"name": "session", "value": cookie, "url": base}])
        page = context.new_page()
        page.goto(base + "/debtors/new", wait_until="networkidle")
        card_request_key = page.locator('input[name="request_key"]').input_value()
        page.locator('input[name="last_name"]').fill("Синтетическая")
        page.locator('input[name="first_name"]').fill("Перезапуск")
        page.get_by_role("button", name="Подтвердить данные").click()
        page.wait_for_url("**/debtors/*?saved=1")
        card_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
        page.goto(base + "/assignments/new", wait_until="networkidle")
        assignment_form = page.locator(
            f'form[method="post"][action$="/assignments/new"]:has(input[name="debtor_id"][value="{card_id}"])'
        )
        if assignment_form.count() != 1:
            raise AssertionError("Reviewed manual card was absent from assignment selection")
        assignment_request_key = assignment_form.locator('input[name="request_key"]').input_value()
        assignment_form.get_by_role("button", name="Импортировать данные и создать").click()
        page.wait_for_url("**/assignments/*?created=1")
        assignment_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
        browser.close()
        return card_id, assignment_id, card_request_key, assignment_request_key


def replay_requests_after_restart(
    base: str,
    cookie: str,
    card_id: int,
    assignment_id: int,
    card_request_key: str,
    assignment_request_key: str,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", allow_loopback)
        context.add_cookies([{"name": "session", "value": cookie, "url": base}])
        page = context.new_page()
        page.goto(base + "/debtors/new", wait_until="networkidle")
        page.locator('input[name="request_key"]').evaluate(
            "(element, value) => { element.value = value; }", card_request_key,
        )
        page.locator('input[name="last_name"]').fill("Синтетическая")
        page.locator('input[name="first_name"]').fill("Перезапуск")
        page.get_by_role("button", name="Подтвердить данные").click()
        page.wait_for_url(f"**/debtors/{card_id}?saved=1")
        if int(urlsplit(page.url).path.rsplit("/", 1)[1]) != card_id:
            raise AssertionError("Replayed manual-card request returned a different card")

        page.goto(base + "/assignments/new", wait_until="networkidle")
        assignment_form = page.locator(
            f'form[method="post"][action$="/assignments/new"]:has(input[name="debtor_id"][value="{card_id}"])'
        )
        if assignment_form.count() != 1:
            raise AssertionError("Replayed manual card was absent from assignment selection")
        assignment_form.locator('input[name="request_key"]').evaluate(
            "(element, value) => { element.value = value; }", assignment_request_key,
        )
        assignment_form.get_by_role("button", name="Импортировать данные и создать").click()
        page.wait_for_url(f"**/assignments/{assignment_id}?created=1")
        if int(urlsplit(page.url).path.rsplit("/", 1)[1]) != assignment_id:
            raise AssertionError("Replayed assignment request returned a different draft")
        browser.close()


def verify_resumed_work(base: str, cookie: str, card_id: int, assignment_id: int) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", allow_loopback)
        context.add_cookies([{"name": "session", "value": cookie, "url": base}])
        page = context.new_page()
        page.goto(f"{base}/assignments/{assignment_id}", wait_until="networkidle")
        body = page.locator("body").inner_text()
        if (
            "Синтетическая Перезапуск" not in body
            or "Черновик" not in body
            or "СРОАрбитр: не связано" not in body
        ):
            raise AssertionError("Assignment did not resume as the same internal draft after restart")
        page.goto(f"{base}/debtors/{card_id}", wait_until="networkidle")
        if "Синтетическая Перезапуск" not in page.locator("h1").inner_text():
            raise AssertionError("Confirmed debtor card did not resume after app restart")
        if page.locator(".panel").first.locator(".section-heading .badge").inner_text() != "Черновик":
            raise AssertionError("Resumed person card did not show its current internal-work state")
        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18087")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_safe_runtime(args.base_url)
    require_empty_queue()
    assert_empty_benchmark()

    base = args.base_url.rstrip("/")
    cookie, _user_id = make_signed_cookie()
    runtime_dir = Path(tempfile.mkdtemp(prefix="sro-restart-browser-e2e-"))
    temp_log = runtime_dir / "uvicorn.log"
    first_process = second_process = None
    succeeded = False
    try:
        first_process = start_app(base, temp_log)
        card_id, assignment_id, card_request_key, assignment_request_key = create_internal_work(base, cookie)
        stop_app(first_process)
        first_process = None
        second_process = start_app(base, temp_log)
        replay_requests_after_restart(
            base, cookie, card_id, assignment_id,
            card_request_key, assignment_request_key,
        )
        verify_resumed_work(base, cookie, card_id, assignment_id)
        with SessionLocal() as db:
            cards = int(db.scalar(select(func.count()).select_from(DebtorCardDraft)) or 0)
            assignments = int(db.scalar(select(func.count()).select_from(AssignmentDraft)) or 0)
            resumed_card = db.get(DebtorCardDraft, card_id)
            resumed_assignment = db.get(AssignmentDraft, assignment_id)
            if (
                cards != 1 or assignments != 1 or resumed_card is None or resumed_assignment is None
                or resumed_card.entity_kind != "person"
                or resumed_card.preparation_state != "reviewed"
                or resumed_card.source_task_id is not None
                or resumed_assignment.debtor_card_id != card_id
                or resumed_assignment.status != "draft"
            ):
                raise AssertionError("Post-restart database read-back changed card or assignment identity/state")
        succeeded = True
    finally:
        if second_process is not None:
            stop_app(second_process)
        if first_process is not None:
            stop_app(first_process)
        if succeeded:
            shutil.rmtree(runtime_dir)
        else:
            print(f"Preserved failed-run Uvicorn log at {temp_log}", file=sys.stderr)

    result = {
        "synthetic_only": True,
        "internal_card_resumed_after_app_restart": True,
        "linked_assignment_resumed_after_app_restart": True,
        "manual_card_replay_after_restart_reused_same_card": True,
        "assignment_replay_after_restart_reused_same_draft": True,
        "assignment_remains_internal_draft": True,
        "database_identity_and_state_read_back": True,
        "external_sro_accessed": False,
        "card_count": cards,
        "assignment_count": assignments,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
