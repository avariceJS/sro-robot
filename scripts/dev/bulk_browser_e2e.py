#!/usr/bin/env python3
"""Run a local synthetic Playwright upload, review, card, and replay journey.

Requires the same explicit empty SRO_ROBOT_ENV_FILE and loopback-only
benchmark settings as bulk_benchmark.py. It uploads only generated PDFs and
uses the normal persisted app session/cookie helpers; it never visits SRO.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal
from app.db.models import DebtorCardDraft, DebtorCardDraftRevision, IntakeOperation, Task
from scripts.dev.bulk_benchmark import make_signed_cookie, require_safe_runtime, select_mixed


def counts() -> tuple[int, int, int]:
    with SessionLocal() as db:
        return tuple(
            int(db.scalar(select(func.count()).select_from(model)) or 0)
            for model in (Task, IntakeOperation, DebtorCardDraft)
        )


def wait_batch(operation_ids: list[int], expected_count: int, timeout: float = 1200) -> list[Task]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            rows = db.scalars(
                select(Task)
                .options(joinedload(Task.document))
                .where(Task.intake_operation_id.in_(operation_ids))
                .order_by(Task.id)
            ).all()
            if len(rows) == expected_count and all(row.status.value in {"needs_review", "failed"} for row in rows):
                return rows
        time.sleep(0.25)
    raise TimeoutError("Synthetic browser batch did not reach terminal OCR states")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18082")
    parser.add_argument("--count", type=int, default=3, help="valid corpus PDFs; one corrupt PDF is added")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshot-dir", type=Path, required=True)
    args = parser.parse_args()
    require_safe_runtime(args.base_url)
    manifest = json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))
    docs = manifest["documents"]
    if args.count not in {3, 21, 50}:
        raise SystemExit("Browser E2E supports 3, 21, or 50 valid PDF inputs")
    selected = (
        docs[4:6] + [next(item for item in docs if item["kind"] == "scan_low")]
        if args.count == 3
        else select_mixed(docs, args.count)
    )
    if len(selected) != args.count or len({item["sha256"] for item in selected}) != args.count:
        raise SystemExit("Expected unique synthetic browser inputs")
    before = counts()
    if before != (0, 0, 0):
        raise SystemExit("Browser E2E requires a fresh synthetic benchmark database")
    args.screenshot_dir.mkdir(parents=True, exist_ok=True)
    corrupt_path = args.screenshot_dir / "synthetic-corrupt.pdf"
    corrupt_path.write_bytes(b"%PDF-1.4\nsynthetic truncated cross-reference\n%%EOF\n")
    cookie, _user_id = make_signed_cookie()
    base = args.base_url.rstrip("/")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        context.add_cookies([{"name": "session", "value": cookie, "url": base}])
        page = context.new_page()
        page.goto(base + "/", wait_until="networkidle")
        page.locator('input[name="files"]').set_input_files(
            [str(args.corpus / item["filename"]) for item in selected] + [str(corrupt_path)]
        )
        page.get_by_role("button", name="Загрузить").click()
        page.wait_for_url("**/?intake=*", timeout=30000)
        operation_ids = [
            int(item) for item in parse_qs(urlsplit(page.url).query)["intake"][0].split(",")
        ]
        rows = wait_batch(operation_ids, args.count + 1)
        if sum(row.status.value == "needs_review" for row in rows) != args.count or sum(
            row.status.value == "failed" for row in rows
        ) != 1:
            raise AssertionError(f"Expected {args.count} valid review tasks and one isolated corrupt-file failure")
        manual_source = selected[-1]
        low_scan_task_id = next(row.id for row in rows if row.document.filename == manual_source["filename"])
        intake_value = ",".join(str(item) for item in operation_ids)
        page.goto(f"{base}/?intake={intake_value}", wait_until="networkidle")
        dashboard_first_page_rows = page.locator("tbody tr").count()
        next_page_link = page.locator("a").filter(has_text="Дальше").first
        dashboard_second_page_rows = 0
        dashboard_page_size = 50  # app.services.bulk.PAGE_SIZE at the measured source revision
        if args.count + 1 > dashboard_page_size:
            if dashboard_first_page_rows != dashboard_page_size or next_page_link.count() != 1:
                raise AssertionError("Bulk dashboard did not expose its first paginated page")
            next_page_link.click()
            page.wait_for_load_state("networkidle")
            dashboard_second_page_rows = page.locator("tbody tr").count()
            if dashboard_second_page_rows != args.count + 1 - dashboard_page_size:
                raise AssertionError("Dashboard page 2 row count differs from remaining scoped synthetic tasks")
        elif dashboard_first_page_rows != args.count + 1 or next_page_link.count():
            raise AssertionError("Small synthetic dashboard row count or pagination differs")
        page.goto(f"{base}/?intake={intake_value}", wait_until="networkidle")
        page.locator("a").filter(has_text="Карточка есть").first.click()
        page.wait_for_load_state("networkidle")
        with SessionLocal() as db:
            automatic_card_count = int(db.scalar(select(func.count()).select_from(DebtorCardDraft)) or 0)
        uploaded_counts = counts()
        if uploaded_counts != (args.count + 1, len(operation_ids), automatic_card_count):
            raise AssertionError(
                f"Expected bulk upload counts ({args.count + 1}, {len(operation_ids)}, "
                f"{automatic_card_count}), got {uploaded_counts}"
            )
        if page.locator("tbody tr").count() != automatic_card_count or automatic_card_count < 2:
            raise AssertionError("Prepared-card filter row count differs from created synthetic cards")
        page.goto(f"{base}/?intake={intake_value}&status=prepared", wait_until="networkidle")
        args.screenshot_dir.joinpath("dashboard.png").parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(args.screenshot_dir / "dashboard.png"), full_page=True)

        target_row = page.locator("tbody tr").first
        source_filename = target_row.locator("td").first.inner_text().strip()
        source_item = next(item for item in selected if item["filename"] == source_filename)
        target_row.locator("a").filter(has_text="Проверить").first.click()
        page.wait_for_url("**/tasks/*")
        with page.expect_download() as download_info:
            page.get_by_role("link", name="Скачать исходный документ").click()
        download_path = args.screenshot_dir / "source-download.pdf"
        download_info.value.save_as(str(download_path))
        downloaded_hash = hashlib.sha256(download_path.read_bytes()).hexdigest()
        if downloaded_hash != source_item["sha256"]:
            raise AssertionError("Downloaded synthetic source hash differs from uploaded bytes")

        page.goto(f"{base}/?intake={intake_value}&status=prepared", wait_until="networkidle")
        card_link = page.locator("tbody tr").first.locator("a").filter(has_text="Внутренняя карточка").first
        card_link.click()
        page.wait_for_url("**/debtors/*")
        page.screenshot(path=str(args.screenshot_dir / "prepared-card.png"), full_page=True)
        prepared_card_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
        card_form = page.locator("form").nth(1)
        empty_optional_before = {
            name: card_form.locator(f'[name="{name}"]').input_value()
            for name in ("snils", "court")
        }
        if any(value != "" for value in empty_optional_before.values()):
            raise AssertionError(f"Empty optional fields render non-empty before edit: {empty_optional_before}")
        card_form.locator('[name="address"]').fill(
            "101000, вымышленный город Макетный, улица Учебная, дом 1, квартира 99"
        )
        card_form.get_by_role("button", name="Проверить и сохранить карточку").click()
        empty_optional_after = {
            name: page.locator("form").nth(1).locator(f'[name="{name}"]').input_value()
            for name in ("snils", "court")
        }
        if any(value != "" for value in empty_optional_after.values()):
            raise AssertionError(f"Empty optional fields changed after card edit: {empty_optional_after}")
        with SessionLocal() as db:
            card = db.get(DebtorCardDraft, prepared_card_id)
            revisions = db.scalars(
                select(DebtorCardDraftRevision).where(
                    DebtorCardDraftRevision.debtor_card_id == prepared_card_id
                )
            ).all()
            if card is None or card.preparation_state != "reviewed" or card.version != 2 or len(revisions) != 2:
                raise AssertionError("Prepared card edit did not create the expected reviewed revision")

        replay_page = context.new_page()
        replay_page.goto(base + "/", wait_until="networkidle")
        replay_page.locator('input[name="files"]').set_input_files(
            [str(args.corpus / item["filename"]) for item in selected] + [str(corrupt_path)]
        )
        replay_page.get_by_role("button", name="Загрузить").click()
        replay_page.wait_for_url("**/?intake=*", timeout=30000)
        replay_operation_ids = [
            int(item) for item in parse_qs(urlsplit(replay_page.url).query)["intake"][0].split(",")
        ]
        if replay_operation_ids != operation_ids:
            raise AssertionError(f"Replay changed operation identities: {operation_ids} -> {replay_operation_ids}")
        replay_counts = counts()
        if replay_counts != (args.count + 1, len(operation_ids), automatic_card_count):
            raise AssertionError(
                f"Exact replay created new rows: expected ({args.count + 1}, {len(operation_ids)}, "
                f"{automatic_card_count}), got {replay_counts}"
            )

        manual_row = replay_page.locator("tbody tr").filter(has_text=manual_source["filename"])
        if not manual_row.count():
            replay_page.locator("a").filter(has_text="Дальше").first.click()
            replay_page.wait_for_load_state("networkidle")
            manual_row = replay_page.locator("tbody tr").filter(has_text=manual_source["filename"])
        if manual_row.count() != 1:
            raise AssertionError("Pagination did not expose the selected ambiguous synthetic task")
        manual_row.locator("a").filter(has_text="Проверить").first.click()
        replay_page.wait_for_url("**/tasks/*")
        manual_button = replay_page.get_by_role(
            "button", name="Подтвердить и подготовить внутреннюю карточку"
        )
        if manual_button.count() != 1:
            raise AssertionError("Ambiguous scan review did not expose one-action card approval")
        expected = manual_source
        for name, value in (
            ("fio_raw", expected["fio"]),
            ("inn", expected["inn"]),
            ("case_number", expected["case_number"]),
            ("doc_date", expected["doc_date"]),
            ("address", "101000, вымышленный город Макетный, улица Учебная, дом 1"),
        ):
            field = replay_page.locator(f'#review-form [name="{name}"]')
            if field.count():
                field.fill(value)
        manual_button.click()
        try:
            replay_page.wait_for_url("**/debtors/*?created=1", timeout=10000)
        except PlaywrightTimeoutError as exc:
            raise AssertionError("Manual one-action review did not prepare an internal card") from exc
        with SessionLocal() as db:
            manual_card = db.scalar(
                select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == low_scan_task_id)
            )
            if manual_card is None:
                raise AssertionError("Manual review did not create the synthetic internal card")
            manual_state = manual_card.preparation_state
        browser.close()

    result = {
        "synthetic_only": True,
        "base_url": base,
        "valid_document_count": args.count,
        "initial_tasks": before[0] + args.count + 1,
        "initial_intake_operations": before[1] + len(operation_ids),
        "operation_ids": operation_ids,
        "task_count_after_upload": uploaded_counts[0],
        "task_statuses": {
            status: sum(row.status.value == status for row in rows)
            for status in ("needs_review", "failed")
        },
        "valid_tasks_continue_after_corrupt_input": sum(row.status.value == "needs_review" for row in rows) == args.count,
        "corrupt_file_isolated_failure": sum(row.status.value == "failed" for row in rows) == 1,
        "dashboard_prepared_filter_rows": automatic_card_count,
        "dashboard_page_size": dashboard_page_size,
        "dashboard_first_page_rows": dashboard_first_page_rows,
        "dashboard_second_page_rows": dashboard_second_page_rows,
        "source_download_filename": source_filename,
        "download_sha256_matches_corpus": downloaded_hash == source_item["sha256"],
        "edited_card_state": "reviewed",
        "edited_card_revision_count": 2,
        "empty_optional_fields_before_edit": empty_optional_before,
        "empty_optional_fields_after_edit": empty_optional_after,
        "exact_replay_counts": list(replay_counts),
        "ambiguous_manual_card_state": manual_state,
        "screenshots": ["dashboard.png", "prepared-card.png"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
