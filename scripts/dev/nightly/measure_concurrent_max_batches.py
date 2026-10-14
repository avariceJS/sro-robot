#!/usr/bin/env python3
"""Measure two simultaneous exact-limit synthetic uploads on the isolated app."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from redis import Redis
from rq import Queue
from rq.registry import DeferredJobRegistry, FailedJobRegistry, ScheduledJobRegistry, StartedJobRegistry
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.db.base import SessionLocal
from app.db.models import JobOutbox, Task, TaskStatus
from app.settings import settings
from measure_http_batch import (
    PASSWORD,
    USER,
    login,
    multipart,
    post_form,
    process_sample,
    stop_workers,
    wait_for_workers,
)
from measure_max_batch_memory import MAX_BATCH_BYTES, MAX_FILE_BYTES, app_pid, make_limit_corpus

SECOND_USER = "nightly-second-operator"
SECOND_PASSWORD = "nightly-second-synthetic-password-only"


def verify_isolated_endpoints() -> None:
    database = settings.database_url
    redis_url = settings.redis_url
    from sqlalchemy.engine import make_url
    from urllib.parse import urlsplit

    db = make_url(database)
    redis = urlsplit(redis_url)
    if (db.host, db.port, db.database) != ("127.0.0.1", 55442, "sro_nightly"):
        raise SystemExit("Refusing concurrent max-size run outside the dedicated nightly PostgreSQL")
    if (redis.hostname, redis.port) != ("127.0.0.1", 56389):
        raise SystemExit("Refusing concurrent max-size run outside the dedicated nightly Redis")
    if not settings.debug:
        raise SystemExit("Refusing: the local nightly app must run with debug/test settings")


def require_clean_queue() -> None:
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        queue = Queue("ocr", connection=client)
        registries = (
            StartedJobRegistry("ocr", connection=client),
            FailedJobRegistry("ocr", connection=client),
            DeferredJobRegistry("ocr", connection=client),
            ScheduledJobRegistry("ocr", connection=client),
        )
        if queue.count or any(registry.count for registry in registries):
            raise SystemExit("Refusing: reconcile the isolated OCR queue before this measurement")
    finally:
        client.close()
    with SessionLocal() as db:
        nonterminal = db.scalar(
            select(func.count()).select_from(Task).where(
                Task.status.in_((TaskStatus.new, TaskStatus.recognizing))
            )
        )
        pending_outbox = db.scalar(select(func.count()).select_from(JobOutbox))
        if nonterminal or pending_outbox:
            raise SystemExit("Refusing: reconcile isolated nonterminal tasks and outbox before this measurement")


def task_max_id() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-corpus", required=True, type=Path)
    parser.add_argument("--corpus-output", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--log-dir", required=True, type=Path)
    args = parser.parse_args()

    verify_isolated_endpoints()
    from urllib.parse import urlsplit

    app_url = urlsplit(args.base_url)
    if (app_url.scheme, app_url.hostname, app_url.port) != ("http", "127.0.0.1", 18081):
        raise SystemExit("Refusing: the test app URL must be http://127.0.0.1:18081")
    require_clean_queue()

    group_a = make_limit_corpus(
        args.source_corpus,
        args.corpus_output / "operator-a",
        args.run_id + "-operator-a",
        selection_offset=0,
    )
    group_b = make_limit_corpus(
        args.source_corpus,
        args.corpus_output / "operator-b",
        args.run_id + "-operator-b",
        selection_offset=4,
    )
    if len({item["sha256"] for item in group_a + group_b}) != 8:
        raise AssertionError("The two exact-limit upload batches must contain eight unique PDFs")
    operators = [
        (USER, PASSWORD, group_a),
        (SECOND_USER, SECOND_PASSWORD, group_b),
    ]
    uploads = []
    for ordinal, (operator, password, items) in enumerate(operators):
        key = hashlib.sha256(f"{args.run_id}:{ordinal}".encode()).hexdigest()[:48]
        body, content_type = multipart(items, items[0]["path"].parent, key)
        if len(body) > settings.upload_max_request_bytes:
            raise AssertionError("An exact-limit multipart body exceeded the configured HTTP request limit")
        uploads.append({"operator": operator, "items": items, "key": key, "body": body, "content_type": content_type})
    if any(sum(item["bytes"] for item in upload["items"]) != MAX_BATCH_BYTES for upload in uploads):
        raise AssertionError("Each request must contain exactly the configured 100 MiB file limit")

    sessions = [login(args.base_url, operator, password) for operator, password, _items in operators]
    before_id = task_max_id()
    web_pid = app_pid(args.base_url)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    worker_logs = [args.log_dir / f"worker-{index + 1}.log" for index in range(2)]
    processes: list[subprocess.Popen] = []
    handles = []
    samples: list[tuple[float, int]] = []
    sample_stop = threading.Event()
    peak_client_rss = 0
    start_barrier = threading.Barrier(2)

    def sampler() -> None:
        while not sample_stop.is_set():
            samples.append(process_sample([web_pid, *(process.pid for process in processes)]))
            sample_stop.wait(0.05)

    try:
        for path in worker_logs:
            handle = path.open("wb")
            handles.append(handle)
            processes.append(
                subprocess.Popen(
                    [sys.executable, "scripts/dev/run_worker.py", "ocr"],
                    cwd=ROOT,
                    env={**os.environ, "APP_DEBUG": "1"},
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )
            )
        wait_for_workers(processes, expected=2)
        sample_thread = threading.Thread(target=sampler, daemon=True)
        sample_thread.start()
        started = time.monotonic()

        def send(upload: dict, session) -> dict:
            start_barrier.wait(timeout=10)
            request_started = time.monotonic()
            status = post_form(
                session,
                args.base_url + "/upload",
                upload["body"],
                upload["content_type"],
            )
            return {"operator": upload["operator"], "status": status, "seconds": time.monotonic() - request_started}

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda pair: send(*pair), zip(uploads, sessions)))
        upload_finished = time.monotonic()
        if [item["status"] for item in responses] != [200, 200]:
            raise RuntimeError(f"Concurrent exact-limit requests returned statuses {responses}")

        deadline = time.monotonic() + 300
        snapshot = []
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                rows = db.scalars(
                    select(Task)
                    .options(joinedload(Task.document), joinedload(Task.intake_item), joinedload(Task.intake_operation))
                    .where(Task.id > before_id)
                    .order_by(Task.id)
                ).unique().all()
                snapshot = [
                    {
                        "status": row.status.value,
                        "hash": row.document.content_hash,
                        "filename": row.document.filename,
                        "source_item_id": row.intake_item.id if row.intake_item else None,
                        "operation_id": row.intake_operation_id,
                        "user_id": row.intake_operation.user_id if row.intake_operation else None,
                        "recognized": row.recognized or {},
                    }
                    for row in rows
                ]
            if len(snapshot) == 8 and all(
                item["status"] in (TaskStatus.needs_review.value, TaskStatus.failed.value)
                for item in snapshot
            ):
                break
            time.sleep(0.1)
        finished = time.monotonic()
        sample_stop.set()
        sample_thread.join(timeout=5)

        if len(snapshot) != 8 or any(item["status"] != TaskStatus.needs_review.value for item in snapshot):
            raise AssertionError("Both valid exact-limit batches must produce eight review cards")
        if len({item["hash"] for item in snapshot}) != 8 or any(item["source_item_id"] is None for item in snapshot):
            raise AssertionError("Concurrent max-size task/source mapping is not one-to-one")
        by_operation: dict[int, list[dict]] = {}
        for item in snapshot:
            by_operation.setdefault(item["operation_id"], []).append(item)
        if len(by_operation) != 2 or sorted(map(len, by_operation.values())) != [4, 4]:
            raise AssertionError("Expected two independently attributed four-file intake operations")
        if {len({item["user_id"] for item in batch}) for batch in by_operation.values()} != {1}:
            raise AssertionError("Tasks in one intake operation have mixed user attribution")
        distinct_operators = len({item["user_id"] for item in snapshot})
        if distinct_operators != 2:
            raise AssertionError("The simultaneous requests must belong to two different local test users")

        expected = {item["filename"]: item for upload in uploads for item in upload["items"]}
        field_matches = {name: 0 for name in ("case_number", "doc_date", "fio", "inn")}
        exact_documents = 0
        for item in snapshot:
            source = expected[item["filename"]]
            recognized = item["recognized"]
            actual = {
                "case_number": (recognized.get("case_number") or {}).get("value"),
                "doc_date": (recognized.get("doc_date") or {}).get("value"),
                "fio": (recognized.get("fio_raw") or {}).get("value"),
                "inn": (recognized.get("inn") or {}).get("value"),
            }
            matches = {name: actual[name] == source[name] for name in field_matches}
            for name, matches_expected in matches.items():
                field_matches[name] += matches_expected
            exact_documents += all(matches.values())

        redis_client = Redis.from_url(settings.redis_url)
        try:
            queue = Queue("ocr", connection=redis_client)
            registries_empty = not any(
                registry.count
                for registry in (
                    queue.started_job_registry,
                    queue.failed_job_registry,
                    DeferredJobRegistry("ocr", connection=redis_client),
                    ScheduledJobRegistry("ocr", connection=redis_client),
                )
            )
            queued_after = queue.count
        finally:
            redis_client.close()
        if queued_after or not registries_empty:
            raise AssertionError("Concurrent exact-limit workload did not fully drain the OCR queue")
        with SessionLocal() as db:
            outbox_after = db.scalar(select(func.count()).select_from(JobOutbox))
        if outbox_after:
            raise AssertionError("Concurrent exact-limit workload left a pending outbox row")
        output = {
            "revision": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
            "corpus_manifest_sha256": hashlib.sha256((args.source_corpus / "manifest.json").read_bytes()).hexdigest(),
            "worker_count": len(processes),
            "concurrent_uploads": 2,
            "files_per_upload": 4,
            "distinct_source_hashes": len({item["hash"] for item in snapshot}),
            "source_linked_tasks": sum(item["source_item_id"] is not None for item in snapshot),
            "intake_operations": len(by_operation),
            "distinct_operators": distinct_operators,
            "tasks_by_operator": sorted(len(batch) for batch in by_operation.values()),
            "statuses": {status: sum(item["status"] == status for item in snapshot) for status in (TaskStatus.needs_review.value, TaskStatus.failed.value)},
            "exact_documents": exact_documents,
            "field_matches": field_matches,
            "field_total": 32,
            "per_file_bytes": MAX_FILE_BYTES,
            "per_request_file_bytes": [sum(item["bytes"] for item in upload["items"]) for upload in uploads],
            "per_request_multipart_bytes": [len(upload["body"]) for upload in uploads],
            "configured_http_request_bytes": settings.upload_max_request_bytes,
            "request_latencies_seconds": [round(item["seconds"], 3) for item in responses],
            "parallel_request_wall_seconds": round(upload_finished - started, 3),
            "package_wall_seconds": round(finished - started, 3),
            "peak_app_and_worker_tree_rss_mib": round(max((item[1] for item in samples), default=0) / (1024 * 1024), 2),
            "mean_sampled_app_and_worker_cpu_percent": round(sum(item[0] for item in samples) / len(samples), 2) if samples else None,
            "queue_empty_after_completion": queued_after == 0 and registries_empty,
            "pending_outbox_after_completion": outbox_after,
            "input_hashes": sorted(item["hash"] for item in snapshot),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(output, sort_keys=True))
        return 0
    finally:
        sample_stop.set()
        if processes:
            stop_workers(processes)
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
