#!/usr/bin/env python3
"""Exercise the 100-file browser UI and a dropped response against the local app."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright
from redis import Redis
from rq import Queue, Worker
from sqlalchemy import select, func

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal
from app.db.models import Task, TaskStatus
from app.settings import settings

USER = "nightly-operator"
PASSWORD = "nightly-synthetic-password-only"


def wait_workers(processes: list[subprocess.Popen], count: int, timeout: float = 30) -> None:
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    queue = Queue("ocr", connection=redis)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("Isolated OCR worker exited before registration")
            if len(Worker.all(connection=redis, queue=queue)) >= count:
                return
            time.sleep(0.2)
    finally:
        redis.close()
    raise TimeoutError("Isolated OCR workers did not register")


def task_snapshot_after(max_id: int) -> list[dict]:
    with SessionLocal() as db:
        tasks = db.scalars(select(Task).where(Task.id > max_id).order_by(Task.id)).all()
        return [
            {
                "id": task.id,
                "filename": task.document.filename,
                "content_hash": task.document.content_hash,
                "has_source_item": task.intake_item is not None,
                "status": task.status,
                "recognized": task.recognized or {},
            }
            for task in tasks
        ]


def max_task_id() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)


def wait_terminal(max_id: int, expected_count: int, timeout: float = 600) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = task_snapshot_after(max_id)
        if len(rows) == expected_count and all(
            row["status"] in (TaskStatus.needs_review, TaskStatus.failed) for row in rows
        ):
            return rows
        time.sleep(0.25)
    raise TimeoutError("The isolated browser-intake tasks did not reach terminal OCR states")


def stop_workers(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
    for process in processes:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--workers", type=int, choices=(1, 2, 4, 8, 12), default=8)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    manifest_path = args.corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    docs = manifest["documents"]
    if len(docs) != 100 or len({doc["sha256"] for doc in docs}) != 100:
        raise SystemExit("Expected exactly 100 distinct synthetic PDFs")
    preflight_redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    preflight_queue = Queue("ocr", connection=preflight_redis)
    if preflight_queue.count or preflight_queue.started_job_registry.count or preflight_queue.failed_job_registry.count:
        preflight_redis.close()
        raise SystemExit("Refused: reconcile all isolated OCR queue registries before the browser run")
    preflight_redis.close()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_paths = [args.log_dir / f"ui-worker-{i + 1}.log" for i in range(args.workers)]
    handles = []
    processes = []
    request_file_counts: list[int] = []
    dropped = False
    started = time.monotonic()
    before_id = max_task_id()
    try:
        for log_path in log_paths:
            handles.append(log_path.open("wb"))
            processes.append(
                subprocess.Popen(
                    [sys.executable, "scripts/dev/run_worker.py", "ocr"],
                    cwd=ROOT,
                    env={**os.environ, "APP_DEBUG": "1"},
                    stdout=handles[-1],
                    stderr=subprocess.STDOUT,
                )
            )
        wait_workers(processes, args.workers)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            context.expose_function("recordUploadChunk", lambda count: request_file_counts.append(int(count)))
            context.add_init_script(
                """(() => {
                  const originalFetch = window.fetch.bind(window);
                  let uploads = 0;
                  window.fetch = async (...args) => {
                    const target = typeof args[0] === 'string' ? args[0] : args[0].url;
                    if (target.endsWith('/upload')) {
                      await window.recordUploadChunk(args[1].body.getAll('files').length);
                    }
                    const response = await originalFetch(...args);
                    if (target.endsWith('/upload') && ++uploads === 3 &&
                        window.localStorage.getItem('sro-nightly-drop-simulated') !== 'done') {
                      window.localStorage.setItem('sro-nightly-drop-simulated', 'done');
                      throw new TypeError('Simulated lost response after server acceptance');
                    }
                    return response;
                  };
                })();"""
            )
            page.goto(args.base_url + "/login", wait_until="domcontentloaded")
            page.locator("#login").fill(USER)
            page.locator("#password").fill(PASSWORD)
            page.get_by_role("button", name="Войти").click()
            page.wait_for_url(args.base_url + "/", timeout=15000)
            page.locator("#document-upload").wait_for(state="visible")
            page.set_input_files(
                '#document-upload input[type="file"]',
                [str(args.corpus / doc["filename"]) for doc in docs],
            )

            page.get_by_role("button", name="Загрузить").click()
            page.get_by_text("Ответ сервера не подтверждён", exact=False).wait_for(
                state="visible", timeout=60000
            )
            partial_message = page.locator("#document-upload-status").inner_text()
            dropped = "40 из 100" in partial_message
            if not dropped:
                raise AssertionError("The page did not expose the expected explicit partial/unknown status")

            first_rows = wait_terminal(before_id, 60, timeout=args.timeout_seconds)
            if len(first_rows) != 60:
                raise AssertionError("The browser's first three chunks were not persisted independently")

            retry_started = time.monotonic()
            page.close()
            page = context.new_page()
            page.goto(args.base_url + "/", wait_until="domcontentloaded")
            page.locator("#document-upload").wait_for(state="visible")
            page.set_input_files(
                '#document-upload input[type="file"]',
                [str(args.corpus / doc["filename"]) for doc in docs],
            )
            page.get_by_role("button", name="Загрузить").click()
            page.get_by_text("Принято 100 файлов", exact=False).wait_for(
                state="visible", timeout=60000
            )
            deadline = time.monotonic() + args.timeout_seconds
            while page.locator("table tbody tr").count() != 100:
                if time.monotonic() >= deadline:
                    raise TimeoutError("The task table did not show 100 cards after replay")
                page.wait_for_timeout(250)
            retry_to_cards = time.monotonic() - retry_started
            context.close()
            browser.close()

        rows = wait_terminal(before_id, 100, timeout=args.timeout_seconds)
        if len(rows) != 100 or len({row["filename"] for row in rows}) != 100:
            raise AssertionError("The browser UI did not create 100 separate source-linked tasks")
        if request_file_counts != [20] * 8:
            raise AssertionError(
                f"Expected eight 20-file requests including retry, got {request_file_counts}"
            )
        expected = {doc["filename"]: doc for doc in docs}
        if any(not row["has_source_item"] or row["content_hash"] != expected[row["filename"]]["sha256"] for row in rows):
            raise AssertionError("A UI task lost its source item or content hash")
        exact_fields = 0
        exact_documents = 0
        for row in rows:
            doc = expected[row["filename"]]
            fields = row["recognized"]
            matches = (
                (fields.get("case_number") or {}).get("value") == doc["case_number"],
                (fields.get("doc_date") or {}).get("value") == doc["doc_date"],
                (fields.get("fio_raw") or {}).get("value") == doc["fio"],
                (fields.get("inn") or {}).get("value") == doc["inn"],
            )
            exact_fields += sum(matches)
            exact_documents += all(matches)

        wall = time.monotonic() - started
        result_redis = Redis.from_url(settings.redis_url)
        queued_after_processing = Queue("ocr", connection=result_redis).count
        result_redis.close()
        summary = {
            "revision": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
            "corpus_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "worker_count": args.workers,
            "single_ui_selection_files": len(docs),
            "upload_requests_including_same_page_retry": len(request_file_counts),
            "files_per_request": request_file_counts,
            "simulated_lost_response": dropped,
            "confirmed_before_retry": 40,
            "tasks_created_after_retry": len(rows),
            "source_linked_tasks": sum(row["has_source_item"] for row in rows),
            "failed_tasks": sum(row["status"] == TaskStatus.failed for row in rows),
            "exact_expected_documents": exact_documents,
            "exact_expected_fields": exact_fields,
            "fields_total": 400,
            "package_wall_seconds": wall,
            "retry_to_100_cards_seconds": retry_to_cards,
            "queued_after_processing": queued_after_processing,
        }
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    finally:
        stop_workers(processes)
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
