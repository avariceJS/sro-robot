#!/usr/bin/env python3
"""Run simultaneous 100-file intakes from synthetic local operators."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, func
from sqlalchemy.orm import joinedload

from app.db.base import SessionLocal
from app.db.models import Task, TaskStatus, User
from app.services.auth import create_user
from app.settings import settings

from measure_http_batch import (
    PASSWORD,
    USER,
    login,
    multipart,
    post_form,
    wait_for_workers,
    stop_workers,
)
from redis import Redis
from rq import Queue
from rq.registry import DeferredJobRegistry, FailedJobRegistry, ScheduledJobRegistry, StartedJobRegistry


def ensure_operator(login: str, password: str, display_name: str) -> int:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.login == login))
        if user is None:
            user = create_user(db, login, password, display_name)
            db.commit()
        return user.id


def task_max_id() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)


def queue_empty() -> bool:
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    queue = Queue("ocr", connection=redis)
    try:
        registry_types = (StartedJobRegistry, ScheduledJobRegistry, DeferredJobRegistry, FailedJobRegistry)
        return not queue.count and all(
            registry_type(queue.name, connection=redis, queue=queue).count == 0
            for registry_type in registry_types
        )
    finally:
        redis.close()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081")
    parser.add_argument("--workers", type=int, choices=(4, 8, 12), default=8)
    parser.add_argument("--operators", type=int, choices=(2, 5, 10, 20), default=2)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--log-dir", required=True, type=Path)
    args = parser.parse_args()
    manifest_path = args.corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    docs = manifest["documents"]
    if len(docs) != 100 or len({doc["sha256"] for doc in docs}) != 100:
        raise SystemExit("Expected exactly 100 distinct synthetic PDFs")
    if not queue_empty():
        raise SystemExit("Isolated OCR queue is not empty; reconcile before concurrent upload test")
    with SessionLocal() as db:
        first_user_id = db.scalar(select(User.id).where(User.login == USER))
    if first_user_id is None:
        raise RuntimeError("Primary synthetic operator is missing from the isolated DB")
    operators = [(USER, PASSWORD, first_user_id)]
    for ordinal in range(2, args.operators + 1):
        login_name = f"nightly-concurrent-{ordinal:02d}"
        password = f"nightly-concurrent-synthetic-password-{ordinal:02d}"
        user_id = ensure_operator(
            login_name,
            password,
            f"Synthetic Nightly Operator {ordinal}",
        )
        operators.append((login_name, password, user_id))
    args.log_dir.mkdir(parents=True, exist_ok=True)
    processes = []
    handles = []
    pids = []
    start_barrier = threading.Barrier(args.operators)
    before_id = task_max_id()
    started = time.monotonic()
    try:
        for index in range(args.workers):
            handle = (args.log_dir / f"{args.run_id}-worker-{index + 1}.log").open("wb")
            handles.append(handle)
            process = subprocess.Popen(
                [sys.executable, "scripts/dev/run_worker.py", "ocr"],
                cwd=ROOT, env={**os.environ, "APP_DEBUG": "1"},
                stdout=handle, stderr=subprocess.STDOUT,
            )
            processes.append(process)
            pids.append(process.pid)
        wait_for_workers(processes, args.workers)

        # Authenticate serially so the app's deliberately strict login flood
        # guard is exercised as configured; the measured burst begins at intake.
        authenticated = [
            (login(args.base_url, operator_login, password), operator_login, operator_id)
            for operator_login, password, operator_id in operators
        ]

        def upload(opener, operator_login: str, operator_id: int) -> dict:
            start_barrier.wait(timeout=10)
            durations = []
            for chunk_index, start in enumerate(range(0, len(docs), 20)):
                key = hashlib.sha256(
                    f"{args.run_id}:{operator_id}:{chunk_index}".encode()
                ).hexdigest()[:48]
                body, content_type = multipart(docs[start : start + 20], args.corpus, key)
                request_started = time.monotonic()
                status = post_form(opener, args.base_url + "/upload", body, content_type)
                durations.append(time.monotonic() - request_started)
                if status != 200:
                    raise RuntimeError(f"Operator {operator_login} chunk returned HTTP {status}")
            return {"operator": operator_login, "user_id": operator_id, "chunk_seconds": durations}

        with ThreadPoolExecutor(max_workers=args.operators) as pool:
            futures = [pool.submit(upload, *session) for session in authenticated]
            uploads = [future.result() for future in futures]

        expected = {doc["filename"]: doc for doc in docs}
        expected_tasks = args.operators * len(docs)
        deadline = time.monotonic() + 1200
        rows = []
        peak_rss = 0
        cpu_samples = []
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                rows = db.scalars(
                    select(Task).options(joinedload(Task.document), joinedload(Task.intake_item))
                    .where(Task.id > before_id).order_by(Task.id)
                ).unique().all()
                snapshot = [
                    {
                        "id": task.id,
                        "status": task.status.value,
                        "filename": task.document.filename,
                        "received_by_id": task.intake_item.received_by_id if task.intake_item else None,
                        "intake_item_id": task.intake_item.id if task.intake_item else None,
                        "recognized": task.recognized or {},
                    }
                    for task in rows
                ]
            if len(snapshot) == expected_tasks and all(row["status"] in {TaskStatus.needs_review.value, TaskStatus.failed.value} for row in snapshot):
                break
            from measure_http_batch import process_sample

            cpu, rss = process_sample(pids)
            cpu_samples.append(cpu)
            peak_rss = max(peak_rss, rss)
            time.sleep(0.25)
        if len(snapshot) != expected_tasks:
            raise AssertionError(f"Expected {expected_tasks} tasks after concurrent uploads; got {len(snapshot)}")
        user_ids = {upload["user_id"] for upload in uploads}
        if any(row["received_by_id"] not in user_ids for row in snapshot):
            raise AssertionError("Concurrent upload tasks were attributed to the wrong operator")
        attribution_counts = Counter(row["received_by_id"] for row in snapshot)
        expected_attribution = Counter({operator[2]: len(docs) for operator in operators})
        if attribution_counts != expected_attribution:
            raise AssertionError(f"Each operator should own {len(docs)} source occurrences; got {attribution_counts}")
        if len({row["intake_item_id"] for row in snapshot}) != expected_tasks:
            raise AssertionError("Concurrent upload source occurrences were not unique")
        if any(not row["filename"] in expected for row in snapshot):
            raise AssertionError("Concurrent upload produced a filename outside the synthetic manifest")
        exact_fields = exact_docs = 0
        for row in snapshot:
            doc = expected[row["filename"]]
            fields = row["recognized"]
            matches = (
                (fields.get("case_number") or {}).get("value") == doc["case_number"],
                (fields.get("doc_date") or {}).get("value") == doc["doc_date"],
                (fields.get("fio_raw") or {}).get("value") == doc["fio"],
                (fields.get("inn") or {}).get("value") == doc["inn"],
            )
            exact_fields += sum(matches)
            exact_docs += all(matches)
        if not queue_empty():
            raise AssertionError("OCR queue registries were not empty after concurrent completion")
        wall = time.monotonic() - started
        summary = {
            "run_id": args.run_id,
            "revision": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
            "corpus_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "operators": [{"login": upload["operator"], "user_id": upload["user_id"]} for upload in uploads],
            "simultaneous_upload_count": len(operators),
            "login_setup": "sequential; concurrent burst starts at upload intake to retain the login flood guard",
            "files_per_operator": 100,
            "http_upload_requests": len(operators) * 5,
            "request_files_each": 20,
            "tasks": len(snapshot),
            "unique_source_items": len({row["intake_item_id"] for row in snapshot}),
            "source_items_per_operator": dict(attribution_counts),
            "failed_tasks": sum(row["status"] == TaskStatus.failed.value for row in snapshot),
            "extraction_exact_documents": exact_docs,
            "extraction_exact_fields": exact_fields,
            "extraction_fields_total": len(snapshot) * 4,
            "package_wall_seconds": wall,
            "docs_per_minute": len(snapshot) / wall * 60,
            "peak_sampled_workers_rss_mib_total": round(peak_rss / (1024 * 1024), 2),
            "average_sampled_workers_cpu_percent_total": round(sum(cpu_samples) / len(cpu_samples), 2) if cpu_samples else None,
            "queue_empty_after_completion": True,
            "uploads": uploads,
        }
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    finally:
        stop_workers(processes)
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
