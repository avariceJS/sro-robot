#!/usr/bin/env python3
"""Measure a 100-document queue backlog before starting local OCR workers."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from redis import Redis
from rq import Queue, Worker
from rq.registry import DeferredJobRegistry, FailedJobRegistry, ScheduledJobRegistry, StartedJobRegistry
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import joinedload

from app.db.base import SessionLocal
from app.db.models import JobOutbox, Task, TaskStatus, User
from app.settings import settings
from measure_http_batch import USER, PASSWORD, login, multipart, post_form, process_sample, stop_workers, wait_for_workers

POSTGRES_IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
REDIS_IMAGE = "redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"
REGISTRIES = (StartedJobRegistry, ScheduledJobRegistry, DeferredJobRegistry, FailedJobRegistry)


def verify_stack() -> None:
    pg, redis_url = make_url(settings.database_url), make_url(settings.redis_url)
    if (pg.host, pg.port, pg.database) != ("127.0.0.1", 55442, "sro_nightly"):
        raise SystemExit("Refusing to run outside the dedicated nightly PostgreSQL endpoint")
    if (redis_url.host, redis_url.port) != ("127.0.0.1", 56389):
        raise SystemExit("Refusing to run outside the dedicated nightly Redis endpoint")
    expected = {
        "sro-nightly-20261014-db-1": (POSTGRES_IMAGE, "5432/tcp", "127.0.0.1:55442"),
        "sro-nightly-20261014-redis-1": (REDIS_IMAGE, "6379/tcp", "127.0.0.1:56389"),
    }
    for name, (image, port, host_port) in expected.items():
        state = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}|{{.Config.Image}}", name], capture_output=True, text=True, check=False)
        published = subprocess.run(["docker", "port", name, port], capture_output=True, text=True, check=False)
        if state.returncode or state.stdout.strip() != f"true|{image}" or host_port not in published.stdout.splitlines():
            raise SystemExit("Refusing to run: dedicated nightly service identity is not verified")


def snapshot() -> tuple[list[Task], dict[str, int], list[Worker]]:
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=2)
    try:
        queue = Queue("ocr", connection=client)
        counts = {"queued": queue.count}
        for registry_type in REGISTRIES:
            counts[registry_type.__name__] = registry_type(queue.name, connection=client, queue=queue).count
        workers = Worker.all(connection=client, queue=queue)
    finally:
        client.close()
    with SessionLocal() as db:
        rows = list(db.scalars(select(Task).options(joinedload(Task.document), joinedload(Task.intake_item))
                               .order_by(Task.id)).unique())
    return rows, counts, workers


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--workers", type=int, choices=(1, 2, 4, 8), default=8)
    args = parser.parse_args()
    verify_stack()
    manifest_bytes = (args.corpus / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    docs = manifest.get("documents", [])
    if len(docs) != 100 or len({doc["sha256"] for doc in docs}) != 100:
        raise SystemExit("Expected exactly 100 distinct generated synthetic PDFs")

    _, queue_before, workers_before = snapshot()
    if queue_before != {"queued": 0, "StartedJobRegistry": 0, "ScheduledJobRegistry": 0,
                        "DeferredJobRegistry": 0, "FailedJobRegistry": 0} or workers_before:
        raise SystemExit("Reconcile isolated OCR queue/workers before the queue-depth check")
    with SessionLocal() as db:
        pending_before = int(db.scalar(select(func.count()).select_from(JobOutbox)) or 0)
        before_id = int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)
        # Resolve the synthetic intake account via the known synthetic login without exposing credentials.
        user_id = db.scalar(select(User.id).where(User.login == USER))
    if pending_before or user_id is None:
        raise SystemExit("Expected zero outbox intents and the known synthetic HTTP operator")

    opener = login(args.base_url, USER, PASSWORD)
    started = time.monotonic()
    for chunk_index, begin in enumerate(range(0, 100, 20)):
        chunk = docs[begin : begin + 20]
        key = hashlib.sha256(f"{args.run_id}:{user_id}:{chunk_index}".encode()).hexdigest()[:48]
        body, content_type = multipart(chunk, args.corpus, key)
        status = post_form(opener, args.base_url + "/upload", body, content_type)
        if status != 200:
            raise AssertionError(f"Synthetic HTTP chunk {chunk_index} returned {status}")

    rows_queued, queue_queued, workers_queued = snapshot()
    new_rows = [row for row in rows_queued if row.id > before_id]
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=2)
    try:
        queued_jobs = Queue("ocr", connection=client).get_jobs(offset=0, length=-1)
        queued_task_ids = sorted(job.args[0] for job in queued_jobs
                                 if job.func_name == "app.workers.jobs.recognize" and len(job.args) == 1)
    finally:
        client.close()
    with SessionLocal() as db:
        pending_after_accept = int(db.scalar(select(func.count()).select_from(JobOutbox)) or 0)
    if len(new_rows) != 100 or any(row.status != TaskStatus.new for row in new_rows):
        raise AssertionError("Before workers start, expected exactly 100 new accepted tasks")
    by_filename = {doc["filename"]: doc for doc in docs}
    if len({row.intake_item.id for row in new_rows if row.intake_item is not None}) != 100:
        raise AssertionError("The 100 accepted tasks must each have a distinct source item")
    if any(row.intake_item is None or row.document.content_hash != by_filename[row.document.filename]["sha256"]
           for row in new_rows):
        raise AssertionError("An accepted task lost or changed its source document")
    expected_task_ids = sorted(row.id for row in new_rows)
    if queue_queued != {"queued": 100, "StartedJobRegistry": 0, "ScheduledJobRegistry": 0,
                        "DeferredJobRegistry": 0, "FailedJobRegistry": 0} or workers_queued:
        raise AssertionError(f"Expected exactly 100 queued jobs and no worker before OCR; got {queue_queued}")
    if queued_task_ids != expected_task_ids or pending_after_accept != 0:
        raise AssertionError("Queue contents, task IDs or durable outbox did not match the 100 accepted sources")

    args.log_dir.mkdir(parents=True, exist_ok=True)
    handles = []
    processes = []
    pids = []
    per_task_latency: list[float] = []
    cpu_samples = []
    peak_rss = 0
    try:
        for index in range(args.workers):
            handle = (args.log_dir / f"{args.run_id}-worker-{index + 1}.log").open("wb")
            handles.append(handle)
            process = subprocess.Popen([sys.executable, "scripts/dev/run_worker.py", "ocr"], cwd=ROOT,
                                       env={**os.environ, "APP_DEBUG": "1"}, stdout=handle, stderr=subprocess.STDOUT)
            processes.append(process)
            pids.append(process.pid)
        wait_for_workers(processes, args.workers)
        completed = []
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                completed = list(db.scalars(select(Task).options(joinedload(Task.document), joinedload(Task.intake_item))
                                            .where(Task.id > before_id).order_by(Task.id)).unique())
            if len(completed) == 100 and all(row.status in (TaskStatus.needs_review, TaskStatus.failed) for row in completed):
                break
            cpu, rss = process_sample(pids)
            cpu_samples.append(cpu)
            peak_rss = max(peak_rss, rss)
            time.sleep(0.1)
        else:
            raise TimeoutError("100 accepted synthetic tasks did not reach review/error state")
        if len(completed) != 100 or any(row.status != TaskStatus.needs_review for row in completed):
            raise AssertionError("All 100 valid synthetic documents should reach needs_review")
        for row in completed:
            age = (row.updated_at - row.created_at).total_seconds()
            per_task_latency.append(age)
        queue_final = snapshot()[1]
        if any(queue_final.values()):
            raise AssertionError(f"OCR queue did not drain after processing: {queue_final}")
    finally:
        stop_workers(processes)
        for handle in handles:
            handle.close()

    exact_docs = exact_fields = 0
    for row in completed:
        expected = by_filename[row.document.filename]
        fields = row.recognized or {}
        matches = [
            (fields.get("case_number") or {}).get("value") == expected["case_number"],
            (fields.get("doc_date") or {}).get("value") == expected["doc_date"],
            (fields.get("fio_raw") or {}).get("value") == expected["fio"],
            (fields.get("inn") or {}).get("value") == expected["inn"],
        ]
        exact_fields += sum(matches)
        exact_docs += all(matches)
    ordered_latency = sorted(per_task_latency)
    p95 = ordered_latency[math.ceil(0.95 * len(ordered_latency)) - 1]
    wall = time.monotonic() - started
    result = {
        "run_id": args.run_id,
        "revision": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
        "corpus_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "accepted_files": 100,
        "http_requests": 5,
        "accepted_before_workers": 100,
        "rq_jobs_queued_before_workers": len(queued_task_ids),
        "durable_outbox_after_acceptance": pending_after_accept,
        "new_task_rows_before_workers": len(new_rows),
        "source_links_before_workers": len({row.intake_item.id for row in new_rows}),
        "ocr_workers": args.workers,
        "review_cards_after_workers": len(completed),
        "failed_tasks": 0,
        "exact_documents": exact_docs,
        "exact_fields": exact_fields,
        "fields_total": 400,
        "total_wall_seconds": round(wall, 3),
        "documents_per_minute": round(100 / wall * 60, 2),
        "task_latency_p50_seconds": round(statistics.median(per_task_latency), 3),
        "task_latency_p95_seconds": round(p95, 3),
        "peak_worker_tree_rss_mib": round(peak_rss / (1024 * 1024), 2),
        "average_worker_tree_cpu_percent": round(sum(cpu_samples) / len(cpu_samples), 2) if cpu_samples else None,
        "queue_empty_after_processing": not any(queue_final.values()),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
