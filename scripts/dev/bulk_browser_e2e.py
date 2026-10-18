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
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal
from app.db.models import (
    AssignmentDraft,
    AssignmentDraftRevision,
    DebtorCardDraft,
    DebtorCardDraftRevision,
    IntakeOperation,
    Task,
)
from scripts.dev.bulk_benchmark import make_signed_cookie, require_safe_runtime, select_mixed
from scripts.dev.quality.entity_scan_smoke import CASES as ENTITY_SCAN_CASES, font_path, write_scanned_pdf


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
    parser.add_argument("--entity-flow", action="store_true",
                        help="also upload synthetic company/IP scans through review and internal assignment")
    parser.add_argument("--repeat-same-bytes", action="store_true",
                        help="verify explicit reprocessing creates a separate task but refuses a duplicate person card")
    parser.add_argument("--headed", action="store_true",
                        help="run Chromium with its PDF viewer enabled for visual preview checks")
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
        browser = playwright.chromium.launch(headless=not args.headed)
        context = browser.new_context(accept_downloads=True)
        context.add_cookies([{"name": "session", "value": cookie, "url": base}])
        page = context.new_page()
        page.goto(base + "/debtors/intake", wait_until="networkidle")
        page.locator('input[name="files"]').set_input_files(
            [str(args.corpus / item["filename"]) for item in selected] + [str(corrupt_path)]
        )
        page.get_by_role("button", name="Загрузить").click()
        page.wait_for_url("**/debtors/intake?intake=*", timeout=30000)
        operation_ids = [
            int(item) for item in parse_qs(urlsplit(page.url).query)["intake"][0].split(",")
        ]
        rows = wait_batch(operation_ids, args.count + 1)
        if sum(row.status.value == "needs_review" for row in rows) != args.count or sum(
            row.status.value == "failed" for row in rows
        ) != 1:
            raise AssertionError(f"Expected {args.count} valid review tasks and one isolated corrupt-file failure")
        failed_task_id = next(row.id for row in rows if row.status.value == "failed")
        page.goto(f"{base}/tasks/{failed_task_id}", wait_until="networkidle")
        recovery_link = page.get_by_role("link", name="Открыть новую загрузку")
        recovery_href = recovery_link.get_attribute("href") or ""
        if not recovery_href.endswith("/debtors/intake#document-upload"):
            raise AssertionError("Failed OCR page did not link to the supported document retry flow")
        failure_guidance = page.locator("body").inner_text()
        if "Намеренно запустить новую обработку этих файлов" not in failure_guidance:
            raise AssertionError("Failed OCR page omitted the explicit new-run instruction")
        manual_source = selected[-1]
        low_scan_task_id = next(row.id for row in rows if row.document.filename == manual_source["filename"])
        intake_value = ",".join(str(item) for item in operation_ids)
        page.goto(f"{base}/debtors/intake?intake={intake_value}", wait_until="networkidle")
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
        page.goto(f"{base}/debtors/intake?intake={intake_value}", wait_until="networkidle")
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
        page.goto(f"{base}/debtors/intake?intake={intake_value}&status=prepared", wait_until="networkidle")
        metric_rects = page.locator(".bulk-counts > div").evaluate_all(
            "items => items.map(item => { const rect = item.getBoundingClientRect(); "
            "return {x: rect.x, y: rect.y}; })"
        )
        metric_rows = {}
        for rect in metric_rects:
            metric_rows.setdefault(round(rect["y"]), 0)
            metric_rows[round(rect["y"])] += 1
        metric_columns = max(metric_rows.values(), default=0)
        if len(metric_rects) != 9 or metric_columns < 3 or len(metric_rows) > 4:
            raise AssertionError(
                "Bulk worklist counters did not form a compact grid: "
                f"tiles={len(metric_rects)}, rows={len(metric_rows)}, "
                f"widest_row={metric_columns}"
            )
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

        page.goto(f"{base}/debtors/intake?intake={intake_value}&status=prepared", wait_until="networkidle")
        card_link = page.locator("tbody tr").first.locator("a").filter(has_text="Внутренняя карточка").first
        card_link.click()
        page.wait_for_url("**/debtors/*")
        card_preview = page.locator("iframe.document-preview")
        if card_preview.count() != 1:
            raise AssertionError("Reviewed debtor card did not embed its source PDF")
        card_preview.scroll_into_view_if_needed()
        if args.headed:
            page.wait_for_timeout(1500)
        page.screenshot(path=str(args.screenshot_dir / "prepared-card.png"), full_page=True)
        prepared_card_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
        card_form = page.locator("#debtor-edit-form")
        empty_optional_before = {
            name: card_form.locator(f'[name="{name}"]').input_value()
            for name in ("snils", "court")
        }
        if any(value != "" for value in empty_optional_before.values()):
            raise AssertionError(f"Empty optional fields render non-empty before edit: {empty_optional_before}")
        card_form.locator('[name="address"]').fill(
            "101000, вымышленный город Макетный, улица Учебная, дом 1, квартира 99"
        )
        card_form.get_by_role("button", name="Подтвердить данные").click()
        empty_optional_after = {
            name: page.locator("#debtor-edit-form").locator(f'[name="{name}"]').input_value()
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
        replay_page.goto(base + "/debtors/intake", wait_until="networkidle")
        replay_page.locator('input[name="files"]').set_input_files(
            [str(args.corpus / item["filename"]) for item in selected] + [str(corrupt_path)]
        )
        replay_page.get_by_role("button", name="Загрузить").click()
        replay_page.wait_for_url("**/debtors/intake?intake=*", timeout=30000)
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

        repeated_source_task_id = None
        repeated_source_card_id = None
        repeat_result = {"ran": False}
        if args.repeat_same_bytes:
            cards_before_repeat = counts()[2]
            with SessionLocal() as db:
                candidate = db.scalar(
                    select(Task)
                    .join(DebtorCardDraft, DebtorCardDraft.source_task_id == Task.id)
                    .where(Task.intake_operation_id.in_(operation_ids), Task.scenario == "person_card")
                    .order_by(Task.id)
                )
                if candidate is None:
                    raise AssertionError("Synthetic corpus has no automatically prepared person card to repeat")
                repeated_source_task_id = candidate.id
                repeated_source_card_id = db.scalar(
                    select(DebtorCardDraft.id).where(DebtorCardDraft.source_task_id == candidate.id)
                )
                repeated_source_filename = candidate.document.filename
                repeated_source_item = next(
                    item for item in selected if item["filename"] == repeated_source_filename
                )
            repeat_page = context.new_page()
            repeat_page.goto(base + "/debtors/intake", wait_until="networkidle")
            repeat_page.locator('input[name="files"]').set_input_files(
                str(args.corpus / repeated_source_item["filename"])
            )
            repeat_page.locator('input[name="new_processing_run"]').check()
            repeat_page.get_by_role("button", name="Загрузить").click()
            repeat_page.wait_for_url("**/debtors/intake?intake=*", timeout=30000)
            repeated_operation_ids = [
                int(item) for item in parse_qs(urlsplit(repeat_page.url).query)["intake"][0].split(",")
            ]
            if len(repeated_operation_ids) != 1 or repeated_operation_ids[0] in operation_ids:
                raise AssertionError("Intentional same-byte reprocessing reused the prior intake operation")
            repeated_rows = wait_batch(repeated_operation_ids, 1)
            if repeated_rows[0].status.value != "needs_review":
                raise AssertionError("Repeated synthetic document did not return to its review state")
            with SessionLocal() as db:
                original = db.get(Task, repeated_source_task_id)
                repeated = db.get(Task, repeated_rows[0].id)
                card = db.get(DebtorCardDraft, repeated_source_card_id)
                new_card = db.scalar(
                    select(DebtorCardDraft).where(DebtorCardDraft.source_task_id == repeated.id)
                )
                all_cards = int(db.scalar(select(func.count()).select_from(DebtorCardDraft)) or 0)
                if (
                    original is None or repeated is None or card is None
                    or original.id == repeated.id
                    or original.intake_operation_id == repeated.intake_operation_id
                    or original.intake_item_id == repeated.intake_item_id
                    or original.document_id != repeated.document_id
                    or new_card is not None
                    or all_cards != cards_before_repeat
                    or repeated.recognized.get("_internal_preparation_v1", {}).get("reason") != "possible_duplicate"
                    or repeated.recognized.get("_internal_preparation_v1", {}).get("possible_card_id") != card.id
                ):
                    raise AssertionError("Repeated occurrence did not preserve provenance and block a duplicate person card")
            repeat_result = {
                "ran": True,
                "new_operation_id": repeated_operation_ids[0],
                "new_task_id": repeated_rows[0].id,
                "same_document_id": repeated_rows[0].document_id,
                "distinct_intake_item": True,
                "duplicate_person_card_refused_with_existing_card_reference": True,
                "card_count_unchanged": True,
            }
        manual_row = replay_page.locator("tbody tr").filter(has_text=manual_source["filename"])
        if not manual_row.count():
            replay_page.locator("a").filter(has_text="Дальше").first.click()
            replay_page.wait_for_load_state("networkidle")
            manual_row = replay_page.locator("tbody tr").filter(has_text=manual_source["filename"])
        if manual_row.count() != 1:
            raise AssertionError("Pagination did not expose the selected ambiguous synthetic task")
        with replay_page.expect_response(
            lambda response: response.url.endswith(
                f"/tasks/{low_scan_task_id}/document/preview"
            )
        ) as preview_response_info:
            manual_row.locator("a").filter(has_text="Проверить").first.click()
        replay_page.wait_for_url("**/tasks/*")
        preview_response = preview_response_info.value
        preview_headers = preview_response.all_headers()
        # Chromium's PDF plugin consumes and navigates the iframe response, so
        # Playwright may discard its body before the parent-page action returns.
        # Read the same authenticated, read-only endpoint through the context
        # request client for a stable byte-level assertion.
        preview_body_response = replay_page.request.get(preview_response.url)
        preview_body = preview_body_response.body()
        if (
            preview_response.status != 200
            or preview_body_response.status != 200
            or preview_headers.get("content-type", "").split(";", 1)[0] != "application/pdf"
            or not preview_body.startswith(b"%PDF-")
            or preview_headers.get("x-content-type-options") != "nosniff"
            or preview_headers.get("x-frame-options") != "SAMEORIGIN"
            or preview_headers.get("content-security-policy")
            != "default-src 'none'; sandbox; frame-ancestors 'self'"
            or preview_headers.get("cache-control") != "no-store"
        ):
            raise AssertionError(
                "Source preview did not return the verified, private, sandboxed PDF response: "
                f"iframe_status={preview_response.status}, api_status={preview_body_response.status}, "
                f"content_type={preview_headers.get('content-type')!r}, "
                f"signature={preview_body[:5]!r}, "
                f"nosniff={preview_headers.get('x-content-type-options')!r}, "
                f"csp={preview_headers.get('content-security-policy')!r}, "
                f"cache={preview_headers.get('cache-control')!r}"
            )
        manual_button = replay_page.get_by_role(
            "button", name="Подтвердить и подготовить внутреннюю карточку"
        )
        if manual_button.count() != 1:
            raise AssertionError("Ambiguous scan review did not expose one-action card approval")
        expected = manual_source
        date_input = replay_page.locator('#review-form [name="doc_date"]')
        initial_date = date_input.input_value()
        review_text = replay_page.locator("body").inner_text()
        if initial_date != expected["doc_date"]:
            raise AssertionError("Low-resolution date cross-check did not reach the employee review form")
        if "Распознавание даты расходится при другом масштабе" not in review_text:
            raise AssertionError("Low-resolution alternate date was not marked for employee review")
        if args.headed:
            # Allow Chromium's built-in PDF plugin to paint after the iframe
            # response completes; it renders asynchronously from DOM readiness.
            replay_page.wait_for_timeout(1500)
        replay_page.screenshot(path=str(args.screenshot_dir / "low-resolution-date-review.png"), full_page=True)
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
        replay_page.get_by_role("button", name="Сохранить", exact=True).click()
        replay_page.wait_for_url(f"**/tasks/{low_scan_task_id}")
        replay_page.goto(f"{base}/tasks/{low_scan_task_id}", wait_until="networkidle")
        persisted_date = replay_page.locator('#review-form [name="doc_date"]').input_value()
        if persisted_date != expected["doc_date"]:
            raise AssertionError("Employee-saved alternate date did not persist after navigation")
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
        entity_flow_result = {"ran": False}
        if args.entity_flow:
            with TemporaryDirectory(prefix="sro-entity-browser-") as entity_dir_name:
                entity_dir = Path(entity_dir_name)
                entity_font = font_path()
                entity_paths = []
                duplicate_company_case = dict(
                    ENTITY_SCAN_CASES[0], name="legal_entity_duplicate",
                    expected_case="А99-54321/2099",
                    lines=tuple(
                        "Дело А99-54321/2099" if line == "Дело А99-12345/2099" else line
                        for line in ENTITY_SCAN_CASES[0]["lines"]
                    ),
                )
                entity_browser_cases = [*ENTITY_SCAN_CASES, duplicate_company_case]
                for case in entity_browser_cases:
                    pdf_path = entity_dir / f"{case['name']}.pdf"
                    write_scanned_pdf(pdf_path, case["lines"], entity_font)
                    entity_paths.append(pdf_path)
                entity_page = context.new_page()
                entity_page.goto(base + "/debtors/intake", wait_until="networkidle")
                entity_page.locator('input[name="files"]').set_input_files([str(path) for path in entity_paths])
                entity_page.get_by_role("button", name="Загрузить").click()
                entity_page.wait_for_url("**/debtors/intake?intake=*")
                entity_operation_ids = [
                    int(item) for item in parse_qs(urlsplit(entity_page.url).query)["intake"][0].split(",")
                ]
                entity_tasks = wait_batch(entity_operation_ids, len(entity_browser_cases))
                by_name = {row.document.filename: row for row in entity_tasks}
                if len(by_name) != len(entity_browser_cases) or any(
                    row.status.value != "needs_review" or row.scenario != "entity_review"
                    for row in entity_tasks
                ):
                    raise AssertionError("Mixed company/IP upload did not route both scans to entity review")

                entity_card_ids = []
                assignment_ids = []
                duplicate_refused = False
                corrected_company_saved = False
                assignment_replay_after_navigation = False
                assignment_waiting_state_saved = False
                for case in entity_browser_cases:
                    row = by_name[f"{case['name']}.pdf"]
                    entity_page.goto(f"{base}/tasks/{row.id}", wait_until="networkidle")
                    if "Источник: вариант OCR" not in entity_page.locator("body").inner_text():
                        raise AssertionError("Entity review page omitted OCR source location")
                    if entity_page.locator('#review-form [name="entity_kind"]').input_value() != case["expected_kind"]:
                        raise AssertionError("OCR debtor kind differs from synthetic scan label")
                    entity_page.locator('#review-form [name="case_number"]').fill(case["expected_case"])
                    entity_page.locator('#review-form [name="doc_date"]').fill(case["expected_date"])
                    if case["name"] == "legal_entity":
                        entity_page.locator('#review-form [name="legal_name"]').fill(
                            "ООО «Исправлено сотрудником»"
                        )
                        entity_page.get_by_role("button", name="Сохранить", exact=True).click()
                        entity_page.wait_for_url(f"**/tasks/{row.id}")
                        entity_page.goto(f"{base}/debtors/intake", wait_until="networkidle")
                        entity_page.goto(f"{base}/tasks/{row.id}", wait_until="networkidle")
                        if entity_page.locator('#review-form [name="legal_name"]').input_value() != "ООО «Исправлено сотрудником»":
                            raise AssertionError("Saved legal-entity review correction did not resume after navigation")
                        entity_page.screenshot(path=str(args.screenshot_dir / "entity-review.png"), full_page=True)
                    entity_page.get_by_role(
                        "button", name="Подтвердить и подготовить внутреннюю карточку"
                    ).click()
                    if case["name"] == "legal_entity_duplicate":
                        entity_page.wait_for_url(f"**/tasks/{row.id}?internal_card=duplicate")
                        if "возможный дубль" not in entity_page.locator("body").inner_text().casefold():
                            raise AssertionError("Duplicate company identifier was not shown for employee review")
                        duplicate_refused = True
                        continue
                    entity_page.wait_for_url("**/debtors/*?created=1")
                    card_id = int(urlsplit(entity_page.url).path.rsplit("/", 1)[1])
                    entity_card_ids.append(card_id)
                    if case["name"] == "legal_entity":
                        entity_page.screenshot(path=str(args.screenshot_dir / "entity-card.png"), full_page=True)
                    with SessionLocal() as db:
                        card = db.get(DebtorCardDraft, card_id)
                        if card is None or card.preparation_state != "reviewed" or card.entity_kind != case["expected_kind"]:
                            raise AssertionError("Employee confirmation did not create the expected reviewed internal card")
                        if card.inn != case["expected_inn"]:
                            raise AssertionError("Reviewed internal card lost its synthetic identifier")
                        if case["name"] == "legal_entity" and card.display_name != "ООО «Исправлено сотрудником»":
                            raise AssertionError("Employee correction was not applied to the internal card")
                        if case["name"] == "legal_entity":
                            corrected_company_saved = True
                    entity_page.goto(f"{base}/tasks/{row.id}", wait_until="networkidle")
                    entity_page.get_by_role(
                        "button", name="Создать отдельный внутренний черновик назначения"
                    ).click()
                    entity_page.wait_for_url("**/assignments/*?created=1")
                    assignment_id = int(urlsplit(entity_page.url).path.rsplit("/", 1)[1])
                    assignment_ids.append(assignment_id)
                    with SessionLocal() as db:
                        assignment = db.get(AssignmentDraft, assignment_id)
                        if assignment is None or assignment.debtor_card_id != card_id or assignment.status != "draft":
                            raise AssertionError("Internal assignment did not retain its chosen reviewed debtor card")
                        if assignment.case_number != case["expected_case"]:
                            raise AssertionError("Assignment did not inherit the confirmed case reference")
                        assignment_count = int(db.scalar(
                            select(func.count()).select_from(AssignmentDraft)
                        ) or 0)
                    if case["name"] == "legal_entity":
                        applicant_address = "101000, учебный адрес заявителя, дом 7"
                        assignment_form = entity_page.locator("#assignment-edit-form")
                        assignment_form.locator('[name="applicant_address"]').fill(applicant_address)
                        assignment_form.locator('[name="comments"]').fill(
                            "Ожидаем сведения об управляющем"
                        )
                        assignment_form.locator('[name="status"]').select_option(
                            "awaiting_information"
                        )
                        assignment_form.get_by_role(
                            "button", name="Сохранить назначение"
                        ).click()
                        entity_page.wait_for_url(
                            f"**/assignments/{assignment_id}?saved=1"
                        )
                        entity_page.goto(f"{base}/assignments", wait_until="networkidle")
                        assignment_link = entity_page.get_by_role(
                            "link", name="Открыть", exact=True
                        )
                        assignment_row = entity_page.locator("tbody tr").filter(
                            has=assignment_link
                        )
                        if assignment_row.count() != 1 or "Ожидает информацию" not in assignment_row.inner_text():
                            raise AssertionError(
                                "Assignment worklist did not show the saved awaiting-information state: "
                                f"matching_rows={assignment_row.count()}, "
                                f"rows={entity_page.locator('tbody tr').all_inner_texts()!r}, "
                                f"page={entity_page.locator('body').inner_text()[-800:]!r}"
                            )
                        assignment_link.click()
                        entity_page.wait_for_url(f"**/assignments/{assignment_id}")
                        resumed_form = entity_page.locator("#assignment-edit-form")
                        if (
                            resumed_form.locator('[name="applicant_address"]').input_value()
                            != applicant_address
                            or resumed_form.locator('[name="comments"]').input_value()
                            != "Ожидаем сведения об управляющем"
                            or resumed_form.locator('[name="practitioner"]').input_value()
                            != ""
                            or resumed_form.locator('[name="status"]').input_value()
                            != "awaiting_information"
                        ):
                            raise AssertionError(
                                "Assignment did not resume with its manually confirmed values and missing practitioner"
                            )
                        entity_page.screenshot(
                            path=str(args.screenshot_dir / "assignment-awaiting-information.png"),
                            full_page=True,
                        )
                        with SessionLocal() as db:
                            saved_assignment = db.get(AssignmentDraft, assignment_id)
                            latest_revision = db.scalar(
                                select(AssignmentDraftRevision)
                                .where(
                                    AssignmentDraftRevision.assignment_draft_id
                                    == assignment_id
                                )
                                .order_by(AssignmentDraftRevision.revision.desc())
                                .limit(1)
                            )
                            if (
                                saved_assignment is None
                                or saved_assignment.status != "awaiting_information"
                                or saved_assignment.values.get("applicant_address")
                                != applicant_address
                                or saved_assignment.values.get("practitioner")
                                or saved_assignment.updated_by_id != _user_id
                                or latest_revision is None
                                or latest_revision.changed_by_id != _user_id
                                or latest_revision.status != "awaiting_information"
                                or latest_revision.snapshot.get("values", {}).get(
                                    "applicant_address"
                                ) != applicant_address
                            ):
                                raise AssertionError(
                                    "Assignment read-back lost manual attribution, address, or pending practitioner state"
                                )
                        assignment_waiting_state_saved = True
                        entity_page.goto(f"{base}/tasks/{row.id}", wait_until="networkidle")
                        recovery_link = entity_page.get_by_role(
                            "link", name="Открыть назначение по этому документу", exact=True
                        )
                        if recovery_link.count() != 1:
                            raise AssertionError("Task page hid the existing assignment recovery link")
                        if not (recovery_link.get_attribute("href") or "").endswith(
                            f"/assignments/{assignment_id}"
                        ):
                            raise AssertionError("Task page assignment recovery link points to another draft")
                        recovery_link.click()
                        entity_page.wait_for_url(f"**/assignments/{assignment_id}")
                        with SessionLocal() as db:
                            replayed_assignment = db.scalar(
                                select(AssignmentDraft).where(AssignmentDraft.source_task_id == row.id)
                            )
                            current_assignment_count = int(db.scalar(
                                select(func.count()).select_from(AssignmentDraft)
                            ) or 0)
                            if (
                                replayed_assignment is None
                                or replayed_assignment.id != assignment_id
                                or replayed_assignment.debtor_card_id != card_id
                                or current_assignment_count != assignment_count
                            ):
                                raise AssertionError("Assignment recovery navigation changed or duplicated the draft")
                        assignment_replay_after_navigation = True
                if len(entity_card_ids) != 2 or len(assignment_ids) != 2 or not duplicate_refused:
                    raise AssertionError("Entity review did not produce two cards, two assignments, and one duplicate refusal")
                ip_card_id = entity_card_ids[1]
                person_name = "Синтетический Связанный"
                entity_page.goto(f"{base}/debtors/new?entity_kind=person", wait_until="networkidle")
                entity_page.locator('[name="last_name"]').fill("Синтетический")
                entity_page.locator('[name="first_name"]').fill("Связанный")
                entity_page.get_by_role("button", name="Подтвердить данные").click()
                entity_page.wait_for_url("**/debtors/*?saved=1")
                person_id = int(urlsplit(entity_page.url).path.rsplit("/", 1)[1])
                with SessionLocal() as db:
                    person = db.get(DebtorCardDraft, person_id)
                    if (
                        person is None
                        or person.entity_kind != "person"
                        or person.preparation_state != "reviewed"
                        or person.display_name != person_name
                    ):
                        raise AssertionError("Manual synthetic person form did not create the reviewed link target")
                entity_page.goto(f"{base}/debtors/{ip_card_id}", wait_until="networkidle")
                entity_page.locator('input[name="person_q"]').fill(person_name)
                entity_page.get_by_role("button", name="Найти").click()
                entity_page.wait_for_load_state("networkidle")
                link_form = entity_page.locator(f'form[action$="/debtors/{ip_card_id}/related-person"]').last
                person_select = link_form.locator('select[name="person_card_id"]')
                if person_select.count() != 1:
                    raise AssertionError(
                        "Person-card search returned no selectable reviewed card: "
                        + entity_page.locator("body").inner_text()[-1200:]
                    )
                person_select.select_option(str(person_id))
                first_link_version = link_form.locator('input[name="version"]').input_value()
                link_form.get_by_role("button", name="Сохранить выбранную связь").click()
                entity_page.wait_for_url(f"**/debtors/{ip_card_id}?saved=1")
                with SessionLocal() as db:
                    ip_card = db.get(DebtorCardDraft, ip_card_id)
                    linked_version = ip_card.version
                    linked_revision_count = int(db.scalar(
                        select(func.count()).select_from(DebtorCardDraftRevision).where(
                            DebtorCardDraftRevision.debtor_card_id == ip_card_id
                        )
                    ) or 0)
                    if ip_card.values.get("related_person_card_id", {}).get("value") != str(person_id):
                        raise AssertionError("The selected synthetic person was not linked to the IP card")
                entity_page.locator('input[name="person_q"]').fill(person_name)
                entity_page.get_by_role("button", name="Найти").click()
                entity_page.wait_for_load_state("networkidle")
                replay_link_form = entity_page.locator(
                    f'form[action$="/debtors/{ip_card_id}/related-person"]'
                ).last
                replay_link_form.locator('select[name="person_card_id"]').select_option(str(person_id))
                replay_link_form.locator('input[name="version"]').evaluate(
                    "(element, value) => { element.value = value; }",
                    first_link_version,
                )
                replay_link_form.get_by_role("button", name="Сохранить выбранную связь").click()
                entity_page.wait_for_url(f"**/debtors/{ip_card_id}?saved=1")
                with SessionLocal() as db:
                    ip_card = db.get(DebtorCardDraft, ip_card_id)
                    if ip_card.version != linked_version:
                        raise AssertionError("Exact person-link replay created a new card version")
                    if int(db.scalar(
                        select(func.count()).select_from(DebtorCardDraftRevision).where(
                            DebtorCardDraftRevision.debtor_card_id == ip_card_id
                        )
                    ) or 0) != linked_revision_count:
                        raise AssertionError("Exact person-link replay created an extra revision")
                entity_flow_result = {
                    "ran": True,
                    "synthetic_company_and_ip_scans_uploaded_together": True,
                    "entity_review_tasks": len(entity_tasks),
                    "reviewed_internal_cards": len(entity_card_ids),
                    "linked_internal_assignment_drafts": len(assignment_ids),
                    "employee_correction_saved": corrected_company_saved,
                    "review_draft_resumed_after_navigation": corrected_company_saved,
                    "same_kind_duplicate_inn_refused": duplicate_refused,
                    "assignment_replay_after_navigation_returns_same_draft": assignment_replay_after_navigation,
                    "assignment_waiting_for_practitioner_saved_and_resumed": assignment_waiting_state_saved,
                    "ip_person_link_saved_and_exact_retry_replayed": True,
                    "external_sro_accessed": False,
                }
        browser.close()

    screenshots = [
        "dashboard.png",
        "prepared-card.png",
        "low-resolution-date-review.png",
    ]
    if args.entity_flow:
        screenshots.extend(
            [
                "entity-review.png",
                "entity-card.png",
                "assignment-awaiting-information.png",
            ]
        )
    result = {
        "synthetic_only": True,
        "browser_mode": "headed" if args.headed else "headless",
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
        "failed_task_links_to_explicit_new_ocr_attempt": True,
        "intentional_same_bytes_reprocessing": repeat_result,
        "dashboard_prepared_filter_rows": automatic_card_count,
        "dashboard_metric_grid": {
            "tiles": len(metric_rects),
            "rows": len(metric_rows),
            "widest_row": metric_columns,
        },
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
        "low_resolution_date_review": {
            "initial_candidate_matches_expected": initial_date == manual_source["doc_date"],
            "warning_shown_before_save": "Распознавание даты расходится при другом масштабе" in review_text,
            "employee_saved_date_persisted": persisted_date == manual_source["doc_date"],
            "source_preview_pdf_response_verified": True,
        },
        "screenshots": screenshots,
        "entity_flow": entity_flow_result,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
