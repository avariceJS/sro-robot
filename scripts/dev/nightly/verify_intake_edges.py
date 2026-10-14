#!/usr/bin/env python3
"""Verify empty/oversized rejection and one-document OCR failure isolation."""

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
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.db.base import SessionLocal
from app.db.models import Task, TaskStatus
from app.settings import settings

USER = "nightly-operator"
PASSWORD = "nightly-synthetic-password-only"


def login(base_url: str):
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    with opener.open(base_url + "/login", timeout=10):
        pass
    request = Request(
        base_url + "/login",
        data=urlencode({"login": USER, "password": PASSWORD}).encode(),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": base_url.split("/", 3)[0] + "//" + base_url.split("/", 3)[2],
        },
    )
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(f"Local synthetic login returned HTTP {response.status}")
    except HTTPError as exc:
        raise RuntimeError(f"Local synthetic login returned HTTP {exc.code}") from exc
    return opener


def multipart(parts: list[tuple[str, bytes]], key: str) -> tuple[bytes, str]:
    boundary = "sro-edge-" + hashlib.sha256(key.encode()).hexdigest()[:32]
    body = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"idempotency_key\"\r\n\r\n{key}\r\n".encode()
    ]
    for filename, content in parts:
        body.extend(
            [
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n".encode(),
                content,
                b"\r\n",
            ]
        )
    body.append(f"--{boundary}--\r\n".encode())
    return b"".join(body), f"multipart/form-data; boundary={boundary}"


def post(opener, base_url: str, parts: list[tuple[str, bytes]], key: str) -> int:
    body, content_type = multipart(parts, key)
    request = Request(
        base_url + "/upload",
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Accept": "text/html",
            "Origin": base_url.split("/", 3)[0] + "//" + base_url.split("/", 3)[2],
        },
    )
    try:
        with opener.open(request, timeout=120) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def max_task_id() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)


def wait_worker(timeout: float = 30) -> None:
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    queue = Queue("ocr", connection=redis)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if Worker.all(connection=redis, queue=queue):
                return
            time.sleep(0.2)
    finally:
        redis.close()
    raise TimeoutError("No isolated OCR worker registered")


def ocr_queue_counts() -> dict[str, int]:
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    queue = Queue("ocr", connection=redis)
    try:
        return {
            "queued": queue.count,
            "started": queue.started_job_registry.count,
            "failed": queue.failed_job_registry.count,
        }
    finally:
        redis.close()


def snapshot_after(task_id: int) -> list[dict]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(Task).options(joinedload(Task.document), joinedload(Task.intake_item))
            .where(Task.id > task_id).order_by(Task.id)
        ).unique().all()
        return [
            {
                "id": row.id,
                "filename": row.document.filename,
                "status": row.status.value,
                "has_source_item": row.intake_item is not None,
            }
            for row in rows
        ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--log", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))
    good = (args.corpus / manifest["documents"][0]["filename"]).read_bytes()
    good += f"\n% edge fixture {args.run_id}\n".encode()
    opener = login(args.base_url)
    common = args.run_id + ":"
    empty_status = post(opener, args.base_url, [("empty.pdf", b"")], hashlib.sha256((common + "empty").encode()).hexdigest()[:48])
    oversized = b"%PDF-1.7\n" + b"X" * (settings.upload_max_file_bytes + 1)
    oversized_status = post(opener, args.base_url, [("oversized.pdf", oversized)], hashlib.sha256((common + "large").encode()).hexdigest()[:48])
    if empty_status != 422 or oversized_status != 413:
        raise AssertionError(f"Expected HTTP 422 and 413, got {empty_status} and {oversized_status}")

    before = max_task_id()
    if any(ocr_queue_counts().values()):
        raise RuntimeError("Isolated OCR queue must be reconciled before the replay/edge check")
    replay_doc = good + f"\n% replay fixture {args.run_id}\n".encode()
    replay_key = hashlib.sha256((common + "lost-response-replay").encode()).hexdigest()[:48]
    first_replay_status = post(opener, args.base_url, [("synthetic-replay.pdf", replay_doc)], replay_key)
    queue_after_first = ocr_queue_counts()
    second_replay_status = post(opener, args.base_url, [("synthetic-replay.pdf", replay_doc)], replay_key)
    queue_after_replay = ocr_queue_counts()
    if first_replay_status != 200 or second_replay_status != 200:
        raise AssertionError("Accepted request replay did not return the expected local redirect")
    if queue_after_first["queued"] != 1 or queue_after_replay["queued"] != 1:
        raise AssertionError(
            f"Same-key replay duplicated or lost queued work: first={queue_after_first}, replay={queue_after_replay}"
        )
    log_path = args.log.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as output:
        worker = subprocess.Popen(
            [sys.executable, "scripts/dev/run_worker.py", "ocr"], cwd=ROOT,
            env={**os.environ, "APP_DEBUG": "1"}, stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            wait_worker()
            malformed = b"This is deliberately not a PDF; synthetic edge fixture.\n"
            mixed_status = post(
                opener, args.base_url,
                [("synthetic-corrupt.pdf", malformed), ("synthetic-valid.pdf", good)],
                hashlib.sha256((common + "mixed").encode()).hexdigest()[:48],
            )
            if mixed_status != 200:
                raise AssertionError(f"Mixed valid/corrupt intake returned HTTP {mixed_status}")
            deadline = time.monotonic() + 180
            rows = []
            while time.monotonic() < deadline:
                rows = snapshot_after(before)
                if len(rows) == 3 and all(row["status"] in {s.value for s in (TaskStatus.failed, TaskStatus.needs_review)} for row in rows):
                    break
                time.sleep(0.25)
            by_name = {row["filename"]: row for row in rows}
            if set(by_name) != {"synthetic-replay.pdf", "synthetic-corrupt.pdf", "synthetic-valid.pdf"}:
                raise AssertionError(f"Expected exactly three accepted source documents; got {sorted(by_name)}")
            if by_name["synthetic-corrupt.pdf"]["status"] != TaskStatus.failed.value:
                raise AssertionError("Corrupt synthetic PDF did not receive an explicit failed status")
            if by_name["synthetic-valid.pdf"]["status"] != TaskStatus.needs_review.value:
                raise AssertionError("Valid PDF was lost or failed alongside the corrupt PDF")
            if not all(row["has_source_item"] for row in rows):
                raise AssertionError("An accepted task has no source intake item")
            result = {
                "run_id": args.run_id,
                "empty_http_status": empty_status,
                "oversized_http_status": oversized_status,
                "oversized_bytes": len(oversized),
                "mixed_http_status": mixed_status,
                "accepted_tasks": len(rows),
                "source_linked_tasks": sum(row["has_source_item"] for row in rows),
                "replay_http_statuses": [first_replay_status, second_replay_status],
                "queue_after_first_request": queue_after_first,
                "queue_after_same_key_replay": queue_after_replay,
                "statuses": {name: row["status"] for name, row in by_name.items()},
            }
            args.log.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(result, ensure_ascii=False))
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
