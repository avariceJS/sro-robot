#!/usr/bin/env python3
"""Measure bounded concurrent PostgreSQL OCR claims using synthetic rows only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from math import ceil
import os
from pathlib import Path
import re
import statistics
import subprocess
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
drill_container = os.environ.get("SRO_DRILL_CONTAINER_NAME", "")
if (
    database_url.get_backend_name() != "postgresql"
    or database_url.host != "127.0.0.1"
    or not re.fullmatch(r"sro-restore-drill-[0-9]+", drill_container)
):
    raise SystemExit("refusing synthetic writes without a loopback restore-drill container identity")

try:
    container_running = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}}|{{.Config.Image}}",
            drill_container,
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    published_port = subprocess.run(
        ["docker", "port", drill_container, "5432/tcp"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit("refusing synthetic writes because disposable PostgreSQL identity is unverifiable")

published_endpoints = {
    endpoint.strip()
    for endpoint in published_port.stdout.splitlines()
    if published_port.returncode == 0
}
expected_endpoint = f"127.0.0.1:{database_url.port}"
if (
    container_running.returncode != 0
    or container_running.stdout.strip() != "true|postgres:16-alpine"
    or expected_endpoint not in published_endpoints
):
    raise SystemExit("refusing synthetic writes because the loopback port is not owned by the restore drill")

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
call_failures: Counter[str] = Counter()
with ThreadPoolExecutor(max_workers=worker_count) as pool:
    futures = [pool.submit(claim_one, task_id) for task_id in task_ids]
    for future in as_completed(futures):
        try:
            results.append(future.result())
        except Exception as exc:
            # A DB exception may happen before or after commit. Reconcile every
            # synthetic task from PostgreSQL below instead of inferring its
            # outcome from the client-side exception.
            call_failures[type(exc).__name__] += 1
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
    f"workers={worker_count} elapsed_seconds={elapsed:.3f} "
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
