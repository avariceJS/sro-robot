#!/usr/bin/env python3
"""Measure an exact-limit 100 MiB multipart upload through the local app."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload
from redis import Redis
from rq import Queue

from app.db.base import SessionLocal
from app.db.models import Task, TaskStatus
from app.settings import settings
from measure_http_batch import USER, PASSWORD, login, multipart, post_form, wait_for_workers, stop_workers, process_sample

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_BATCH_BYTES = 100 * 1024 * 1024


def queue_empty() -> bool:
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        queue = Queue("ocr", connection=redis)
        return queue.count == 0 and queue.started_job_registry.count == 0 and queue.failed_job_registry.count == 0
    finally:
        redis.close()


def make_limit_corpus(
    source_root: Path,
    target: Path,
    run_id: str,
    kind: str = "text",
    selection_offset: int = 0,
) -> list[dict]:
    manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
    target.mkdir(parents=True, exist_ok=True)
    result = []
    selected = [item for item in manifest["documents"] if item["kind"] == kind]
    if selection_offset < 0 or len(selected) < selection_offset + 4:
        raise SystemExit(f"Expected four unique synthetic source PDFs for kind {kind} at offset {selection_offset}")
    for index, source in enumerate(selected[selection_offset : selection_offset + 4], start=1):
        name = f"{run_id}-{index:02d}.pdf"
        path = target / name
        prefix = (f"\n% SRO nightly synthetic padding {run_id} file {index}\n").encode()
        with path.open("wb") as output:
            output.write((source_root / source["filename"]).read_bytes())
            output.write(prefix)
            remaining = MAX_FILE_BYTES - output.tell()
            line = (f"% synthetic-padding-{index}-".encode() + b"x" * 4000 + b"\n")
            while remaining >= len(line):
                output.write(line)
                remaining -= len(line)
            if remaining == 1:
                output.write(b"%")  # A final PDF comment may terminate at EOF.
            elif remaining:
                output.write(b"%" + b"x" * (remaining - 2) + b"\n")
        if path.stat().st_size != MAX_FILE_BYTES:
            raise AssertionError(f"File did not reach the exact per-file limit: {name}")
        if kind == "text":
            extracted = subprocess.run(
                ["pdftotext", str(path), "-"], capture_output=True, text=True, check=True, timeout=60
            ).stdout
            if source["case_number"] not in extracted or source["inn"] not in extracted:
                raise AssertionError(f"Padded PDF lost its synthetic text layer: {name}")
        else:
            info = subprocess.run(
                ["pdfinfo", str(path)], capture_output=True, text=True, check=True, timeout=60
            ).stdout
            if not re.search(r"^Pages:\s+1$", info, re.MULTILINE):
                raise AssertionError(f"Padded scanned PDF did not remain a one-page readable PDF: {name}")
        result.append({
            **{key: source[key] for key in ("case_number", "doc_date", "fio", "inn", "kind")},
            "filename": name,
            "path": path,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    if sum(item["bytes"] for item in result) != MAX_BATCH_BYTES:
        raise AssertionError("Generated input does not equal the exact 100 MiB batch limit")
    return result


def app_pid(base_url: str) -> int:
    port = urlsplit(base_url).port
    if port is None:
        raise ValueError("Base URL must have an explicit local port")
    result = subprocess.run(
        ["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True, text=True, check=True, timeout=5,
    )
    pids = [int(value) for value in result.stdout.split()]
    if len(pids) != 1:
        raise RuntimeError("Expected exactly one local app process listening on the test port")
    return pids[0]


def task_max_id() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)


def main() -> int:
    import argparse
    import threading

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-corpus", required=True, type=Path)
    parser.add_argument("--corpus-output", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081")
    parser.add_argument("--kind", choices=("text", "scan_high", "scan_medium", "scan_low"), default="text")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    inputs = make_limit_corpus(args.source_corpus, args.corpus_output, args.run_id, args.kind)
    if not queue_empty():
        raise SystemExit("OCR queue is not empty; reconcile before the maximum-batch measurement")

    key = hashlib.sha256(f"exact-limit:{args.run_id}".encode()).hexdigest()[:48]
    upload_body, content_type = multipart(inputs, args.corpus_output, key)
    if len(upload_body) > settings.upload_max_request_bytes:
        raise AssertionError("Exact-limit multipart body exceeded the configured request ceiling")
    operator = login(args.base_url, USER, PASSWORD)
    before_id = task_max_id()
    max_id = app_pid(args.base_url)
    log_path = args.output.with_suffix(".worker.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    worker = None
    samples: list[tuple[float, int]] = []
    with log_path.open("wb") as log_handle:
        worker = subprocess.Popen(
            [sys.executable, "scripts/dev/run_worker.py", "ocr"],
            cwd=ROOT, env={**os.environ, "APP_DEBUG": "1"},
            stdout=log_handle, stderr=subprocess.STDOUT,
        )
        try:
            wait_for_workers([worker], 1)
            process_roots = [max_id, worker.pid]
            sampling_stop = threading.Event()

            def sample() -> None:
                while not sampling_stop.is_set():
                    samples.append(process_sample(process_roots))
                    sampling_stop.wait(0.05)

            sampler = threading.Thread(target=sample, daemon=True)
            sampler.start()
            started = time.monotonic()
            status = post_form(
                operator, args.base_url + "/upload", upload_body, content_type
            )
            if status != 200:
                raise RuntimeError(f"Exact-limit multipart upload returned HTTP {status}")

            deadline = time.monotonic() + 900
            rows = []
            while time.monotonic() < deadline:
                with SessionLocal() as db:
                    rows = db.scalars(
                        select(Task).options(joinedload(Task.document), joinedload(Task.intake_item))
                        .where(Task.id > before_id).order_by(Task.id)
                    ).unique().all()
                    snapshot = [
                        {
                            "status": row.status.value,
                            "filename": row.document.filename,
                            "content_sha256": row.document.content_hash,
                            "source_item_id": row.intake_item.id if row.intake_item else None,
                            "recognized": row.recognized or {},
                        }
                        for row in rows
                    ]
                if len(snapshot) == 4 and all(
                    row["status"] in (TaskStatus.needs_review.value, TaskStatus.failed.value)
                    for row in snapshot
                ):
                    break
                time.sleep(0.25)
            wall = time.monotonic() - started
            sampling_stop.set()
            sampler.join(timeout=5)
            if len(snapshot) != 4:
                raise TimeoutError(f"Expected four max-batch tasks; found {len(snapshot)}")
            expected = {item["filename"]: item for item in inputs}
            if len({row["source_item_id"] for row in snapshot}) != 4 or any(
                row["source_item_id"] is None for row in snapshot
            ):
                raise AssertionError("Exact-limit upload did not retain four unique source links")
            if any(row["status"] != TaskStatus.needs_review.value for row in snapshot):
                raise AssertionError("A valid exact-limit PDF did not reach local review")
            match_counts = {name: 0 for name in ("case_number", "doc_date", "fio", "inn")}
            for row in snapshot:
                source = expected[row["filename"]]
                recognized = row["recognized"]
                actual = {
                    "case_number": (recognized.get("case_number") or {}).get("value"),
                    "doc_date": (recognized.get("doc_date") or {}).get("value"),
                    "fio": (recognized.get("fio_raw") or {}).get("value"),
                    "inn": (recognized.get("inn") or {}).get("value"),
                }
                for name, value in actual.items():
                    match_counts[name] += value == source[name]
            if not queue_empty():
                raise AssertionError("OCR queue did not drain after the exact-limit upload")
            output = {
                "run_id": args.run_id,
                "input_kind": args.kind,
                "revision": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
                "file_count": len(inputs),
                "per_file_bytes": MAX_FILE_BYTES,
                "batch_file_bytes": sum(item["bytes"] for item in inputs),
                "multipart_request_bytes": len(upload_body),
                "request_limit_bytes": settings.upload_max_request_bytes,
                "http_status": status,
                "tasks": len(snapshot),
                "source_linked_tasks": len({row["source_item_id"] for row in snapshot}),
                "final_task_statuses": {name: sum(row["status"] == name for row in snapshot) for name in (TaskStatus.needs_review.value, TaskStatus.failed.value)},
                "field_matches": match_counts,
                "package_wall_seconds": wall,
                "mean_process_tree_cpu_percent": round(sum(item[0] for item in samples) / len(samples), 2) if samples else None,
                "peak_app_and_worker_tree_rss_mib": round(max((item[1] for item in samples), default=0) / (1024 * 1024), 2),
                "queue_empty_after_completion": True,
                "pdf_inputs": [{"filename": item["filename"], "bytes": item["bytes"], "sha256": item["sha256"]} for item in inputs],
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(output, ensure_ascii=False))
        finally:
            if worker is not None:
                stop_workers([worker])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
