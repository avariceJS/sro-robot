#!/usr/bin/env python3
"""Exercise local Redis/PostgreSQL outage recovery against the isolated Compose stack."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from redis import Redis
from rq import Queue, Worker
from sqlalchemy import func, select, text
from sqlalchemy.orm import joinedload

from app.db.base import SessionLocal, engine
from app.db.models import JobOutbox, Task, TaskStatus
from app.settings import settings
from measure_http_batch import USER, PASSWORD

COMPOSE = ["docker", "compose", "--env-file", ".env", "-f", "deploy/docker-compose.nightly.yml", "-p", "sro-nightly-20261014"]


def compose(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], cwd=ROOT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def login(base_url: str):
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    with opener.open(base_url + "/login", timeout=10):
        pass
    body = urlencode({"login": USER, "password": PASSWORD}).encode()
    origin = base_url.split("/", 3)[0] + "//" + base_url.split("/", 3)[2]
    request = Request(
        base_url + "/login", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": origin},
    )
    try:
        with opener.open(request, timeout=10):
            pass
    except HTTPError as exc:
        raise RuntimeError(f"Synthetic local login returned HTTP {exc.code}") from exc
    return opener


def post(opener, base_url: str, filename: str, content: bytes, key: str) -> int:
    boundary = "sro-recovery-" + hashlib.sha256(key.encode()).hexdigest()[:32]
    payload = b"".join([
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"idempotency_key\"\r\n\r\n{key}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n".encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    origin = base_url.split("/", 3)[0] + "//" + base_url.split("/", 3)[2]
    request = Request(
        base_url + "/upload", data=payload, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Origin": origin, "Accept": "text/html"},
    )
    try:
        with opener.open(request, timeout=60) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def max_task_id() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)


def queue_counts() -> dict[str, int]:
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=2)
    queue = Queue("ocr", connection=redis)
    try:
        return {"queued": queue.count, "started": queue.started_job_registry.count, "failed": queue.failed_job_registry.count}
    finally:
        redis.close()


def wait_redis(timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
            client.ping()
            client.close()
            return
        except Exception:
            time.sleep(1)
    raise TimeoutError("Isolated Redis did not become available")


def wait_database(timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except Exception:
            engine.dispose()
            time.sleep(1)
    raise TimeoutError("Isolated PostgreSQL did not become available")


def snapshot_after(task_id: int) -> list[dict]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(Task).options(joinedload(Task.document), joinedload(Task.intake_item))
            .where(Task.id > task_id).order_by(Task.id)
        ).unique().all()
        return [{"id": task.id, "filename": task.document.filename, "status": task.status.value,
                 "source_item": task.intake_item is not None} for task in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--log-dir", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))
    source = (args.corpus / manifest["documents"][1]["filename"]).read_bytes()
    source += f"\n% recovery fixture {args.run_id}\n".encode()
    opener = login(args.base_url)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    before = max_task_id()
    if any(queue_counts().values()):
        raise RuntimeError("Reconcile the isolated OCR queue before recovery checks")
    with SessionLocal() as db:
        pending_before = db.scalar(select(func.count()).select_from(JobOutbox)) or 0
    if pending_before:
        raise RuntimeError("Reconcile the isolated outbox before recovery checks")

    results = {}
    redis_key = hashlib.sha256((args.run_id + ":redis").encode()).hexdigest()[:48]
    redis_name = f"synthetic-redis-recovery-{args.run_id}.pdf"
    compose("stop", "redis")
    try:
        redis_down_status = post(opener, args.base_url, redis_name, source, redis_key)
    finally:
        compose("up", "-d", "redis")
    wait_redis()
    after_redis_failure = snapshot_after(before)
    with SessionLocal() as db:
        outbox_after_failure = db.scalar(select(func.count()).select_from(JobOutbox)) or 0
    if redis_down_status != 503 or len(after_redis_failure) != 1 or outbox_after_failure != 1:
        raise AssertionError("Redis outage did not leave one explicit task and durable outbox intent")
    time.sleep(6)
    redis_replay_status = post(opener, args.base_url, redis_name, source, redis_key)
    results["redis"] = {"status_while_unavailable": redis_down_status,
                         "status_after_restore_same_key": redis_replay_status,
                         "tasks_after_failure": len(after_redis_failure),
                         "pending_outbox_after_failure": outbox_after_failure,
                         "tasks_after_replay": len(snapshot_after(before)),
                         "queue_after_replay": queue_counts()}
    if redis_replay_status != 200 or len(snapshot_after(before)) != 1 or queue_counts()["queued"] != 1:
        raise AssertionError("Redis replay did not dispatch one existing operation exactly once")
    with SessionLocal() as db:
        if (db.scalar(select(func.count()).select_from(JobOutbox)) or 0) != 0:
            raise AssertionError("Recovered outbox intent remained pending after enqueue")

    pg_key = hashlib.sha256((args.run_id + ":postgres").encode()).hexdigest()[:48]
    pg_name = f"synthetic-postgres-recovery-{args.run_id}.pdf"
    compose("stop", "db")
    try:
        postgres_down_status = post(opener, args.base_url, pg_name, source, pg_key)
    finally:
        compose("up", "-d", "db")
    wait_database()
    after_pg_failure = snapshot_after(before)
    if postgres_down_status < 500 or len(after_pg_failure) != 1:
        raise AssertionError("PostgreSQL outage had an unclear persisted result; do not retry")
    postgres_retry_status = post(opener, args.base_url, pg_name, source, pg_key)
    after_pg_retry = snapshot_after(before)
    if postgres_retry_status != 200 or len(after_pg_retry) != 2:
        raise AssertionError("Same-key PostgreSQL retry did not produce exactly one additional task")
    results["postgresql"] = {"status_while_unavailable": postgres_down_status,
                             "task_count_read_back_before_retry": len(after_pg_failure),
                             "status_after_restore_same_key": postgres_retry_status,
                             "task_count_after_retry": len(after_pg_retry),
                             "queue_after_retry": queue_counts()}

    log_path = args.log_dir / f"{args.run_id}-recovery-worker.log"
    with log_path.open("wb") as handle:
        worker = subprocess.Popen([sys.executable, "scripts/dev/run_worker.py", "ocr"], cwd=ROOT,
                                  env={**os.environ, "APP_DEBUG": "1"}, stdout=handle, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                redis = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=2)
                try:
                    active = Worker.all(connection=redis, queue=Queue("ocr", connection=redis))
                finally:
                    redis.close()
                if active:
                    break
                time.sleep(0.2)
            else:
                raise TimeoutError("Recovery worker did not register")
            deadline = time.monotonic() + 300
            rows = []
            while time.monotonic() < deadline:
                rows = snapshot_after(before)
                if len(rows) == 2 and all(row["status"] == TaskStatus.needs_review.value for row in rows):
                    break
                time.sleep(0.25)
            if len(rows) != 2 or not all(row["status"] == TaskStatus.needs_review.value for row in rows):
                raise AssertionError(f"Recovered tasks did not reach review: {rows}")
            if any(queue_counts().values()):
                raise AssertionError("Queue registries not empty after recovered tasks finished")
            results["final"] = {"tasks": rows, "queue_empty": True, "outbox_pending": 0}
            summary = {"run_id": args.run_id, "results": results}
            output = args.log_dir / f"{args.run_id}-recovery-summary.json"
            output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(summary, ensure_ascii=False))
            return 0
        finally:
            if worker.poll() is None:
                worker.send_signal(signal.SIGTERM)
                try:
                    worker.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    worker.send_signal(signal.SIGINT)
                    worker.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
