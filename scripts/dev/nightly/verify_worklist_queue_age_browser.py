#!/usr/bin/env python3
"""Verify the queued-wait indicator through a synthetic Chromium upload.

Requires the explicitly empty environment file and loopback benchmark services
used by ``bulk_browser_e2e.py``. No OCR worker should be running: the accepted
synthetic task must remain queued while the browser renders and polls the list.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright
from redis import Redis
from rq import Queue
from rq.job import Job
from sqlalchemy import select
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal
from app.db.models import Task, TaskStatus
from app.settings import settings
from scripts.dev.bulk_benchmark import make_signed_cookie, require_empty_queue, require_safe_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18084")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, required=True)
    args = parser.parse_args()

    require_safe_runtime(args.base_url)
    require_empty_queue()
    with TemporaryDirectory(prefix="sro-queue-age-") as temporary:
        pdf_path = Path(temporary) / "synthetic-queued-task.pdf"
        pdf = canvas.Canvas(str(pdf_path), invariant=1)
        pdf.drawString(52, 740, "СИНТЕТИЧЕСКИЙ ДОКУМЕНТ ДЛЯ ЛОКАЛЬНОЙ ПРОВЕРКИ")
        pdf.drawString(52, 706, "Дело А99-12345/2099")
        pdf.drawString(52, 672, "Должник: Учебный Иван Иванович")
        pdf.save()

        cookie, _user_id = make_signed_cookie()
        base = args.base_url.rstrip("/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(accept_downloads=True)
                context.add_cookies([{"name": "session", "value": cookie, "url": base}])
                page = context.new_page()
                page.goto(base + "/debtors/intake", wait_until="networkidle")
                page.locator('input[name="files"]').set_input_files(str(pdf_path))
                page.get_by_role("button", name="Загрузить").click()
                page.wait_for_url(re.compile(r"/debtors/intake\?intake=[1-9][0-9]*$"), timeout=30_000)

                dashboard = page.locator("#task-dashboard")
                age_message = dashboard.get_by_text(
                    "Самое старое задание ожидает обработки", exact=False
                )
                age_message.wait_for(state="visible", timeout=10_000)
                queued_count = dashboard.locator("dl.bulk-counts div").filter(
                    has_text="В очереди"
                ).locator("dd").inner_text()
                if queued_count != "1":
                    raise AssertionError("Expected one queued task in the intake dashboard")
                # Let the page's existing two-second poll refresh the fragment.
                with page.expect_response(lambda response: "/tasks/status-list" in response.url) as response_info:
                    page.wait_for_timeout(2_500)
                poll_response = response_info.value
                if (
                    poll_response.status != 200
                    or "Самое старое задание ожидает обработки" not in poll_response.text()
                    or not age_message.is_visible()
                ):
                    raise AssertionError("The polled dashboard fragment omitted the queued-age indicator")
                age_text = age_message.inner_text()
                if ".." in age_text:
                    raise AssertionError("The queued-age indicator contains duplicate punctuation")
                page.screenshot(path=str(args.screenshot), full_page=True)
                operation_id = int(parse_qs(urlsplit(page.url).query)["intake"][0])
            finally:
                browser.close()

    redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        queue = Queue("ocr", connection=redis_client)
        queued_ids = queue.get_job_ids()
        queued_jobs = queue.count
        jobs = Job.fetch_many(queued_ids, connection=redis_client)
    finally:
        redis_client.close()
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.intake_operation_id == operation_id))
        if task is None or task.status != TaskStatus.new:
            raise AssertionError("The synthetic intake task did not remain in the queued state")
        task_id = task.id

    if queued_jobs != 1 or len(jobs) != 1 or jobs[0] is None:
        raise AssertionError("Expected exactly one readable Redis OCR job for the queued task")
    if jobs[0].func_name != "app.workers.jobs.recognize" or jobs[0].args != (task_id,):
        raise AssertionError("The queued OCR delivery does not match the accepted task")
    rendered = {
        "synthetic_only": True,
        "operation_id": operation_id,
        "queued_task_count": int(queued_count),
        "task_status": TaskStatus.new.value,
        "redis_ocr_queue_count": queued_jobs,
        "oldest_queued_age_indicator_visible": True,
        "oldest_queued_age_text": age_text,
        "poll_fragment_refreshed": True,
        "external_sro_accessed": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rendered, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rendered))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
