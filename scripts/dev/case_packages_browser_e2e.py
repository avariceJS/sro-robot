#!/usr/bin/env python3
"""Exercise explicit case-package work in loopback Chromium and disposable PostgreSQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import urlopen
import uuid

from playwright.sync_api import sync_playwright
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal
from app.db.models import Document, IntakeItem, IntakeOperation, IntakeSource, Task, TaskStatus, User
from app.services import case_packages
from scripts.dev.bulk_benchmark import make_signed_cookie
from tests.postgres_safety import assert_disposable_postgres


def allow_loopback(route) -> None:
    target = urlsplit(route.request.url)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        route.abort("blockedbyclient")
        return
    route.continue_()


def start_app(base_url: str, log_path: Path) -> subprocess.Popen:
    target = urlsplit(base_url)
    with socket.socket() as probe:
        if probe.connect_ex((target.hostname or "", target.port or 0)) == 0:
            raise SystemExit("Refusing to use a loopback port owned by another process")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(target.port), "--log-level", "warning"],
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log_file.close()
            raise RuntimeError(f"Owned local web process exited ({process.returncode})")
        try:
            with urlopen(base_url + "/healthz", timeout=1) as response:
                if response.status == 200:
                    log_file.close()
                    return process
        except (OSError, URLError):
            time.sleep(0.2)
    stop_app(process)
    log_file.close()
    raise TimeoutError("Owned loopback web process did not become ready")


def stop_app(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def seed_browser_inputs(user_id: int) -> int:
    suffix = uuid.uuid4().hex
    payload = f"synthetic browser case act {suffix}".encode()
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None:
            raise RuntimeError("Synthetic browser user was not persisted")
        document = Document(
            filename=f"browser-document-{suffix}.pdf",
            stored_path=f"/tmp/{suffix}.pdf",
            content_hash=hashlib.sha256(payload).hexdigest(),
            source=IntakeSource.upload,
            uploaded_by_id=user_id,
        )
        db.add(document)
        db.flush()
        operation = IntakeOperation(
            user_id=user_id,
            idempotency_key=uuid.uuid4().hex * 2,
            manifest_sha256=hashlib.sha256(f"manifest-{suffix}".encode()).hexdigest(),
            source_metadata_sha256=None,
        )
        db.add(operation)
        db.flush()
        occurrence = IntakeItem(
            operation_id=operation.id,
            item_index=0,
            document_id=document.id,
            filename=f"browser-source-occurrence-{suffix}.pdf",
            source=IntakeSource.upload,
            received_by_id=user_id,
        )
        db.add(occurrence)
        db.flush()
        task = Task(
            document_id=document.id,
            scenario="person",
            profile="default",
            status=TaskStatus.needs_review,
            recognized={},
            values={},
            case_number=f"А00-{suffix[:8]}/2026",
            intake_operation_id=operation.id,
            intake_item_id=occurrence.id,
        )
        db.add(task)
        db.flush()
        task_id = task.id
        for index in range(26):
            similar_document = Document(
                filename=f"court-act-{index:02d}-{suffix}.pdf",
                stored_path=f"/tmp/{suffix}-{index:02d}.pdf",
                content_hash=hashlib.sha256(f"similar-{suffix}-{index}".encode()).hexdigest(),
                source=IntakeSource.upload,
                uploaded_by_id=user_id,
            )
            db.add(similar_document)
            db.flush()
            db.add(Task(
                document_id=similar_document.id,
                scenario="person",
                profile="default",
                status=TaskStatus.needs_review,
                recognized={},
                values={},
                case_number="А77-1/2026",
            ))
        for index in range(46):
            case_packages.create_once(
                db,
                user_id=user_id,
                request_key=uuid.uuid4().hex * 2,
                title=f"Synthetic browser paging {suffix[:8]} {index:02d}",
                case_number="",
            )
        db.commit()
    return task_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()

    assert_disposable_postgres()
    if (
        os.environ.get("SRO_CASE_PACKAGE_TEST_DATABASE") != "1"
        or os.environ.get("APP_DEBUG") != "1"
    ):
        raise SystemExit("Refusing browser check outside the marked case-package test database")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    cookie, user_id = make_signed_cookie()
    task_id = seed_browser_inputs(user_id)
    process = start_app(base, args.log)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            context.route("**/*", allow_loopback)
            context.add_cookies([{"name": "session", "value": cookie, "url": base}])
            page = context.new_page()
            page.goto(base + "/case-packages", wait_until="networkidle")
            page.get_by_role("link", name="Создать папку").click()
            page.wait_for_url("**/case-packages/new")
            page.locator('input[name="title"]').fill("Синтетическая папка Chromium")
            page.locator('input[name="case_number"]').fill("А00-SYNTHETIC/2026")
            page.get_by_role("button", name="Создать папку").click()
            page.wait_for_url("**/case-packages/*?created=1")
            package_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
            page.goto(base + "/case-packages", wait_until="networkidle")
            page.locator('input[name="q"]').fill("А00-SYNTHETIC/2026")
            page.get_by_role("button", name="Найти").click()
            page.wait_for_load_state("networkidle")
            package_link = page.get_by_role("link", name="Синтетическая папка Chromium")
            if package_link.count() != 1:
                raise AssertionError("Case package search did not find the title/number match")
            package_link.click()
            page.wait_for_url(f"**/case-packages/{package_id}")

            with SessionLocal() as db:
                task = db.get(Task, task_id)
                query = task.case_number
                source_filename = task.intake_item.filename
            page.locator('input[name="q"]').fill(query)
            page.get_by_role("button", name="Найти").click()
            page.wait_for_load_state("networkidle")
            row = page.locator("tbody tr").filter(has_text=source_filename)
            if row.count() != 1:
                raise AssertionError("Case-package search did not show the synthetic source occurrence")
            stale_add_url = row.locator("form").get_attribute("action")
            if not stale_add_url or source_filename not in row.inner_text():
                raise AssertionError("Candidate row omitted its occurrence filename or add action")
            row.get_by_role("button", name="Добавить").click()
            page.wait_for_load_state("networkidle")
            members_section = page.locator('section[aria-labelledby="package-documents-heading"]')
            if source_filename not in members_section.inner_text():
                raise AssertionError("Explicitly added source did not appear in the package")

            member = members_section.locator("tbody tr").filter(has_text=source_filename)
            member.locator("button", has_text="Убрать").click()
            page.wait_for_load_state("networkidle")
            stale_response = page.request.post(
                urljoin(base + "/", stale_add_url),
                headers={"Origin": base, "Referer": page.url},
                max_redirects=0,
            )
            if stale_response.status != 303:
                raise AssertionError("A stale add replay was not redirected as a rejected transition")
            location = stale_response.headers.get("location", "")
            if "Состав папки изменился" not in unquote(location):
                raise AssertionError("Stale replay did not explain that the package had changed")
            with SessionLocal() as db:
                if case_packages.current_membership_events(db, package_id):
                    raise AssertionError("Stale replay restored a removed package member")

            page.goto(base + "/case-packages", wait_until="networkidle")
            older = page.get_by_role("link", name="Старее →")
            if older.count() != 1:
                raise AssertionError("More than 50 packages did not expose the next list page")
            older.click()
            page.wait_for_url("**/case-packages?page=2")
            if page.locator("tbody tr").count() < 1:
                raise AssertionError("The second case-package list page was empty")
            page.goto(base + f"/case-packages/{package_id}", wait_until="networkidle")
            search = page.locator('input[name="q"]')
            search.fill("А77-1/2026")
            page.get_by_role("button", name="Найти").click()
            page.wait_for_load_state("networkidle")
            task_pages = page.get_by_role("navigation", name="Страницы судебных заданий")
            next_tasks = task_pages.get_by_role("link", name="Следующие задания →")
            if next_tasks.count() != 1:
                raise AssertionError("More than 25 same-case tasks did not expose candidate pagination")
            next_tasks.click()
            page.wait_for_url("**tasks_page=2")
            older_act = (
                page.locator('section[aria-labelledby="add-package-document-heading"]')
                .locator("tbody tr")
                .filter(has_text="court-act-00-")
            )
            if older_act.count() != 1:
                raise AssertionError("The older same-case source act was inaccessible on candidate page 2")
            browser.close()

        result = {
            "synthetic_only": True,
            "chromium": "pass",
            "package_created_through_ui": True,
            "package_search": True,
            "explicit_task_add_remove": True,
            "stale_replay_rejected": True,
            "source_occurrence_filename_visible": True,
            "package_list_pagination": True,
            "candidate_pagination": True,
            "external_sro_accessed": False,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        stop_app(process)


if __name__ == "__main__":
    raise SystemExit(main())
