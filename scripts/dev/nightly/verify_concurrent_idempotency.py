#!/usr/bin/env python3
"""Race two identical upload requests and verify one operation, task and RQ job."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload
from redis import Redis
from rq import Queue, Worker

from app.db.base import SessionLocal
from app.db.models import Task, TaskStatus
from app.settings import settings
from measure_http_batch import USER, PASSWORD, login, multipart, post_form, wait_for_workers, stop_workers


def queue_snapshot() -> dict[str, int]:
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        queue = Queue("ocr", connection=redis)
        return {
            "queued": queue.count,
            "started": queue.started_job_registry.count,
            "failed": queue.failed_job_registry.count,
        }
    finally:
        redis.close()


def task_max_id() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)


def task_snapshot(after_id: int) -> list[dict]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(Task)
            .options(joinedload(Task.document), joinedload(Task.intake_item))
            .where(Task.id > after_id)
            .order_by(Task.id)
        ).unique().all()
        return [
            {
                "id": row.id,
                "status": row.status.value,
                "filename": row.document.filename,
                "content_sha256": row.document.content_hash,
                "source_item_id": row.intake_item.id if row.intake_item else None,
                "received_by_id": row.intake_item.received_by_id if row.intake_item else None,
            }
            for row in rows
        ]


def delete_queued_task_job(task_id: int) -> None:
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        queue = Queue("ocr", connection=redis)
        matches = [job for job in queue.get_jobs(offset=0, length=-1) if tuple(job.args) == (task_id,)]
        if len(matches) != 1:
            raise AssertionError(f"Expected one queued synthetic OCR job for task {task_id}")
        matches[0].delete()
    finally:
        redis.close()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--log-dir", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))
    doc = manifest["documents"][0]
    if queue_snapshot() != {"queued": 0, "started": 0, "failed": 0}:
        raise SystemExit("Isolated OCR queue is not empty; reconcile before the idempotency race")

    before_id = task_max_id()
    key = hashlib.sha256(f"concurrent-idempotency:{args.run_id}".encode()).hexdigest()[:48]
    body, content_type = multipart([doc], args.corpus, key)
    barrier = threading.Barrier(2)
    openers = [login(args.base_url, USER, PASSWORD) for _ in range(2)]

    def submit(opener) -> int:
        barrier.wait(timeout=10)
        return post_form(opener, args.base_url + "/upload", body, content_type)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, opener) for opener in openers]
        statuses = [future.result(timeout=30) for future in futures]

    # A concurrent dispatcher may report an uncertain 503 while the other
    # request owns the outbox lease. Same-key retry reconciles the durable row.
    if any(status not in (200, 503) for status in statuses):
        raise AssertionError(f"Concurrent same-key upload returned unexpected HTTP statuses: {statuses}")
    tasks = task_snapshot(before_id)
    if len(tasks) != 1:
        raise AssertionError(f"Concurrent idempotency race created {len(tasks)} tasks; expected one")
    if tasks[0]["filename"] != doc["filename"] or tasks[0]["content_sha256"] != doc["sha256"]:
        raise AssertionError("Concurrent idempotency race linked the wrong source document")
    source_item = tasks[0]["source_item_id"]
    if source_item is None:
        raise AssertionError("Concurrent idempotency race lost its source item")
    queue_before_replay = queue_snapshot()
    opener = login(args.base_url, USER, PASSWORD)
    replay_status = post_form(opener, args.base_url + "/upload", body, content_type)
    tasks_after_replay = task_snapshot(before_id)
    queue_after_replay = queue_snapshot()
    if replay_status not in (200, 503) or len(tasks_after_replay) != 1:
        raise AssertionError("Same-key replay did not preserve the single accepted task")
    if queue_after_replay != queue_before_replay:
        raise AssertionError("Same-key replay changed queue admissions after concurrent upload")
    if queue_after_replay != {"queued": 1, "started": 0, "failed": 0}:
        raise AssertionError(f"Expected exactly one OCR job after replay; observed {queue_after_replay}")

    # Model Redis losing a job after its outbox acknowledgement was committed.
    # The durable task is still new; same-key replay must recreate only its
    # missing queue admission without creating a second card.
    delete_queued_task_job(tasks[0]["id"])
    queue_after_job_loss = queue_snapshot()
    if queue_after_job_loss != {"queued": 0, "started": 0, "failed": 0}:
        raise AssertionError("Synthetic RQ job loss did not clear the queue")
    replay_after_job_loss = post_form(opener, args.base_url + "/upload", body, content_type)
    tasks_after_job_loss_replay = task_snapshot(before_id)
    queue_after_job_loss_replay = queue_snapshot()
    if replay_after_job_loss not in (200, 503) or len(tasks_after_job_loss_replay) != 1:
        raise AssertionError("Replay after queue loss did not preserve the single task")
    if queue_after_job_loss_replay != {"queued": 1, "started": 0, "failed": 0}:
        raise AssertionError("Replay after queue loss did not restore exactly one OCR job")

    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log_dir / f"{args.run_id}-worker.log"
    process = None
    with log_path.open("wb") as log_handle:
        process = subprocess.Popen(
            [sys.executable, "scripts/dev/run_worker.py", "ocr"],
            cwd=ROOT,
            env={**os.environ, "APP_DEBUG": "1"},
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_for_workers([process], 1)
            deadline = time.monotonic() + 180
            final = tasks_after_replay
            while time.monotonic() < deadline:
                final = task_snapshot(before_id)
                if len(final) == 1 and final[0]["status"] in (
                    TaskStatus.needs_review.value,
                    TaskStatus.failed.value,
                ):
                    break
                time.sleep(0.25)
            if len(final) != 1 or final[0]["status"] != TaskStatus.needs_review.value:
                raise AssertionError(f"Single accepted task did not reach review: {final}")
            if queue_snapshot() != {"queued": 0, "started": 0, "failed": 0}:
                raise AssertionError("OCR queue did not drain after processing the single task")
        finally:
            stop_workers([process])

    print(json.dumps({
        "run_id": args.run_id,
        "revision": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
        "corpus_manifest_sha256": hashlib.sha256((args.corpus / "manifest.json").read_bytes()).hexdigest(),
        "simultaneous_same_key_requests": 2,
        "http_statuses": statuses,
        "replay_status": replay_status,
        "tasks_after_race_and_replay": len(tasks_after_replay),
        "source_item_id": source_item,
        "queue_after_race": queue_before_replay,
        "queue_after_replay": queue_after_replay,
        "queue_after_simulated_job_loss": queue_after_job_loss,
        "replay_after_simulated_job_loss": replay_after_job_loss,
        "tasks_after_job_loss_replay": len(tasks_after_job_loss_replay),
        "queue_after_job_loss_replay": queue_after_job_loss_replay,
        "final_task_status": final[0]["status"],
        "queue_empty_after_worker": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
