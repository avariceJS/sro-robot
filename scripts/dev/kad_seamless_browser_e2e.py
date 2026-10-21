#!/usr/bin/env python3
"""Headed Chromium E2E for the HTTPS KAD service bridge and local receipt.

The app is reached through the canonical production origin, but every request
is routed to the supplied loopback app. KAD pages and PDF bytes are in-memory
synthetic fixtures. The check requires a fresh APP_DEBUG loopback benchmark DB
and an empty private SRO_ROBOT_ENV_FILE. It never contacts KAD or SROArbitr.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import time
from io import BytesIO
from urllib.parse import urlsplit
from zipfile import ZipFile

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from itsdangerous import TimestampSigner

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.kad_helper_package import store_archive
from app.db.base import SessionLocal
from app.db.models import (
    AssignmentDraft,
    DebtorCardDraft,
    Document,
    Event,
    IntakeItem,
    IntakeOperation,
    JobOutbox,
    KadImportRequestReceipt,
    KadSourceRun,
    LoginSession,
    Task,
    User,
)
from app.services import auth
from app.services.kad_import import check_case
from app.settings import settings
from sro_robot.core.extract import parse_file


OFFICE_ORIGIN = "https://sro-office.ru"
KAD_ORIGIN = "https://kad.arbitr.ru"
CASE = "А41-12345/2026"
CARD_ID = "11111111-2222-4333-8444-555555555555"
ACT_ID_EARLIER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ACT_ID_SELECTED = "99999999-8888-4777-8666-555555555555"
ACT_DATE = "2026-02-10"
SELECTED_LABEL = "Синтетическое определение о принятии заявления.pdf"
SELECTED_PDF_PATH = f"/Kad/PdfDocument/{CARD_ID}/{ACT_ID_SELECTED}/selected-act.pdf"
def require_safe_runtime(base_url: str) -> None:
    env_path = Path(os.environ.get("SRO_ROBOT_ENV_FILE", ""))
    database = make_url(settings.database_url)
    target = urlsplit(base_url)
    if (
        not env_path.is_file()
        or env_path.is_symlink()
        or env_path.stat().st_size
        or env_path.stat().st_mode & 0o077
    ):
        raise SystemExit("SRO_ROBOT_ENV_FILE must point to an empty private regular file")
    if (
        not settings.debug
        or os.environ.get("APP_DEBUG") != "1"
        or database.get_backend_name() != "postgresql"
        or database.host not in {"127.0.0.1", "localhost", "::1"}
        or "benchmark" not in (database.database or "").casefold()
    ):
        raise SystemExit("Refusing browser check outside APP_DEBUG loopback benchmark PostgreSQL")
    if (target.scheme, target.hostname) != ("http", "127.0.0.1") or target.port is None:
        raise SystemExit("Refusing non-loopback or non-HTTP app URL")


def require_empty_database() -> None:
    models = (
        User, LoginSession, Document, Task, Event, IntakeOperation, IntakeItem,
        JobOutbox, KadSourceRun, KadImportRequestReceipt, DebtorCardDraft,
        AssignmentDraft,
    )
    with SessionLocal() as db:
        counts = {
            model.__name__: int(db.scalar(select(func.count()).select_from(model)) or 0)
            for model in models
        }
    if any(counts.values()):
        raise SystemExit(f"Browser check requires an empty benchmark database: {counts}")


def create_staff_cookie() -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    with SessionLocal() as db:
        user = User(
            login=f"synthetic-kad-seamless-{secrets.token_hex(8)}",
            full_name="Synthetic KAD Browser Staff",
            password_hash="synthetic-only",
            sro_login="synthetic-only",
            is_active=True,
        )
        db.add(user)
        db.flush()
        session = LoginSession(
            user_id=user.id,
            token_sha256=auth._session_token_sha256(token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            external_state_ciphertext=b"synthetic-browser-state",
            external_state_expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        db.add(session)
        db.commit()
        user_id = user.id
    payload = base64.b64encode(json.dumps(
        {"user_id": user_id, "session_token": token}, separators=(",", ":")
    ).encode())
    signed = TimestampSigner(settings.secret_key).sign(payload).decode("ascii")
    return signed, user_id


def synthetic_pdf() -> bytes:
    # A searchable, one-page synthetic Russian act. This uses the repo's
    # pinned benchmark-only ReportLab dependency and an installed Cyrillic
    # TrueType font so the normal local OCR/parser can confirm the case/date.
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font_candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    )
    font_path = next((path for path in font_candidates if path.is_file()), None)
    if font_path is None:
        raise RuntimeError("A local Cyrillic TrueType font is required for the synthetic OCR fixture")
    font_name = "KadSyntheticCyrillic"
    try:
        pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
    except KeyError:
        pass
    output = BytesIO()
    page = canvas.Canvas(output, pagesize=(595, 842), invariant=1)
    page.setTitle("Synthetic KAD act for local OCR only")
    page.setFont(font_name, 16)
    for y, line in zip(
        (780, 748, 716, 684),
        (
            "ОПРЕДЕЛЕНИЕ О ПРИНЯТИИ ЗАЯВЛЕНИЯ",
            "Дело № А41-12345/2026",
            "Дата судебного акта: 10.02.2026",
            "СИНТЕТИЧЕСКИЙ ДОКУМЕНТ. ДАННЫЕ ВЫМЫШЛЕНЫ.",
        ),
    ):
        page.drawString(56, y, line)
    page.showPage()
    page.save()
    return output.getvalue()


def assert_positive_ocr_fixture(pdf: bytes) -> dict[str, str]:
    source = {
        "candidateInventoryComplete": False,
        "caseNumber": CASE,
        "requestId": "12345678-1234-4234-8234-123456789abc",
        "cardUrl": f"{KAD_ORIGIN}/Card/{CARD_ID}",
        "selectedUrl": f"{KAD_ORIGIN}{SELECTED_PDF_PATH}",
        "actDate": ACT_DATE,
        "selectionReason": "employee_selected_visible_document",
        "candidates": [{
            "url": f"{KAD_ORIGIN}{SELECTED_PDF_PATH}",
            "label": SELECTED_LABEL,
            "actDate": ACT_DATE,
        }],
    }
    with tempfile.NamedTemporaryFile(suffix=".pdf") as fixture:
        fixture.write(pdf)
        fixture.flush()
        card = parse_file(fixture.name)
    check_case(source, card)
    return {"case_number": card.case_number.value, "act_date": card.doc_date.value}


def search_fixture() -> str:
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Картотека арбитражных дел</title>
<style>body{{font:16px sans-serif}} .tag{{display:inline-block}} i{{display:inline-block;width:18px;height:18px}}</style></head>
<body><main><input placeholder="например, А50-5568/08">
<div id="sug-cases"><div class="tag"><i class="b-icon add"></i></div></div>
<div id="b-form-submit"><button type="submit">Найти</button></div>
<div id="results"><p>Укажите атрибуты дела для поиска</p></div></main>
<script>
window.syntheticSearchSubmissions=0;
const input=document.querySelector('input[placeholder="например, А50-5568/08"]');
document.querySelector('#sug-cases .tag > i.b-icon.add').addEventListener('click',()=>{{
 const tag=document.createElement('span');tag.className='tag added';
 const committed=document.createElement('input');committed.value=input.value;tag.append(committed);
 document.querySelector('#sug-cases').append(tag);input.value='';
}});
document.querySelector('#b-form-submit button[type=submit]').addEventListener('click',event=>{{
 event.preventDefault();window.syntheticSearchSubmissions++;
 const committed=document.querySelector('#sug-cases .tag.added input').value;
 document.querySelector('#results').innerHTML=`<p>Найдено 1 дело</p><table><tbody><tr><td>${{committed}}</td><td><a href="https://kad.arbitr.ru/Card/{CARD_ID}">${{committed}}</a></td></tr></tbody></table>`;
}});
</script></body></html>"""


def card_fixture() -> str:
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{CASE}</title></head>
<body><h1>{CASE} · Арбитражный суд</h1><section>
<div class="b-chrono-item-header"><span class="b-case-instance-number">{CASE}</span><button class="b-collapse js-collapse" type="button">Развернуть</button></div>
<div class="js-chrono-items-container" style="display:none">
<article class="js-chrono-item" data-date="09.02.2026"><span class="case-date">09.02.2026</span>
<a href="{KAD_ORIGIN}/Kad/PdfDocument/{CARD_ID}/{ACT_ID_EARLIER}/earlier.pdf">Ранее опубликованный акт.pdf</a></article>
<article class="js-chrono-item" data-date="10.02.2026"><span class="case-date">10.02.2026</span>
<a href="{KAD_ORIGIN}{SELECTED_PDF_PATH}">{SELECTED_LABEL}</a></article></div></section>
<script>document.querySelector('.b-collapse.js-collapse').addEventListener('click',event=>{{
 const header=event.currentTarget.closest('.b-chrono-item-header');header.classList.add('b-chrono-item-header-expanded');
 header.nextElementSibling.style.display='block';}});</script></body></html>"""


def inspect_package(directory: Path, *, legacy: bool = False) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=True)
    archive_bytes = store_archive("chrome")
    with ZipFile(__import__("io").BytesIO(archive_bytes)) as archive:
        archive.extractall(directory)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if legacy:
        # A production artifact with only its version pin lowered exercises the
        # real page's update-required branch without substituting a fake bridge.
        manifest["version"] = "0.2.12"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if manifest.get("version") != ("0.2.12" if legacy else "0.3.0"):
        raise AssertionError("Packaged helper version did not match the expected production pin")
    if manifest.get("host_permissions") != ["https://kad.arbitr.ru/*", OFFICE_ORIGIN + "/*"]:
        raise AssertionError("Store package includes a host other than canonical office and KAD origins")
    scripts = [item for item in manifest.get("content_scripts", []) if "service.js" in item.get("js", [])]
    if len(scripts) != 1 or scripts[0].get("matches") != [OFFICE_ORIGIN + "/*"]:
        raise AssertionError("Store package does not pin the service script to the production HTTPS origin")
    return {"version": manifest["version"], "sha256": sha256(archive_bytes).hexdigest()}


def wait_for_status(page, expected: str, timeout_ms: int = 12000) -> str:
    status = page.locator("#kad-browser-status")
    status.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_function(
        "([selector, marker]) => document.querySelector(selector)?.textContent.includes(marker)",
        arg=["#kad-browser-status", expected], timeout=timeout_ms,
    )
    return status.inner_text()


def assert_no_overflow(page, viewport_width: int) -> None:
    metrics = page.evaluate("""() => ({
      viewport: document.documentElement.clientWidth,
      scroll: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
    })""")
    if metrics["viewport"] != viewport_width or metrics["scroll"] > viewport_width or metrics["body"] > viewport_width:
        raise AssertionError(f"Page overflow at {viewport_width}px: {metrics}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:64622")
    parser.add_argument("--evidence-dir", default="/tmp/sro-kad-seamless-demo/browser-e2e")
    parser.add_argument("--keep-browser", action="store_true", help="Leave the headed production-extension window open")
    args = parser.parse_args()
    require_safe_runtime(args.base_url)
    require_empty_database()
    base_url = args.base_url.rstrip("/")
    evidence = Path(args.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    extension_root = evidence / "production-chrome-extension"
    legacy_root = evidence / "simulated-legacy-extension"
    profile_root = Path(tempfile.mkdtemp(prefix="sro-kad-seamless-e2e-"))
    package = inspect_package(extension_root)
    inspect_package(legacy_root, legacy=True)
    staff_cookie, staff_user_id = create_staff_cookie()
    pdf = synthetic_pdf()
    ocr_fixture = assert_positive_ocr_fixture(pdf)
    unexpected_requests: list[str] = []
    app_statuses: list[int] = []
    fixture_counts = {"search": 0, "card": 0, "pdf": 0}
    console_errors: list[str] = []
    results: dict[str, object] = {
        "synthetic_only": True,
        "external_sro_accessed": False,
        "external_kad_accessed": False,
        "production_package": package,
        "production_package_installed_as_unpacked_extension": True,
        "legacy_ui_check": "simulated older manifest version 0.2.12 using current packaged source; update gate only",
        "office_origin": OFFICE_ORIGIN,
        "local_app_origin": base_url,
        "screenshots": [],
        "ocr_fixture_preflight": ocr_fixture,
    }

    app_routes = (
        "/", "/debtors/intake", "/kad-helper", "/tasks/",
        "/kad-helper/",
        "/static/", "/assets/", "/health", "/ready",
    )

    def allowed_app_path(path: str) -> bool:
        return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in app_routes)

    def make_interceptor(pdf_bytes: bytes):
        def intercept(route) -> None:
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.scheme in {"http", "https"} and parsed.hostname == "sro-office.ru":
                if not allowed_app_path(parsed.path):
                    unexpected_requests.append("office:" + parsed.path)
                    route.abort()
                    return
                headers = dict(request.headers)
                headers["host"] = "sro-office.ru"
                headers["x-forwarded-proto"] = "https"
                headers["x-forwarded-host"] = "sro-office.ru"
                try:
                    response = route.fetch(
                        url=base_url + parsed.path + (("?" + parsed.query) if parsed.query else ""),
                        headers=headers,
                        timeout=30000,
                    )
                    app_statuses.append(response.status)
                    route.fulfill(response=response)
                except PlaywrightError:
                    unexpected_requests.append("office:local-forward-failed")
                    route.abort()
                return
            if parsed.scheme == "https" and parsed.hostname == "kad.arbitr.ru":
                if request.is_navigation_request and parsed.path == "/":
                    fixture_counts["search"] += 1
                    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=search_fixture())
                elif request.is_navigation_request and parsed.path == f"/Card/{CARD_ID}":
                    fixture_counts["card"] += 1
                    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=card_fixture())
                elif parsed.path == SELECTED_PDF_PATH:
                    fixture_counts["pdf"] += 1
                    route.fulfill(status=200, content_type="application/pdf", body=pdf_bytes,
                                  headers={"content-disposition": 'inline; filename="selected-act.pdf"'})
                elif parsed.path.endswith(".pdf"):
                    # The employee selects the second row. Opening any other PDF
                    # would be a selection or stale-link failure.
                    unexpected_requests.append("kad:unselected-pdf")
                    route.abort()
                else:
                    unexpected_requests.append("kad:" + parsed.path)
                    route.abort()
                return
            unexpected_requests.append(f"external:{parsed.scheme}:{parsed.hostname or ''}")
            route.abort()
        return intercept

    def block_http(route) -> None:
        parsed = urlsplit(route.request.url)
        unexpected_requests.append(f"unexpected-http:{parsed.hostname or ''}{parsed.path}")
        route.abort()

    def install_network_guard(context, pdf_bytes: bytes) -> None:
        # Only web traffic belongs to these fixtures. Let Chrome's own PDF
        # viewer and extension pages run outside Playwright's Fetch routing.
        context.route("https://**/*", make_interceptor(pdf_bytes))
        context.route("http://**/*", block_http)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        prod_context = None
        legacy_context = None
        try:
            # No-extension state on the actual intake page.
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
            install_network_guard(context, pdf)
            context.add_cookies([{"name": "session", "value": staff_cookie, "url": OFFICE_ORIGIN}])
            page = context.new_page()
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.goto(OFFICE_ORIGIN + "/debtors/intake", wait_until="domcontentloaded")
            page.locator("#kad-case-number").fill(CASE)
            page.locator("#kad-browser-search").evaluate("form => form.requestSubmit()")
            wait_for_status(page, "не подключён")
            page.set_viewport_size({"width": 1440, "height": 1000})
            assert_no_overflow(page, 1440)
            screenshot = evidence / "intake-unavailable-desktop.png"
            page.screenshot(path=str(screenshot), full_page=True)
            results["screenshots"].append(str(screenshot))
            results["unavailable_state"] = page.locator("#kad-browser-status").inner_text()
            context.close()

            # Production store package loaded into a fresh headed Chromium profile.
            user_data = profile_root / "production"
            prod_context = playwright.chromium.launch_persistent_context(
                str(user_data), headless=False, viewport={"width": 1440, "height": 1000},
                args=[
                    f"--disable-extensions-except={extension_root}",
                    f"--load-extension={extension_root}",
                    "--disable-features=OptimizationHints",
                ],
            )
            install_network_guard(prod_context, pdf)
            prod_context.add_cookies([{"name": "session", "value": staff_cookie, "url": OFFICE_ORIGIN}])
            prod_page = prod_context.new_page()
            prod_page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            prod_page.goto(OFFICE_ORIGIN + "/debtors/intake", wait_until="domcontentloaded")
            status = wait_for_status(prod_page, "Помощник подключён и совместим", timeout_ms=20000)
            prod_page.set_viewport_size({"width": 1440, "height": 1000})
            assert_no_overflow(prod_page, 1440)
            desktop_screenshot = evidence / "intake-connected-desktop.png"
            prod_page.screenshot(path=str(desktop_screenshot), full_page=True)
            results["screenshots"].append(str(desktop_screenshot))
            results["connected_state"] = status

            # The search action remains blocked until a compatible extension handshake.
            form = prod_page.locator("#kad-browser-search")
            case_input = prod_page.locator("#kad-case-number")
            date_input = prod_page.locator("#kad-act-date")
            submit = form.locator('button[type="submit"]')
            if submit.is_disabled() or form.get_attribute("data-kad-bridge") != "ready":
                raise AssertionError("Installed production helper did not complete the content bridge handshake")
            case_input.fill(CASE)
            date_input.fill(ACT_DATE)
            submit.click()
            candidates = prod_page.locator("#kad-candidate-choices")
            selected_row = candidates.locator("article.kad-candidate-choice").filter(has_text=SELECTED_LABEL)
            earlier_row = candidates.locator("article.kad-candidate-choice").filter(has_text="Ранее опубликованный акт.pdf")
            try:
                selected_row.get_by_role("button", name="Выбрать этот PDF для распознавания").wait_for(state="visible", timeout=45000)
            except Exception:
                failure_screenshot = evidence / "intake-candidate-failure.png"
                prod_page.screenshot(path=str(failure_screenshot), full_page=True)
                diagnostic = {
                    "status": prod_page.locator("#kad-browser-status").inner_text(),
                    "candidate_text": candidates.inner_text(),
                    "fixture_counts": fixture_counts,
                    "console_errors": console_errors,
                    "unexpected_requests": unexpected_requests[:12],
                    "tabs": prod_context.pages and [item.url for item in prod_context.pages],
                    "screenshot": str(failure_screenshot),
                }
                (evidence / "candidate-failure.json").write_text(
                    json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                print(json.dumps(diagnostic, ensure_ascii=False, indent=2))
                raise
            candidate_buttons = candidates.locator("button")
            if candidate_buttons.count() != 2:
                raise AssertionError("Expected both visible synthetic chronology documents")
            candidate_copy = candidates.inner_text()
            if "09.02.2026" not in candidate_copy or "10.02.2026" not in candidate_copy or "полнота списка страниц не подтверждена" not in candidate_copy.casefold():
                raise AssertionError("Candidate list did not show explicit title/date and partial-inventory note")
            date_disabled = earlier_row.get_by_role("button", name="Выбрать этот PDF для распознавания")
            if not date_disabled.is_disabled():
                raise AssertionError("Non-matching date candidate must be disabled")
            selected = selected_row.get_by_role("button", name="Выбрать этот PDF для распознавания")
            mobile_screenshot = evidence / "intake-candidates-mobile.png"
            prod_page.set_viewport_size({"width": 390, "height": 844})
            assert_no_overflow(prod_page, 390)
            prod_page.screenshot(path=str(mobile_screenshot), full_page=True)
            results["screenshots"].append(str(mobile_screenshot))
            results["mobile_overflow"] = "none at 390px"
            prod_page.set_viewport_size({"width": 1280, "height": 800})
            assert_no_overflow(prod_page, 1280)
            candidate_1280 = evidence / "intake-candidates-desktop-1280x800.png"
            candidates.scroll_into_view_if_needed()
            prod_page.screenshot(path=str(candidate_1280))
            results["screenshots"].append(str(candidate_1280))
            selected.click()
            try:
                deadline = time.monotonic() + 35
                task_ready = False
                while time.monotonic() < deadline:
                    current_path = urlsplit(prod_page.url).path
                    if current_path.startswith("/tasks/") and current_path.rsplit("/", 1)[1].isdigit():
                        task_ready = True
                        break
                    current_status = prod_page.locator("#kad-browser-status").inner_text().casefold()
                    if any(term in current_status for term in (
                        "потеряна связь", "передача остановлена", "не подтверждён", "ошибка", "недоступно",
                    )):
                        break
                    prod_page.wait_for_timeout(500)
                if not task_ready:
                    raise TimeoutError("Selected PDF did not reach the local task receipt")
            except Exception:
                diagnostic = {
                    "service_url": prod_page.url,
                    "service_status": prod_page.locator("#kad-browser-status").inner_text(),
                    "fixture_counts": fixture_counts,
                    "console_errors": console_errors,
                    "unexpected_requests": unexpected_requests[:12],
                    "tabs": [],
                }
                for index, tab in enumerate(prod_context.pages):
                    info = {"url": tab.url}
                    if tab.url.startswith(KAD_ORIGIN):
                        try:
                            info["body_text"] = tab.locator("body").inner_text(timeout=3000)[:1200]
                            path = evidence / f"kad-tab-after-selection-{index}.png"
                            tab.screenshot(path=str(path), full_page=True)
                            info["screenshot"] = str(path)
                        except Exception as exc:
                            info["capture_error"] = type(exc).__name__
                    diagnostic["tabs"].append(info)
                (evidence / "pdf-capture-failure.json").write_text(
                    json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                failure_screenshot = evidence / "intake-pdf-capture-failure.png"
                prod_page.screenshot(path=str(failure_screenshot), full_page=True)
                diagnostic["screenshot"] = str(failure_screenshot)
                print(json.dumps(diagnostic, ensure_ascii=False, indent=2))
                raise
            task_id = int(urlsplit(prod_page.url).path.rsplit("/", 1)[1])
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                source_run = db.scalar(select(KadSourceRun).where(KadSourceRun.task_id == task_id))
                receipt = db.scalar(select(KadImportRequestReceipt).where(KadImportRequestReceipt.user_id == staff_user_id))
                document = db.get(Document, task.document_id) if task else None
                if not task or not source_run or not receipt or not document:
                    raise AssertionError("The real intake route did not persist task, selected source, receipt, and document")
                if task.case_number != CASE or source_run.generation != 1:
                    raise AssertionError("Persisted task/source identity did not match the synthetic selected case")
                if document.content_hash != sha256(pdf).hexdigest():
                    raise AssertionError("Backend document bytes did not match the browser-captured synthetic PDF")
                persisted_metadata = receipt.source_metadata
                if persisted_metadata.get("selectedUrl", "").endswith(SELECTED_PDF_PATH) is False:
                    raise AssertionError("The backend receipt lost the explicitly selected PDF provenance")
                if persisted_metadata.get("selectionReason") != "employee_selected_visible_document":
                    raise AssertionError("The backend receipt lost the employee selection reason")
                task_status = task.status.value if hasattr(task.status, "value") else str(task.status)
            results["import_receipt"] = {
                "task_id": task_id,
                "staff_id": staff_user_id,
                "task_status_after_import": task_status,
                "document_sha256": sha256(pdf).hexdigest(),
                "source_selection": SELECTED_LABEL,
                "source_date": ACT_DATE,
                "selection_reason": "employee_selected_visible_document",
                "backend_receipt": "persisted",
                "ocr_dispatch": "local task admitted; worker completion not asserted",
            }
            prod_page.screenshot(path=str(evidence / "task-receipt-desktop.png"), full_page=True)
            results["screenshots"].append(str(evidence / "task-receipt-desktop.png"))

            # Test the real update-required UI with the production artifact's
            # source unchanged apart from a deliberately stale manifest version.
            legacy_user_data = profile_root / "legacy"
            legacy_context = playwright.chromium.launch_persistent_context(
                str(legacy_user_data), headless=False, viewport={"width": 1440, "height": 1000},
                args=[
                    f"--disable-extensions-except={legacy_root}",
                    f"--load-extension={legacy_root}",
                ],
            )
            install_network_guard(legacy_context, pdf)
            legacy_context.add_cookies([{"name": "session", "value": staff_cookie, "url": OFFICE_ORIGIN}])
            legacy_page = legacy_context.new_page()
            legacy_page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            legacy_page.goto(OFFICE_ORIGIN + "/debtors/intake", wait_until="domcontentloaded")
            legacy_text = wait_for_status(legacy_page, "несовместима", timeout_ms=20000)
            if not legacy_page.locator("#kad-browser-search button[type=submit]").is_disabled():
                raise AssertionError("Update-required state must keep search blocked")
            legacy_screenshot = evidence / "intake-update-required-desktop.png"
            legacy_page.screenshot(path=str(legacy_screenshot), full_page=True)
            results["screenshots"].append(str(legacy_screenshot))
            results["update_required_state"] = legacy_text

            # Connection setup page has no extension-specific browser dependency.
            connection_page = prod_context.new_page()
            connection_page.goto(OFFICE_ORIGIN + "/kad-helper", wait_until="domcontentloaded")
            connection_status_element = connection_page.locator("#kad-helper-connection-status")
            connection_status_element.wait_for(state="visible", timeout=15000)
            connection_page.wait_for_function(
                "document.querySelector('#kad-helper-connection-status')?.dataset.state === 'ready'",
                timeout=20000,
            )
            connection_page.set_viewport_size({"width": 1280, "height": 800})
            assert_no_overflow(connection_page, 1280)
            connection_status = connection_page.locator("#kad-helper-connection-status").inner_text()
            connection_page.screenshot(path=str(evidence / "helper-connection-desktop-1280x800.png"))
            results["screenshots"].append(str(evidence / "helper-connection-desktop-1280x800.png"))
            results["connection_page"] = connection_status

            if unexpected_requests:
                raise AssertionError(f"Unexpected external or fixture requests: {unexpected_requests[:12]}")
            if fixture_counts != {"search": 1, "card": 1, "pdf": 1}:
                raise AssertionError(f"Synthetic KAD fixture sequence was not exact: {fixture_counts}")
            if any(status >= 500 for status in app_statuses):
                raise AssertionError(f"Local app returned a server error: {app_statuses}")
            # Browsers can emit benign extension initialization messages; only
            # application-page console errors are recorded by these handlers.
            if console_errors:
                raise AssertionError(f"Browser console errors: {console_errors[:8]}")
            results["synthetic_kad_fixture_requests"] = fixture_counts
            results["unexpected_requests"] = 0
            results["console_errors"] = 0
            results["app_http_5xx"] = 0
            results_path = evidence / "result.json"
            results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(results, ensure_ascii=False, indent=2))

            if args.keep_browser:
                # Keep the production-enabled representative window visible for
                # the owner. The test data and local app remain synthetic.
                connection_page.bring_to_front()
                print("Headed Chromium remains open; press Ctrl-C to close this check.")
                while True:
                    connection_page.wait_for_timeout(1000)
        finally:
            if legacy_context is not None:
                legacy_context.close()
            if prod_context is not None:
                prod_context.close()
            browser.close()
            shutil.rmtree(profile_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
