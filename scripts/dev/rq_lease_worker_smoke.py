#!/usr/bin/env python3
"""Run a real RQ worker subprocess against one synthetic leased OCR task."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from redis import Redis
from rq import Queue, Worker
from sqlalchemy import func, select

from app.db.base import session_scope
from app.db.models import Document, Event, IntakeSource, Task, TaskLease, TaskStatus
from app.services.leases import reconcilable_ocr_task_ids
from app.settings import settings
from lease_claim_load_safety import verify_disposable_postgres, verify_disposable_redis
from scripts.dev.lease_claim_rq_job import recognize_synthetic


def _create_synthetic_task(marker: str) -> int:
    content_hash = hashlib.sha256(marker.encode()).hexdigest()
    with session_scope() as db:
        document = Document(
            filename="synthetic-rq-worker-smoke.pdf",
            stored_path=str(settings.upload_dir / f"{content_hash}.pdf"),
            content_hash=content_hash,
            source=IntakeSource.upload,
        )
        db.add(document)
        db.flush()
        task = Task(
            document_id=document.id,
            scenario="person",
            profile="default",
            status=TaskStatus.new,
            recognized={},
            values={"rq_worker_smoke": marker},
        )
        db.add(task)
        db.flush()
        task_id = task.id
        db.commit()
    return task_id


def _verify(marker: str, *, expected_attempt: int = 1) -> None:
    with session_scope() as db:
        task = db.scalar(select(Task).where(Task.values["rq_worker_smoke"].astext == marker))
        if task is None or task.status != TaskStatus.needs_review:
            raise SystemExit("synthetic RQ worker task read-back failed")
        lease = db.get(TaskLease, task.id)
        if (
            lease is None
            or lease.attempt != expected_attempt
            or lease.generation != expected_attempt
        ):
            raise SystemExit("synthetic RQ lease identity read-back failed")
        event_counts = dict(
            db.execute(
                select(Event.kind, func.count())
                .where(Event.task_id == task.id)
                .group_by(Event.kind)
            ).all()
        )
        if (
            event_counts.get("recognized") != 1
            or event_counts.get("ocr_claimed") != expected_attempt
            or event_counts.get("ocr_claim_completed") != 1
        ):
            raise SystemExit("synthetic RQ lease event read-back failed")


def _run_worker(queue_name: str) -> None:
    environment = os.environ.copy()
    environment["SRO_RQ_WORKER_SMOKE_QUEUE"] = queue_name
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "worker"],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise SystemExit(f"synthetic RQ worker subprocess failed ({result.returncode})")


def _verify_in_fresh_process(
    marker: str,
    message: str,
    *,
    expected_attempt: int = 1,
) -> None:
    arguments = [sys.executable, str(Path(__file__).resolve()), "verify", marker]
    if expected_attempt != 1:
        arguments.append(str(expected_attempt))
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(message)


def _run() -> None:
    if not settings.ocr_task_leases_enabled:
        raise SystemExit("refusing RQ worker smoke unless OCR lease mode is enabled")
    verify_disposable_postgres(settings.database_url)
    verify_disposable_redis(settings.redis_url)

    marker = f"rq-worker-smoke-{uuid.uuid4().hex}"
    queue_name = f"lease-smoke-{uuid.uuid4().hex[:16]}"
    task_id = _create_synthetic_task(marker)

    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=3,
    )
    try:
        queue = Queue(queue_name, connection=client)
        if queue.count != 0:
            raise SystemExit("synthetic RQ smoke queue was unexpectedly nonempty")
        queue.enqueue(recognize_synthetic, task_id, marker)
    finally:
        client.close()

    _run_worker(queue_name)
    _verify_in_fresh_process(marker, "fresh-process synthetic RQ task read-back failed")

    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=3,
    )
    try:
        Queue(queue_name, connection=client).enqueue(recognize_synthetic, task_id, marker)
    finally:
        client.close()
    _run_worker(queue_name)
    _verify_in_fresh_process(marker, "fresh-process duplicate-delivery read-back failed")

    crash_marker = f"rq-worker-crash-smoke-{uuid.uuid4().hex}"
    crash_task_id = _create_synthetic_task(crash_marker)
    crash_key = f"sro:lease-smoke:claimed:{crash_marker}"
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=3,
    )
    try:
        Queue(queue_name, connection=client).enqueue(
            recognize_synthetic,
            crash_task_id,
            crash_marker,
            True,
        )
        client.delete(crash_key)
    finally:
        client.close()
    _run_worker(queue_name)

    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=3,
    )
    try:
        for _ in range(100):
            if client.get(crash_key) == b"1":
                break
            time.sleep(0.1)
        else:
            raise SystemExit("synthetic RQ crash point was not reached")
    finally:
        client.close()

    with session_scope() as db:
        crash_task = db.scalar(
            select(Task).where(Task.values["rq_worker_smoke"].astext == crash_marker)
        )
        crash_lease = db.get(TaskLease, crash_task_id)
        if (
            crash_task is None
            or crash_task.status != TaskStatus.recognizing
            or crash_lease is None
            or crash_lease.attempt != 1
            or crash_lease.generation != 1
            or crash_task_id in reconcilable_ocr_task_ids(db)
        ):
            raise SystemExit("synthetic RQ crash did not preserve its active lease")
        now = db.scalar(select(func.clock_timestamp()))
        crash_lease.lease_until = now - timedelta(seconds=1)
        db.commit()

    with session_scope() as db:
        if crash_task_id not in reconcilable_ocr_task_ids(db):
            raise SystemExit("expired synthetic RQ lease was not selected for recovery")

    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=3,
    )
    try:
        Queue(queue_name, connection=client).enqueue(
            recognize_synthetic,
            crash_task_id,
            crash_marker,
        )
    finally:
        client.close()
    _run_worker(queue_name)
    _verify_in_fresh_process(
        crash_marker,
        "fresh-process RQ crash-recovery read-back failed",
        expected_attempt=2,
    )
    print(
        "RQ subprocess lease completion, duplicate delivery, crash recovery, "
        "and fresh-process read-back passed"
    )


def _worker(queue_name: str) -> None:
    verify_disposable_postgres(settings.database_url)
    verify_disposable_redis(settings.redis_url)
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=3,
    )
    try:
        worker = Worker(
            [Queue(queue_name, connection=client)],
            connection=client,
            name=f"synthetic-lease-smoke-{os.getpid()}",
        )
        worker.work(burst=True, logging_level="WARNING")
    finally:
        client.close()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        _run()
    elif len(sys.argv) == 2 and sys.argv[1] == "worker":
        queue_name = os.environ.get("SRO_RQ_WORKER_SMOKE_QUEUE", "")
        if not queue_name.startswith("lease-smoke-"):
            raise SystemExit("refusing RQ worker smoke without its isolated queue name")
        _worker(queue_name)
    elif len(sys.argv) in (3, 4) and sys.argv[1] == "verify":
        verify_disposable_postgres(settings.database_url)
        expected_attempt = int(sys.argv[3]) if len(sys.argv) == 4 else 1
        _verify(sys.argv[2], expected_attempt=expected_attempt)
        print("synthetic RQ worker task read-back passed")
    else:
        raise SystemExit("Usage: rq_lease_worker_smoke.py [worker|verify marker]")
