#!/usr/bin/env python3
"""Exercise manual debtor-card confirmation and internal assignment in Chromium.

Run only against a fresh loopback benchmark database with the empty private
SRO_ROBOT_ENV_FILE required by ``bulk_benchmark.require_safe_runtime``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal
from app.db.models import AssignmentDraft, DebtorCardDraft, IntakeOperation, Task
from scripts.dev.bulk_benchmark import make_signed_cookie, require_empty_queue, require_safe_runtime


def assert_empty_benchmark() -> None:
    with SessionLocal() as db:
        counts = {
            model.__name__: int(db.scalar(select(func.count()).select_from(model)) or 0)
            for model in (Task, IntakeOperation, DebtorCardDraft, AssignmentDraft)
        }
    if any(counts.values()):
        raise SystemExit("Manual-card browser check requires an empty benchmark database")


def allow_loopback(route) -> None:
    target = urlsplit(route.request.url)
    if target.scheme not in {"http", "https"} or target.hostname not in {
        "127.0.0.1", "localhost", "::1",
    }:
        route.abort("blockedbyclient")
        return
    route.continue_()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18082")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    require_safe_runtime(args.base_url)
    require_empty_queue()
    assert_empty_benchmark()

    cookie, _user_id = make_signed_cookie()
    base = args.base_url.rstrip("/")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", allow_loopback)
        context.add_cookies([{"name": "session", "value": cookie, "url": base}])
        page = context.new_page()
        page.goto(base + "/debtors/new", wait_until="networkidle")
        if page.get_by_role("heading", name="Добавить должника").count() != 1:
            raise AssertionError("Manual debtor-card form did not open")

        # The browser's required-field constraint must stop an empty confirmation
        # before it creates a draft or sends a request.
        page.get_by_role("button", name="Подтвердить данные").click()
        if not page.url.endswith("/debtors/new"):
            raise AssertionError("An incomplete confirmation left the new-card form")
        for name in ("last_name", "first_name"):
            if not page.locator(f'input[name="{name}"]').evaluate("element => element.validity.valueMissing"):
                raise AssertionError("An empty required person field was not blocked by the browser")

        page.locator('input[name="last_name"]').fill("Синтетическая")
        page.locator('input[name="first_name"]').fill("Проверка")
        page.get_by_role("button", name="Подтвердить данные").click()
        page.wait_for_url("**/debtors/*?saved=1")
        debtor_card_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
        card_text = page.locator("body").inner_text()
        if "Синтетическая Проверка" not in page.locator("h1").inner_text():
            raise AssertionError("Confirmed manual card did not preserve the employee's values")
        if "Данные подтверждены сотрудником" not in card_text:
            raise AssertionError("Manual confirmation did not create a reviewed internal card")

        create_assignment = page.get_by_role(
            "button", name="Создать внутренний черновик назначения"
        )
        if create_assignment.count() != 1:
            raise AssertionError("Reviewed manual card did not offer internal assignment creation")
        create_assignment.click()
        page.wait_for_url("**/assignments/*")
        assignment_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
        assignment_text = page.locator("body").inner_text()
        if "Черновик" not in assignment_text or "СРОАрбитр: не связано" not in assignment_text:
            raise AssertionError("The assignment was not clearly shown as an internal draft")
        if "Синтетическая Проверка" not in assignment_text:
            raise AssertionError("The assignment lost its debtor-card relationship")

        with SessionLocal() as db:
            cards = int(db.scalar(select(func.count()).select_from(DebtorCardDraft)) or 0)
            drafts = int(db.scalar(select(func.count()).select_from(AssignmentDraft)) or 0)
            assignment = db.get(AssignmentDraft, assignment_id)
            if (
                cards != 1 or drafts != 1 or assignment is None
                or assignment.debtor_card_id != debtor_card_id
                or assignment.case_number != ""
            ):
                raise AssertionError("Manual workflow did not persist one linked draft without an invented case number")

        if args.screenshot:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(args.screenshot), full_page=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result = {
            "synthetic_only": True,
            "browser_blocked_incomplete_confirmation": True,
            "manual_card_confirmed": True,
            "linked_assignment_created": True,
            "assignment_remains_internal_draft": True,
            "empty_case_number_not_invented": True,
            "external_sro_accessed": False,
            "card_count": cards,
            "assignment_count": drafts,
        }
        args.output.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
        browser.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
