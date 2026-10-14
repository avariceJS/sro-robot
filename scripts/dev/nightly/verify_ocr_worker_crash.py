#!/usr/bin/env python3
"""Kill an OCR work horse in the isolated stack and recover its expired lease."""

from __future__ import annotations

import argparse
from datetime import timedelta
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
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.db.base import SessionLocal
from app.db.models import Event, IntakeSource, Task, TaskLease, TaskStatus
from app.settings import settings
from app.services.leases import reconcilable_ocr_task_ids
from measure_http_batch import USER, PASSWORD

POSTGRES_IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
REDIS_IMAGE = "redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"


def verify_nightly_stack() -> None:
    postgres = make_url(settings.database_url)
    redis = make_url(settings.redis_url)
    if postgres.host != "127.0.0.1" or postgres.port != 55442 or postgres.database != "sro_nightly":
        raise SystemExit("Refusing worker crash drill outside the dedicated nightly PostgreSQL endpoint")
    if redis.host != "127.0.0.1" or redis.port != 56389:
        raise SystemExit("Refusing worker crash drill outside the dedicated nightly Redis endpoint")
    expected = {
        "sro-nightly-20261014-db-1": (POSTGRES_IMAGE, "5432/tcp", "127.0.0.1:55442"),
        "sro-nightly-20261014-redis-1": (REDIS_IMAGE, "6379/tcp", "127.0.0.1:56389"),
    }
    for name, (image, port, host_port) in expected.items():
        result = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}|{{.Config.Image}}", name],
                                capture_output=True, text=True, check=False)
        published = subprocess.run(["docker", "port", name, port], capture_output=True, text=True, check=False)
        if result.returncode or result.stdout.strip() != f"true|{image}" or host_port not in published.stdout.splitlines():
            raise SystemExit("Refusing worker crash drill: isolated service identity could not be verified")


def login(base_url: str):
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    with opener.open(base_url + "/login", timeout=10):
        pass
    origin = base_url.split("/", 3)[0] + "//" + base_url.split("/", 3)[2]
    request = Request(base_url + "/login", data=urlencode({"login": USER, "password": PASSWORD}).encode(),
                      method="POST", headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": origin})
    try:
        with opener.open(request, timeout=10):
            pass
    except HTTPError as exc:
        raise RuntimeError(f"Synthetic local login returned HTTP {exc.code}") from exc
    return opener


def upload(opener, base_url: str, filename: str, content: bytes, key: str) -> int:
    boundary = "sro-crash-" + hashlib.sha256(key.encode()).hexdigest()[:32]
    body = b"".join([
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"idempotency_key\"\r\n\r\n{key}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n".encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    origin = base_url.split("/", 3)[0] + "//" + base_url.split("/", 3)[2]
    request = Request(base_url + "/upload", data=body, method="POST",
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Origin": origin})
    try:
        with opener.open(request, timeout=60) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def queue_and_redis():
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=2)
    return client, Queue("ocr", connection=client)


def find_work_horse(worker_pid: int) -> int | None:
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        pieces = line.strip().split(None, 2)
        if len(pieces) == 3 and pieces[1].isdigit() and int(pieces[1]) == worker_pid and "run_worker.py ocr" in pieces[2]:
            return int(pieces[0])
    return None


def wait_for_job(task_id: int, *, expect_failed: bool, timeout: float = 60) -> str:
    client, queue = queue_and_redis()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job_ids = queue.failed_job_registry.get_job_ids()
            for job_id in job_ids:
                job = queue.fetch_job(job_id)
                if job is not None and job.func_name.endswith("recognize") and job.args == (task_id,):
                    return job_id
            if not expect_failed:
                with SessionLocal() as db:
                    task = db.get(Task, task_id)
                    if task is not None and task.status == TaskStatus.needs_review:
                        return "completed"
            time.sleep(0.1)
        raise TimeoutError("OCR queue did not reach the expected recovery state")
    finally:
        client.close()


def stop_worker(worker: subprocess.Popen) -> None:
    if worker.poll() is None:
        worker.send_signal(signal.SIGTERM)
        try:
            worker.wait(timeout=15)
        except subprocess.TimeoutExpired:
            worker.send_signal(signal.SIGINT)
            worker.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--log-dir", required=True, type=Path)
    args = parser.parse_args()
    verify_nightly_stack()
    manifest = json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))
    doc = next(item for item in manifest["documents"] if item["kind"] == "scan_low")
    content = (args.corpus / doc["filename"]).read_bytes() + f"\n% crash drill {args.run_id}\n".encode()
    key = hashlib.sha256((args.run_id + ":intake").encode()).hexdigest()[:48]
    opener = login(args.base_url)
    client, queue = queue_and_redis()
    if queue.count or queue.started_job_registry.count or queue.failed_job_registry.count:
        client.close()
        raise SystemExit("Reconcile the isolated OCR queue before worker crash drill")
    client.close()
    with SessionLocal() as db:
        before_id = int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)
    status = upload(opener, args.base_url, f"crash-{args.run_id}.pdf", content, key)
    if status != 200:
        raise AssertionError(f"Synthetic OCR intake returned HTTP {status}")
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.id > before_id).order_by(Task.id))
        if task is None or task.intake_item is None or task.document is None:
            raise AssertionError("Synthetic OCR task/source was not persisted")
        task_id = task.id

    args.log_dir.mkdir(parents=True, exist_ok=True)
    first_log = (args.log_dir / f"{args.run_id}-worker-first.log").open("wb")
    env = {**os.environ, "APP_DEBUG": "1", "OCR_TASK_LEASES_ENABLED": "1"}
    first_worker = subprocess.Popen([sys.executable, "scripts/dev/run_worker.py", "ocr"], cwd=ROOT,
                                    env=env, stdout=first_log, stderr=subprocess.STDOUT)
    horse_pid = None
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                lease = db.get(TaskLease, task_id)
                if task is not None and task.status == TaskStatus.recognizing and lease is not None:
                    horse_pid = find_work_horse(first_worker.pid)
                    if horse_pid:
                        break
            if first_worker.poll() is not None:
                raise RuntimeError("Initial leased OCR worker exited unexpectedly")
            time.sleep(0.05)
        if horse_pid is None:
            raise TimeoutError("Could not observe an active OCR work horse and database lease")
        os.kill(horse_pid, signal.SIGKILL)
        failed_job_id = wait_for_job(task_id, expect_failed=True)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            lease = db.get(TaskLease, task_id)
            if task is None or task.status != TaskStatus.recognizing or lease is None:
                raise AssertionError("Worker crash did not leave the explicit active-lease state")
            lease.lease_until = db.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
            db.commit()
            if task_id not in reconcilable_ocr_task_ids(db):
                raise AssertionError("Expired synthetic OCR lease was not recoverable")
    finally:
        stop_worker(first_worker)
        first_log.close()

    client, queue = queue_and_redis()
    try:
        job = queue.fetch_job(failed_job_id)
        if job is None or not job.func_name.endswith("recognize") or job.args != (task_id,):
            raise AssertionError("Refusing to requeue a failed job whose task identity does not match")
        queue.failed_job_registry.requeue(failed_job_id, at_front=True)
    finally:
        client.close()

    second_log = (args.log_dir / f"{args.run_id}-worker-restart.log").open("wb")
    second_worker = subprocess.Popen([sys.executable, "scripts/dev/run_worker.py", "ocr"], cwd=ROOT,
                                     env=env, stdout=second_log, stderr=subprocess.STDOUT)
    try:
        final_state = wait_for_job(task_id, expect_failed=False, timeout=300)
        if final_state != "completed":
            raise AssertionError("Requeued leased OCR job did not complete")
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            lease = db.get(TaskLease, task_id)
            attempts = list(db.scalars(select(Event.kind).where(Event.task_id == task_id)))
            if task is None or task.status != TaskStatus.needs_review or lease is None or lease.attempt != 2:
                raise AssertionError("Recovered OCR result or second lease attempt failed read-back")
            if attempts.count("ocr_claimed") != 2 or attempts.count("recognized") != 1 or attempts.count("ocr_claim_completed") != 1:
                raise AssertionError("OCR recovery audit event counts did not match one crash and one completion")
        result = {"run_id": args.run_id, "task_id": task_id, "input_kind": doc["kind"],
                  "intake_http_status": status, "worker_horse_killed": True,
                  "task_state_after_crash": "recognizing with active lease",
                  "failed_job_reconciled_by_task_id": task_id,
                  "same_rq_job_requeued_after_lease_expiry": True,
                  "final_task_status": "needs_review", "lease_attempts": 2,
                  "audit_claims": attempts.count("ocr_claimed"),
                  "audit_recognized": attempts.count("recognized"),
                  "audit_completions": attempts.count("ocr_claim_completed")}
        output = args.log_dir / f"{args.run_id}-worker-crash-summary.json"
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        stop_worker(second_worker)
        second_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
