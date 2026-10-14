#!/usr/bin/env python3
"""Upload and process the synthetic corpus through isolated app HTTP and RQ workers."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import math
import os
from pathlib import Path
import platform
import signal
import statistics
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
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
TERMINAL = {TaskStatus.needs_review, TaskStatus.failed}


def percentile(values: list[float], percent: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


def post_form(opener, url: str, data: bytes, content_type: str) -> int:
    request = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Origin": url.split("/", 3)[0] + "//" + url.split("/", 3)[2],
            "Accept": "text/html",
        },
    )
    try:
        with opener.open(request, timeout=300) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def login(base_url: str, username: str = USER, password: str = PASSWORD):
    cookie_jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    with opener.open(base_url + "/login", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("Local app login page unavailable")
    status = post_form(
        opener,
        base_url + "/login",
        urlencode({"login": username, "password": password}).encode(),
        "application/x-www-form-urlencoded",
    )
    if status != 200:
        raise RuntimeError(f"Synthetic operator login failed with HTTP {status}")
    with opener.open(base_url + "/", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("Authenticated local app page unavailable")
    return opener


def multipart(chunk: list[dict], root: Path, key: str) -> tuple[bytes, str]:
    boundary = "sro-nightly-" + hashlib.sha256((key + str(time.monotonic_ns())).encode()).hexdigest()[:32]
    pieces = []

    def field(name: str, value: str) -> None:
        pieces.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )

    field("idempotency_key", key)
    for item in chunk:
        filename = item["filename"]
        content = (root / filename).read_bytes()
        pieces.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                    "Content-Type: application/pdf\r\n\r\n"
                ).encode(),
                content,
                b"\r\n",
            ]
        )
    pieces.append(f"--{boundary}--\r\n".encode())
    return b"".join(pieces), f"multipart/form-data; boundary={boundary}"


def current_task_max_id() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)


def new_tasks_after(task_id: int) -> list[Task]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(Task)
            .options(joinedload(Task.document), joinedload(Task.intake_item))
            .where(Task.id > task_id)
            .order_by(Task.id)
        ).unique().all()
        db.expunge_all()
        return rows


def queue_is_empty() -> bool:
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        queue = Queue("ocr", connection=redis)
        return queue.count == 0 and queue.started_job_registry.count == 0
    finally:
        redis.close()


def wait_for_workers(processes: list[subprocess.Popen], expected: int, timeout: float = 30) -> None:
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("An isolated worker exited before registration")
            workers = Worker.all(connection=redis, queue=Queue("ocr", connection=redis))
            if len(workers) >= expected:
                return
            time.sleep(0.2)
    finally:
        redis.close()
    raise TimeoutError("Isolated RQ workers did not register in time")


def process_sample(pids: list[int]) -> tuple[float, int]:
    if not pids:
        return 0.0, 0
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,%cpu=,rss="],
        capture_output=True,
        text=True,
        check=False,
    )
    processes = {}
    for line in result.stdout.splitlines():
        try:
            pid, ppid, cpu, rss = line.split()
            processes[int(pid)] = (int(ppid), float(cpu), int(rss))
        except (ValueError, TypeError):
            continue
    descendants = set(pids)
    while True:
        child_pids = {pid for pid, (ppid, _cpu, _rss) in processes.items() if ppid in descendants}
        expanded = descendants | child_pids
        if expanded == descendants:
            break
        descendants = expanded
    cpu_total = 0.0
    rss_total = 0
    for pid in descendants:
        record = processes.get(pid)
        if record is not None:
            _ppid, cpu, rss = record
            cpu_total += cpu
            rss_total += rss * 1024
    return cpu_total, rss_total


def upload_chunks(opener, base_url: str, root: Path, docs: list[dict], seed: str) -> list[float]:
    durations = []
    for chunk_index, start in enumerate(range(0, len(docs), 20)):
        chunk = docs[start : start + 20]
        key = hashlib.sha256(f"{seed}:{chunk_index}".encode()).hexdigest()[:48]
        body, content_type = multipart(chunk, root, key)
        started = time.monotonic()
        status = post_form(opener, base_url + "/upload", body, content_type)
        elapsed = time.monotonic() - started
        durations.append(elapsed)
        if status != 200:
            raise RuntimeError(f"Local upload chunk {chunk_index + 1} returned HTTP {status}")
    return durations


def wait_for_tasks(ids: list[int], timeout: float, pids: list[int]) -> tuple[list[dict], float, float, int]:
    started = time.monotonic()
    deadline = started + timeout
    peak_rss = 0
    cpu_samples = []
    last_rows = []
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            rows = db.scalars(select(Task).where(Task.id.in_(ids))).all()
            last_rows = [
                {
                    "id": task.id,
                    "filename": task.document.filename,
                    "content_hash": task.document.content_hash,
                    "status": task.status.value,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                    "recognized": task.recognized,
                    "error": task.error,
                }
                for task in rows
            ]
        if len(last_rows) == len(ids) and all(row["status"] in {s.value for s in TERMINAL} for row in last_rows):
            break
        cpu, rss = process_sample(pids)
        cpu_samples.append(cpu)
        peak_rss = max(peak_rss, rss)
        time.sleep(0.25)
    wall = time.monotonic() - started
    if len(last_rows) != len(ids) or not all(row["status"] in {s.value for s in TERMINAL} for row in last_rows):
        raise TimeoutError("Not all synthetic tasks reached a terminal OCR review state")
    avg_cpu = statistics.fmean(cpu_samples) if cpu_samples else 0.0
    return last_rows, wall, avg_cpu, peak_rss


def stop_workers(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
    for process in processes:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--workers", type=int, choices=(1, 2, 4, 8, 12), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args()

    manifest_path = args.corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    docs = manifest["documents"]
    if len(docs) != 100 or len({doc["sha256"] for doc in docs}) != 100:
        raise SystemExit("Expected exactly 100 distinct synthetic PDFs")
    if not queue_is_empty():
        raise SystemExit("Isolated OCR queue is not empty; reconcile before this run")

    output_root = args.output.parent
    output_root.mkdir(parents=True, exist_ok=True)
    log_paths = [output_root / f"{args.run_id}-worker-{i + 1}.log" for i in range(args.workers)]
    processes = []
    log_handles = []
    try:
        for log_path in log_paths:
            log_handles.append(log_path.open("wb"))
            processes.append(
                subprocess.Popen(
                    [sys.executable, "scripts/dev/run_worker.py", "ocr"],
                    stdout=log_handles[-1],
                    stderr=subprocess.STDOUT,
                    cwd=ROOT,
                    env={**os.environ, "APP_DEBUG": "1"},
                )
            )
        wait_for_workers(processes, args.workers)
        pids = [process.pid for process in processes]
        opener = login(args.base_url)
        before_id = current_task_max_id()
        package_started = time.monotonic()
        upload_started = package_started
        upload_seconds = upload_chunks(opener, args.base_url, args.corpus, docs, args.run_id)
        upload_wall = time.monotonic() - upload_started
        created = new_tasks_after(before_id)
        if len(created) != 100:
            raise AssertionError(f"Expected 100 newly created local tasks; found {len(created)}")
        task_ids = [task.id for task in created]
        task_map = {task.document.filename: task for task in created}
        if set(task_map) != {doc["filename"] for doc in docs}:
            raise AssertionError("Uploaded task sources do not map one-to-one to the corpus")
        for doc in docs:
            task = task_map[doc["filename"]]
            if task.document.content_hash != doc["sha256"] or task.intake_item is None:
                raise AssertionError("Uploaded task lost its expected source hash or intake item")

        rows, process_wall, avg_cpu, peak_rss = wait_for_tasks(task_ids, args.timeout_seconds, pids)
        package_wall = time.monotonic() - package_started
        by_name = {row["filename"]: row for row in rows}
        field_results = []
        for doc in docs:
            row = by_name[doc["filename"]]
            recognized = row["recognized"] or {}
            expected = {name: doc[name] for name in ("case_number", "doc_date", "fio", "inn")}
            actual = {
                "case_number": (recognized.get("case_number") or {}).get("value"),
                "doc_date": (recognized.get("doc_date") or {}).get("value"),
                "fio": (recognized.get("fio_raw") or {}).get("value"),
                "inn": (recognized.get("inn") or {}).get("value"),
            }
            field_results.append({
                "kind": doc["kind"],
                "expected": expected,
                "actual": actual,
                "matched": expected == actual,
            })
        field_names = ("case_number", "doc_date", "fio", "inn")
        field_match_counts = {
            name: sum(result["expected"][name] == result["actual"][name] for result in field_results)
            for name in field_names
        }
        extraction_by_kind = {}
        for kind in sorted({result["kind"] for result in field_results}):
            group = [result for result in field_results if result["kind"] == kind]
            extraction_by_kind[kind] = {
                "documents": len(group),
                "exact_documents": sum(result["matched"] for result in group),
                "field_matches": {
                    name: sum(result["expected"][name] == result["actual"][name] for result in group)
                    for name in field_names
                },
            }

        task_count_before_replay = current_task_max_id()
        replay_start = time.monotonic()
        upload_chunks(opener, args.base_url, args.corpus, docs, args.run_id)
        replay_wall = time.monotonic() - replay_start
        task_count_after_replay = current_task_max_id()
        if task_count_after_replay != task_count_before_replay:
            raise AssertionError("Same-key replay created additional tasks")

        latencies = [
            max(0.0, (row["updated_at"] - row["created_at"]).total_seconds())
            for row in rows
        ]
        review_count = sum(row["status"] == TaskStatus.needs_review.value for row in rows)
        error_count = sum(row["status"] == TaskStatus.failed.value for row in rows)
        queue_and_card_count = len(rows)
        summary = {
            "run_id": args.run_id,
            "revision": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
            "host": platform.platform(),
            "python": platform.python_version(),
            "reportlab": "4.4.4",
            "workers": args.workers,
            "corpus_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "corpus_files": len(docs),
            "corpus_bytes": sum(doc["bytes"] for doc in docs),
            "upload_chunk_count": len(upload_seconds),
            "upload_chunk_seconds": upload_seconds,
            "upload_wall_seconds": upload_wall,
            "queue_tasks_created": queue_and_card_count,
            "review_cards_needs_review": review_count,
            "failed_tasks": error_count,
            "extraction_exact_match": sum(result["matched"] for result in field_results),
            "extraction_field_match_count": sum(sum(result["expected"][k] == result["actual"][k] for k in result["expected"]) for result in field_results),
            "extraction_fields_total": len(field_results) * 4,
            "extraction_field_match_counts": field_match_counts,
            "extraction_field_mismatch_counts": {
                name: len(field_results) - count for name, count in field_match_counts.items()
            },
            "extraction_fully_matched_documents": sum(result["matched"] for result in field_results),
            "extraction_by_input_kind": extraction_by_kind,
            "ocr_wait_after_last_upload_seconds": process_wall,
            "package_wall_seconds": package_wall,
            "docs_per_minute": round(queue_and_card_count * 60 / package_wall, 2) if package_wall else None,
            "task_latency_p50_seconds": percentile(latencies, 50),
            "task_latency_p95_seconds": percentile(latencies, 95),
            "average_sampled_worker_cpu_percent_total": round(avg_cpu, 2),
            "peak_sampled_workers_rss_mib_total": round(peak_rss / (1024 * 1024), 2),
            "same_request_replay_wall_seconds": replay_wall,
            "new_tasks_after_same_key_replay": task_count_after_replay - task_count_before_replay,
            "tasks": [
                {
                    "filename": row["filename"],
                    "content_sha256": row["content_hash"],
                    "status": row["status"],
                    "error_type_present": bool(row["error"]),
                    "matched": field_results[index]["matched"],
                }
                for index, row in enumerate(sorted(rows, key=lambda item: item["filename"]))
            ],
        }
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in summary.items() if key != "tasks"}, ensure_ascii=False))
        return 0
    finally:
        stop_workers(processes)
        for handle in log_handles:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
