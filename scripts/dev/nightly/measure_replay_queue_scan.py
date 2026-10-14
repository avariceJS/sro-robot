#!/usr/bin/env python3
"""Measure same-key replay's active OCR lookup at synthetic queue depths.

This enqueues inert synthetic RQ records only. No worker is started and no
database or document rows are created. The script refuses non-nightly Redis.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from redis import Redis
from rq import Queue
from rq.job import Job
from rq.registry import DeferredJobRegistry, ScheduledJobRegistry, StartedJobRegistry
from sqlalchemy.engine import make_url

from app.settings import settings
from app.workers.jobs import recognize
from app.workers.queues import active_ocr_task_ids


EXPECTED_HOST = "127.0.0.1"
EXPECTED_PORT = 56389
DEPTHS = (100, 1_000, 5_000)
SAMPLES = 7


def require_empty(queue: Queue, redis: Redis) -> None:
    registry_ids = set()
    for registry_type in (StartedJobRegistry, ScheduledJobRegistry, DeferredJobRegistry):
        registry = registry_type(queue.name, connection=redis, queue=queue)
        registry_ids.update(registry.get_job_ids(cleanup=False))
    if queue.count or registry_ids:
        raise SystemExit("Refusing measurement: reconcile the isolated OCR queue first")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--confirm-isolated-redis",
        action="store_true",
        help="confirm that only the dedicated nightly Redis is configured",
    )
    args = parser.parse_args()
    if not args.confirm_isolated_redis:
        raise SystemExit("Refusing: pass --confirm-isolated-redis for the dedicated test stack")
    url = make_url(settings.redis_url)
    if url.host != EXPECTED_HOST or url.port != EXPECTED_PORT:
        raise SystemExit("Refusing: Redis must be the loopback nightly service on port 56389")

    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=5)
    queue = Queue("ocr", connection=redis)
    require_empty(queue, redis)
    results = []
    created: list[Job] = []
    try:
        for depth in DEPTHS:
            require_empty(queue, redis)
            created = [queue.enqueue(recognize, task_id) for task_id in range(-depth, 0)]
            target_ids = set(range(-1, -101, -1))
            if len(created) != depth:
                raise AssertionError("Synthetic queue setup did not create the requested depth")
            timings_ms = []
            observed: set[int] = set()
            for _ in range(SAMPLES):
                started = time.perf_counter()
                observed = active_ocr_task_ids(target_ids)
                timings_ms.append((time.perf_counter() - started) * 1_000)
            expected = target_ids if depth >= 100 else set(range(-depth, 0))
            if observed != expected:
                raise AssertionError("Active OCR lookup returned unexpected synthetic task IDs")
            results.append({
                "queue_depth": depth,
                "target_task_ids": len(target_ids),
                "samples": SAMPLES,
                "median_ms": round(statistics.median(timings_ms), 3),
                "p95_ms": round(sorted(timings_ms)[-1], 3),
                "matched_ids": len(observed),
            })
            for job in created:
                job.delete()
            created = []
    finally:
        for job in created:
            job.delete()
        require_empty(queue, redis)
        redis.close()

    print(json.dumps({"run_id": args.run_id, "redis": f"{EXPECTED_HOST}:{EXPECTED_PORT}", "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
