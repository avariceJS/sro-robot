#!/usr/bin/env python3
"""Measure bounded concurrent PostgreSQL OCR claims using synthetic rows only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil
import os
from pathlib import Path
import statistics
import sys
import time
import uuid

from sqlalchemy import select
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.base import SessionLocal
from app.db.models import Document, IntakeSource, Task, TaskLease, TaskStatus
from app.services.leases import claim_ocr
from app.settings import settings


task_count = int(os.environ.get("SRO_LEASE_LOAD_TASKS", "1000"))
worker_count = int(os.environ.get("SRO_LEASE_LOAD_WORKERS", "20"))
if not 1 <= task_count <= 5000:
    raise SystemExit("SRO_LEASE_LOAD_TASKS must be between 1 and 5000")
if not 1 <= worker_count <= 64:
    raise SystemExit("SRO_LEASE_LOAD_WORKERS must be between 1 and 64")
if os.environ.get("SRO_DISPOSABLE_DRILL") != "1":
    raise SystemExit("refusing synthetic writes without the disposable-drill marker")
database_url = make_url(settings.database_url)
if database_url.get_backend_name() != "postgresql" or database_url.host not in {
    "127.0.0.1",
    "localhost",
    "::1",
}:
    raise SystemExit("refusing synthetic writes unless PostgreSQL is reached via loopback")

marker = f"lease-claim-load-{uuid.uuid4().hex}"
with SessionLocal() as db:
    document = db.scalar(
        select(Document)
        .where(Document.source.in_((IntakeSource.upload, IntakeSource.batch)))
        .order_by(Document.id)
        .limit(1)
    )
    if document is None:
        raise SystemExit("synthetic OCR upload document is missing")
    rows = [
        Task(
            document_id=document.id,
            scenario="person",
            profile="default",
            status=TaskStatus.new,
            recognized={},
            values={"synthetic_load_marker": marker},
        )
        for _ in range(task_count)
    ]
    db.add_all(rows)
    db.flush()
    task_ids = [row.id for row in rows]
    db.commit()


def claim_one(task_id: int) -> tuple[int, float]:
    started = time.perf_counter()
    with SessionLocal() as db:
        claim = claim_ocr(db, task_id)
        if claim is None:
            raise RuntimeError(f"synthetic task {task_id} did not receive a lease")
        db.commit()
    return task_id, time.perf_counter() - started


started = time.perf_counter()
results: list[tuple[int, float]] = []
with ThreadPoolExecutor(max_workers=worker_count) as pool:
    futures = [pool.submit(claim_one, task_id) for task_id in task_ids]
    for future in as_completed(futures):
        results.append(future.result())
elapsed = time.perf_counter() - started

claimed_ids = [task_id for task_id, _latency in results]
if len(claimed_ids) != task_count or len(set(claimed_ids)) != task_count:
    raise SystemExit("claim workload produced missing or duplicate task results")

with SessionLocal() as db:
    claimed = list(
        db.execute(
            select(Task.id, Task.status, TaskLease.owner_token, TaskLease.attempt)
            .join(TaskLease, TaskLease.task_id == Task.id)
            .where(Task.id.in_(task_ids))
        )
    )
    if len(claimed) != task_count:
        raise SystemExit("database lease count differs from synthetic workload")
    if any(status != TaskStatus.recognizing or attempt != 1 for _, status, _token, attempt in claimed):
        raise SystemExit("synthetic claim workload left an unexpected state or attempt count")
    if len({token for _task_id, _status, token, _attempt in claimed}) != task_count:
        raise SystemExit("synthetic claim workload did not assign unique lease owners")

latencies = sorted(latency for _task_id, latency in results)
p95 = latencies[max(0, ceil(len(latencies) * 0.95) - 1)]
print(
    "Synthetic PostgreSQL OCR claim load passed: "
    f"tasks={task_count} workers={worker_count} elapsed_seconds={elapsed:.3f} "
    f"claims_per_second={task_count / elapsed:.1f} "
    f"claim_latency_ms_p50={statistics.median(latencies) * 1000:.1f} "
    f"claim_latency_ms_p95={p95 * 1000:.1f} unique_owners={len({row[2] for row in claimed})}"
)
print("Diagnostic only: synthetic disposable PostgreSQL run; no SLA or production-load claim")
