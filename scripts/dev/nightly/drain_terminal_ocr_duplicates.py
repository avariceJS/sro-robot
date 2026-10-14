#!/usr/bin/env python3
"""Drain only queued OCR jobs already mapped to terminal tasks on the local test DB."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import signal
import subprocess
import sys
import time

from redis import Redis
from rq import Queue
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal
from app.db.models import Task
from app.settings import settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path, help="Keep diagnostic stream outside Git")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    queue = Queue("ocr", connection=redis)
    with SessionLocal() as db:
        before = {task.id: task.status.value for task in db.scalars(select(Task)).all()}
    jobs = list(queue.jobs)
    task_ids = [
        job.args[0]
        for job in jobs
        if job.func_name == "app.workers.jobs.recognize" and job.args and isinstance(job.args[0], int)
    ]
    if len(task_ids) != len(jobs) or any(before.get(task_id) not in ("needs_review", "failed") for task_id in task_ids):
        raise SystemExit("Refused: the queue is not exclusively duplicate OCR for terminal tasks")

    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("wb") as stream:
        worker = subprocess.Popen(
            [sys.executable, "scripts/dev/run_worker.py", "ocr"],
            cwd=ROOT,
            env={**__import__("os").environ, "APP_DEBUG": "1"},
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + args.timeout_seconds
        while time.monotonic() < deadline and (queue.count or queue.started_job_registry.count):
            if worker.poll() is not None:
                raise SystemExit("Worker exited before the duplicate queue drained; preserve the log")
            time.sleep(0.2)
        if queue.count or queue.started_job_registry.count:
            raise SystemExit("Drain timed out; preserve the log and inspect isolated queue state")
        worker.send_signal(signal.SIGINT)
        worker.wait(timeout=15)

    with SessionLocal() as db:
        after = {task.id: task.status.value for task in db.scalars(select(Task)).all()}
    if before != after:
        raise SystemExit("Task status changed while draining duplicate recognition deliveries")
    print(f"drained_queued_jobs={len(jobs)} task_states_unchanged=true")
    print(f"stream_sha256={hashlib.sha256(args.log.read_bytes()).hexdigest()}")
    redis.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
