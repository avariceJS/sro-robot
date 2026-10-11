#!/usr/bin/env python3
"""Measure bounded concurrent PostgreSQL OCR claims using synthetic rows only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from math import ceil
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import uuid

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.base import SessionLocal
from app.db.models import Document, IntakeSource, Task, TaskLease, TaskStatus
from app.services.leases import claim_ocr
from app.settings import settings
from lease_claim_load_safety import verify_disposable_postgres


task_count = int(os.environ.get("SRO_LEASE_LOAD_TASKS", "1000"))
worker_count = int(os.environ.get("SRO_LEASE_LOAD_WORKERS", "20"))
executor_mode = os.environ.get("SRO_LEASE_LOAD_MODE", "thread")
if not 1 <= task_count <= 5000:
    raise SystemExit("SRO_LEASE_LOAD_TASKS must be between 1 and 5000")
if not 1 <= worker_count <= 64:
    raise SystemExit("SRO_LEASE_LOAD_WORKERS must be between 1 and 64")
if executor_mode not in {"thread", "process"}:
    raise SystemExit("SRO_LEASE_LOAD_MODE must be thread or process")
verify_disposable_postgres(settings.database_url)

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


def claim_process_batch(task_batch: list[int]) -> dict:
    worker_script = Path(__file__).with_name("lease_claim_process_worker.py")
    process = subprocess.run(
        [sys.executable, str(worker_script)],
        input=json.dumps(task_batch),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if process.returncode != 0:
        return {"failures": {"process_worker_exit": 1}, "latencies": []}
    try:
        result = json.loads(process.stdout)
    except (json.JSONDecodeError, TypeError):
        return {"failures": {"process_worker_output": 1}, "latencies": []}
    if not isinstance(result, dict):
        return {"failures": {"process_worker_output": 1}, "latencies": []}
    return result


started = time.perf_counter()
results: list[tuple[int, float]] = []
call_failures: Counter[str] = Counter()
if executor_mode == "thread":
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(claim_one, task_id) for task_id in task_ids]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                # A DB exception may happen before or after commit. Reconcile every
                # synthetic task from PostgreSQL below instead of inferring its
                # outcome from the client-side exception.
                # Keep the diagnostic precise enough to distinguish SQLAlchemy's
                # pool timeout from a driver timeout, without exposing exception
                # text that may contain connection details.
                call_failures[f"{type(exc).__module__}.{type(exc).__name__}"] += 1
else:
    process_count = min(worker_count, task_count, 20)
    process_batches = [task_ids[offset::process_count] for offset in range(process_count)]
    with ThreadPoolExecutor(max_workers=process_count) as pool:
        futures = [pool.submit(claim_process_batch, batch) for batch in process_batches]
        for future in as_completed(futures):
            try:
                process_result = future.result()
                call_failures.update(process_result.get("failures", {}))
                results.extend(
                    (-1, float(latency)) for latency in process_result.get("latencies", [])
                )
            except Exception as exc:
                call_failures[f"{type(exc).__module__}.{type(exc).__name__}"] += 1
elapsed = time.perf_counter() - started

with SessionLocal() as db:
    outcomes = list(
        db.execute(
            select(Task.id, Task.status, TaskLease.owner_token, TaskLease.attempt)
            .outerjoin(TaskLease, TaskLease.task_id == Task.id)
            .where(Task.id.in_(task_ids))
        )
    )
if len(outcomes) != task_count:
    raise SystemExit("database read-back count differs from synthetic workload")

claimed = [row for row in outcomes if row[2] is not None]
claimed_ids = [task_id for task_id, _status, _token, _attempt in claimed]
unclaimed = [row for row in outcomes if row[2] is None]
unexpected = [
    row for row in claimed
    if row[1] != TaskStatus.recognizing or row[3] != 1
]
unexpected_unclaimed = [
    row for row in unclaimed
    if row[1] != TaskStatus.new or row[3] is not None
]
owner_tokens = {token for _task_id, _status, token, _attempt in claimed}
if (
    unexpected
    or unexpected_unclaimed
    or len(owner_tokens) != len(claimed_ids)
    or len(set(claimed_ids)) != len(claimed_ids)
):
    raise SystemExit("synthetic claim read-back found duplicate or unexpected lease state")

latencies = sorted(latency for _task_id, latency in results)
p95 = latencies[max(0, ceil(len(latencies) * 0.95) - 1)] if latencies else 0.0
failure_summary = ",".join(
    f"{error_type}:{count}" for error_type, count in sorted(call_failures.items())
) or "none"
print(
    "Synthetic PostgreSQL OCR claim diagnostic: "
    f"tasks={task_count} claimed={len(claimed)} unclaimed={len(unclaimed)} "
    f"workers={process_count if executor_mode == 'process' else worker_count} "
    f"executor={executor_mode} elapsed_seconds={elapsed:.3f} "
    f"pool_size={settings.database_pool_size} "
    f"max_overflow={settings.database_max_overflow} "
    f"claims_per_second={len(claimed) / elapsed:.1f} "
    f"client_errors={failure_summary}"
)
if results:
    print(
        "Successful claim-call latency: "
        f"p50={statistics.median(latencies) * 1000:.1f} ms "
        f"p95={p95 * 1000:.1f} ms; read-back unique owners={len(owner_tokens)}"
    )
if unclaimed:
    print("Diagnostic saturated or incomplete: not all synthetic tasks received a lease")
    raise SystemExit(1)
print("Diagnostic only: synthetic disposable PostgreSQL run; no SLA or production-load claim")
