#!/usr/bin/env python3
"""Race same-key upload replays with a real local OCR worker claim."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from redis import Redis
from rq import Queue
from rq.job import Job
from rq.registry import FinishedJobRegistry
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload
from sqlalchemy.engine import make_url

from app.db.base import SessionLocal
from app.db.models import Event, Task, TaskStatus
from app.settings import settings
from measure_http_batch import USER, PASSWORD, login, multipart, post_form, stop_workers, wait_for_workers

POSTGRES_IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
REDIS_IMAGE = "redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"


def verify_stack() -> None:
    pg = make_url(settings.database_url)
    redis_url = make_url(settings.redis_url)
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
            raise SystemExit("Refusing to run: isolated database/Redis container identity is not verified")


def queue_snapshot() -> dict[str, int]:
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=2)
    try:
        queue = Queue("ocr", connection=client)
        return {"queued": queue.count, "started": queue.started_job_registry.count,
                "failed": queue.failed_job_registry.count}
    finally:
        client.close()


def finished_task_job_ids(task_id: int) -> list[str]:
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=2)
    try:
        queue = Queue("ocr", connection=client)
        registry = FinishedJobRegistry(queue.name, connection=client, queue=queue)
        jobs = Job.fetch_many(registry.get_job_ids(cleanup=False), connection=client)
        return sorted(job.id for job in jobs
                      if job is not None and job.func_name == "app.workers.jobs.recognize"
                      and job.args == (task_id,))
    finally:
        client.close()


def task_rows(after_id: int) -> list[Task]:
    with SessionLocal() as db:
        return list(db.scalars(select(Task).options(joinedload(Task.document), joinedload(Task.intake_item))
                               .where(Task.id > after_id).order_by(Task.id)).unique())


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--replays", type=int, default=16)
    args = parser.parse_args()
    if not 2 <= args.replays <= 24:
        raise SystemExit("Choose 2..24 replay requests for this bounded local race")
    verify_stack()
    manifest_bytes = (args.corpus / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    doc = next(item for item in manifest["documents"] if item["kind"] in ("scan_high", "scan_medium"))
    content = (args.corpus / doc["filename"]).read_bytes()
    with SessionLocal() as db:
        before_id = int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)
    if queue_snapshot() != {"queued": 0, "started": 0, "failed": 0}:
        raise SystemExit("Reconcile the isolated queue before starting this test")

    key = hashlib.sha256(f"replay-worker-race:{args.run_id}".encode()).hexdigest()[:48]
    body, content_type = multipart([doc], args.corpus, key)
    opener = login(args.base_url, USER, PASSWORD)
    accepted = post_form(opener, args.base_url + "/upload", body, content_type)
    if accepted != 200:
        raise AssertionError(f"Synthetic initial upload returned HTTP {accepted}")
    rows = task_rows(before_id)
    if len(rows) != 1 or rows[0].intake_item is None or rows[0].document.content_hash != doc["sha256"]:
        raise AssertionError("Initial upload did not create one source-linked task for the intended document")
    task_id = rows[0].id

    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log_dir / f"{args.run_id}-worker.log"
    process = None
    results: list[int] = []
    try:
        with log_path.open("wb") as log:
            process = subprocess.Popen([sys.executable, "scripts/dev/run_worker.py", "ocr"], cwd=ROOT,
                                       env={**os.environ, "APP_DEBUG": "1"}, stdout=log, stderr=subprocess.STDOUT)
            wait_for_workers([process], 1)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                with SessionLocal() as db:
                    task = db.get(Task, task_id)
                    if task is not None and task.status == TaskStatus.recognizing:
                        break
                if process.poll() is not None:
                    raise RuntimeError("Local OCR worker exited before claiming the synthetic task")
                time.sleep(0.02)
            else:
                raise TimeoutError("Did not observe the local worker claim")

            # Replays arrive while OCR is in progress. They must not recreate
            # outbox intents or duplicate the active queue admission.
            with ThreadPoolExecutor(max_workers=args.replays) as pool:
                futures = [pool.submit(post_form, opener, args.base_url + "/upload", body, content_type)
                           for _ in range(args.replays)]
                results = [future.result(timeout=60) for future in futures]

            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                with SessionLocal() as db:
                    task = db.get(Task, task_id)
                    if task is not None and task.status in (TaskStatus.needs_review, TaskStatus.failed):
                        break
                if process.poll() is not None:
                    raise RuntimeError("Local OCR worker exited while finishing the synthetic task")
                time.sleep(0.1)
            else:
                raise TimeoutError("Synthetic OCR task did not reach an explicit terminal review/error state")
    finally:
        if process is not None:
            stop_workers([process])

    final_rows = task_rows(before_id)
    with SessionLocal() as db:
        kinds = list(db.scalars(select(Event.kind).where(Event.task_id == task_id)))
    final_status = final_rows[0].status if len(final_rows) == 1 else "wrong-card-count"
    queue_final = queue_snapshot()
    finished_jobs = finished_task_job_ids(task_id)
    if any(status != 200 for status in results):
        raise AssertionError(f"Some same-key replays failed: {results}")
    if len(final_rows) != 1 or final_rows[0].status != TaskStatus.needs_review.value:
        raise AssertionError(f"Expected one review task, observed {len(final_rows)} with status {final_status}")
    if final_rows[0].intake_item is None or final_rows[0].document.content_hash != doc["sha256"]:
        raise AssertionError("Replay race lost or changed the source association")
    if kinds.count("recognized") != 1:
        raise AssertionError(f"Expected exactly one recognized event, observed {kinds.count('recognized')}")
    if len(finished_jobs) != 1:
        raise AssertionError(f"Expected one completed OCR delivery for this task, observed {len(finished_jobs)}")
    if queue_final != {"queued": 0, "started": 0, "failed": 0}:
        raise AssertionError(f"Isolated queue did not drain cleanly: {queue_final}")

    summary = {"run_id": args.run_id,
               "revision": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
               "corpus_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
               "source_document_sha256": doc["sha256"], "initial_http_status": accepted,
               "concurrent_replay_requests": args.replays, "replay_statuses": results,
               "tasks_created": len(final_rows), "final_status": final_status,
               "recognized_events": kinds.count("recognized"), "completed_ocr_job_ids": finished_jobs,
               "queue_empty": queue_final == {"queued": 0, "started": 0, "failed": 0}}
    output = args.log_dir / f"{args.run_id}-summary.json"
    output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
