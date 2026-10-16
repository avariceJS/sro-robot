#!/usr/bin/env python3
"""Measure isolated synthetic HTTP upload, OCR/RQ completion, and PostgreSQL cost.

This harness requires an explicit empty SRO_ROBOT_ENV_FILE, APP_DEBUG=1, and
loopback-only benchmark PostgreSQL, Redis, and HTTP endpoints. It creates only a
synthetic local user/session and processes local synthetic PDFs on the OCR queue.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
import statistics
import stat
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from itsdangerous import TimestampSigner
from redis import Redis
from rq import Queue, Worker
from rq.registry import DeferredJobRegistry, FailedJobRegistry, ScheduledJobRegistry, StartedJobRegistry
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import joinedload

from app.db.base import SessionLocal, engine
from app.db.models import DebtorCardDraft, JobOutbox, Task, TaskStatus, User
from app.services import auth
from app.settings import settings

TERMINAL = {TaskStatus.needs_review, TaskStatus.failed}
EXPECTED_FIELDS = {"case_number": "case_number", "doc_date": "doc_date", "fio": "fio_raw", "inn": "inn"}


def percentile(values: list[float], percent: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_safe_runtime(base_url: str) -> None:
    env_path = os.environ.get("SRO_ROBOT_ENV_FILE", "")
    if not env_path:
        raise SystemExit("Set SRO_ROBOT_ENV_FILE to an empty, private regular file")
    private_env = Path(env_path)
    if (
        private_env.is_symlink()
        or not private_env.is_file()
        or private_env.stat().st_size
        or stat.S_IMODE(private_env.stat().st_mode) & 0o077
    ):
        raise SystemExit("SRO_ROBOT_ENV_FILE must be an empty, private regular file")
    if os.environ.get("APP_DEBUG") != "1" or not settings.debug:
        raise SystemExit("Refusing benchmark unless APP_DEBUG=1")
    db = settings.database_url
    parsed_db = make_url(db)
    if parsed_db.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Refusing non-loopback PostgreSQL")
    if not parsed_db.database or "benchmark" not in parsed_db.database.casefold():
        raise SystemExit("Refusing database without 'benchmark' in its name")
    redis = urlsplit(settings.redis_url)
    if redis.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Refusing non-loopback Redis")
    target = urlsplit(base_url)
    if (target.scheme, target.hostname) != ("http", "127.0.0.1") or target.port is None:
        raise SystemExit("Refusing non-loopback or non-HTTP app URL")


def require_empty_queue() -> None:
    connection = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        queue = Queue("ocr", connection=connection)
        registries = (
            StartedJobRegistry("ocr", connection=connection),
            FailedJobRegistry("ocr", connection=connection),
            DeferredJobRegistry("ocr", connection=connection),
            ScheduledJobRegistry("ocr", connection=connection),
        )
        if queue.count or any(registry.count for registry in registries):
            raise SystemExit("Refusing run while isolated OCR queue or registries contain work")
    finally:
        connection.close()
    with SessionLocal() as db:
        nonterminal = db.scalar(
            select(func.count()).select_from(Task).where(
                Task.status.in_((TaskStatus.new, TaskStatus.recognizing))
            )
        )
        pending_outbox = db.scalar(select(func.count()).select_from(JobOutbox))
        if nonterminal or pending_outbox:
            raise SystemExit("Refusing run while isolated tasks or dispatch outbox are pending")


def make_signed_cookie() -> tuple[str, int]:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.login == "synthetic-bulk-benchmark"))
        if user is None:
            user = auth.create_user(
                db,
                "synthetic-bulk-benchmark",
                "synthetic-bulk-benchmark-password-only",
                "Synthetic Bulk Benchmark",
                "",
            )
        elif not user.is_active:
            raise SystemExit("The isolated synthetic benchmark user is inactive")
        token = __import__("secrets").token_urlsafe(32)
        row = auth.new_login_session(db, user.id, token)
        # Encrypted, synthetic-only state satisfies the persisted app-session
        # contract. The OCR route never opens or uses this state.
        state = json.dumps(
            {
                "cookies": [
                    {
                        "name": "synthetic-benchmark-only",
                        "value": "no-external-login",
                        "domain": "sroarbitr.sro-service.ru",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            },
            separators=(",", ":"),
        ).encode()
        auth.attach_external_session(
            db, row, state, settings.secret_key, auth.SESSION_MAX_AGE_SECONDS
        )
        db.commit()
        user_id = user.id
    payload = base64.b64encode(
        json.dumps({"user_id": user_id, "session_token": token}).encode()
    )
    cookie = TimestampSigner(settings.secret_key).sign(payload).decode("ascii")
    return cookie, user_id


def multipart(items: list[dict], corpus: Path, key: str) -> tuple[bytes, str]:
    boundary = "sro-bulk-" + hashlib.sha256((key + str(time.monotonic_ns())).encode()).hexdigest()[:32]
    pieces: list[bytes] = []

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
    for item in items:
        filename = str(item["filename"])
        content = (corpus / filename).read_bytes()
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


def post_upload(base_url: str, cookie: str, body: bytes, content_type: str) -> int:
    request = Request(
        base_url.rstrip("/") + "/upload",
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Origin": base_url.rstrip("/"),
            "Accept": "text/html",
            "Cookie": "session=" + cookie,
        },
    )
    try:
        with urlopen(request, timeout=300) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def task_max_id() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.coalesce(func.max(Task.id), 0))) or 0)


def upload_chunks(
    selected: list[dict], corpus: Path, base_url: str, cookie: str, run_id: str
) -> tuple[list[float], list[int]]:
    before_id = task_max_id()
    durations = []
    for index in range(0, len(selected), settings.upload_max_files):
        chunk = selected[index : index + settings.upload_max_files]
        key = hashlib.sha256(f"{run_id}:{index}".encode()).hexdigest()[:48]
        body, content_type = multipart(chunk, corpus, key)
        started = time.monotonic()
        status = post_upload(base_url, cookie, body, content_type)
        durations.append(time.monotonic() - started)
        if status != 200:
            raise RuntimeError(f"Synthetic upload request returned HTTP {status}")
    with SessionLocal() as db:
        rows = db.scalars(
            select(Task)
            .options(joinedload(Task.document), joinedload(Task.intake_item))
            .where(Task.id > before_id)
            .order_by(Task.id)
        ).unique().all()
        if len(rows) != len(selected):
            raise AssertionError(f"Expected {len(selected)} intake tasks, found {len(rows)}")
        expected = {item["filename"]: item for item in selected}
        if {row.document.filename for row in rows} != set(expected):
            raise AssertionError("Task source filenames differ from the selected corpus inputs")
        for row in rows:
            item = expected[row.document.filename]
            if row.document.content_hash != item["sha256"] or row.intake_item is None:
                raise AssertionError("Uploaded task lost its expected source hash or intake item")
        ids = [row.id for row in rows]
    return durations, ids


def process_sample(pids: list[int]) -> tuple[float, int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,%cpu=,rss="],
        capture_output=True,
        text=True,
        check=False,
    )
    processes: dict[int, tuple[int, float, int]] = {}
    for line in result.stdout.splitlines():
        try:
            pid, ppid, cpu, rss = line.split()
            processes[int(pid)] = (int(ppid), float(cpu), int(rss))
        except (ValueError, TypeError):
            continue
    descendants = set(pids)
    while True:
        expanded = descendants | {
            pid for pid, (ppid, _cpu, _rss) in processes.items() if ppid in descendants
        }
        if expanded == descendants:
            break
        descendants = expanded
    cpu_total = 0.0
    rss_total = 0
    for pid in descendants:
        record = processes.get(pid)
        if record:
            _ppid, cpu, rss = record
            cpu_total += cpu
            rss_total += rss * 1024
    return cpu_total, rss_total


def pg_stats() -> dict[str, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "select xact_commit, tup_inserted, tup_updated, blks_read, blks_hit "
                "from pg_stat_database where datname=current_database()"
            )
        ).one()
    names = ("xact_commit", "tup_inserted", "tup_updated", "blks_read", "blks_hit")
    return {name: int(value) for name, value in zip(names, row)}


def wait_for_tasks(ids: list[int], pids: list[int], timeout: float) -> tuple[list[dict], float, float, int, list[float]]:
    started = time.monotonic()
    deadline = started + timeout
    samples: list[float] = []
    peak_rss = 0
    query_seconds: list[float] = []
    snapshot: list[dict] = []
    while time.monotonic() < deadline:
        query_start = time.monotonic()
        with SessionLocal() as db:
            rows = db.scalars(select(Task).where(Task.id.in_(ids))).all()
            snapshot = [
                {
                    "id": row.id,
                    "status": row.status.value,
                    "recognized": row.recognized or {},
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    "error_present": bool(row.error),
                }
                for row in rows
            ]
        query_seconds.append(time.monotonic() - query_start)
        if len(snapshot) == len(ids) and all(
            item["status"] in {status.value for status in TERMINAL} for item in snapshot
        ):
            break
        cpu, rss = process_sample(pids)
        samples.append(cpu)
        peak_rss = max(peak_rss, rss)
        time.sleep(0.25)
    if len(snapshot) != len(ids) or any(
        item["status"] not in {status.value for status in TERMINAL} for item in snapshot
    ):
        raise TimeoutError("Not all synthetic tasks reached a terminal OCR review state")
    return snapshot, time.monotonic() - started, statistics.fmean(samples) if samples else 0.0, peak_rss, query_seconds


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


def wait_workers(processes: list[subprocess.Popen], expected: int) -> None:
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("An isolated OCR worker exited before registration")
            workers = Worker.all(connection=client, queue=Queue("ocr", connection=client))
            if len(workers) >= expected:
                return
            time.sleep(0.2)
    finally:
        client.close()
    raise TimeoutError("Isolated OCR workers did not register")


def select_mixed(manifest_docs: list[dict], count: int) -> list[dict]:
    texts = [item for item in manifest_docs if item.get("kind") == "text"]
    scans = [item for item in manifest_docs if str(item.get("kind", "")).startswith("scan_")]
    text_count = count // 2
    selected = texts[:text_count] + scans[: count - text_count]
    if len(selected) != count or len({item["sha256"] for item in selected}) != count:
        raise SystemExit(f"Corpus cannot supply {count} unique, balanced text and scan PDFs")
    return selected


def source_hashes() -> dict[str, str]:
    paths = (
        "app/api/routes_tasks.py",
        "app/api/routes_assignments.py",
        "app/workers/jobs.py",
        "app/services/tasks.py",
        "app/services/assignments.py",
        "app/db/models.py",
        "scripts/dev/run_worker.py",
        "migrations/versions/f4b7c2d091a6_internal_card_preparation.py",
        "sro_robot/core/ocr.py",
        "sro_robot/core/extract.py",
        "sro_robot/core/morph.py",
    )
    return {path: sha256(ROOT / path) for path in paths if (ROOT / path).is_file()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081")
    parser.add_argument("--counts", type=int, nargs="+", default=(10, 20, 50))
    parser.add_argument("--workers", type=int, nargs="+", default=(1, 2, 4))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--revision", required=True, help="Git commit/source identity for this run")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--worker-launcher-pool",
        action="store_true",
        help="launch one bounded OCR WorkerPool process for each requested worker count",
    )
    args = parser.parse_args()

    require_safe_runtime(args.base_url)
    manifest_path = args.corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    docs = manifest.get("documents", [])
    if not docs or any(not (args.corpus / item["filename"]).is_file() for item in docs):
        raise SystemExit("Corpus manifest is empty or references a missing PDF")
    selected = {count: select_mixed(docs, count) for count in args.counts}
    if any(count < 1 or count > settings.upload_max_batch_files for count in args.counts):
        raise SystemExit("Count exceeds upload batch configuration")
    args.log_dir.mkdir(parents=True, exist_ok=True)
    cookie, user_id = make_signed_cookie()
    results = []
    for count in args.counts:
        for workers in args.workers:
            require_empty_queue()
            processes: list[subprocess.Popen] = []
            handles = []
            worker_logs = []
            launch_groups = [workers] if args.worker_launcher_pool and workers > 1 else [1] * workers
            for index, pool_size in enumerate(launch_groups):
                log_path = args.log_dir / f"{args.run_id}-{count}-w{workers}-{index + 1}.log"
                handle = log_path.open("wb")
                handles.append(handle)
                worker_logs.append(str(log_path))
                command = [sys.executable, "scripts/dev/run_worker.py"]
                if pool_size > 1:
                    command.extend(["--workers", str(pool_size)])
                command.append("ocr")
                processes.append(
                    subprocess.Popen(
                        command,
                        cwd=ROOT,
                        env=os.environ.copy(),
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                    )
                )
            try:
                wait_workers(processes, workers)
                before_stats = pg_stats()
                run_start = time.monotonic()
                upload_seconds, task_ids = upload_chunks(
                    selected[count], args.corpus, args.base_url, cookie,
                    f"{args.run_id}-{count}-{workers}-{uuid.uuid4().hex}",
                )
                upload_wall = time.monotonic() - run_start
                rows, ocr_wall, cpu, peak_rss, db_reads = wait_for_tasks(
                    task_ids, [process.pid for process in processes], args.timeout_seconds
                )
                total_wall = time.monotonic() - run_start
                after_stats = pg_stats()
                by_id = {row["id"]: row for row in rows}
                with SessionLocal() as db:
                    loaded = db.scalars(
                        select(Task)
                        .options(joinedload(Task.document))
                        .where(Task.id.in_(task_ids))
                    ).unique().all()
                    names = {row.id: row.document.filename for row in loaded}
                    cards = db.scalars(
                        select(DebtorCardDraft).where(
                            DebtorCardDraft.source_task_id.in_(task_ids)
                        )
                    ).all()
                expected = {item["filename"]: item for item in selected[count]}
                matches = {field: 0 for field in EXPECTED_FIELDS}
                field_outcomes = {
                    field: {"exact": 0, "missing": 0, "mismatch": 0}
                    for field in EXPECTED_FIELDS
                }
                matches_by_kind: dict[str, dict[str, int]] = {}
                for task_id, row in by_id.items():
                    filename = names[task_id]
                    item = expected[filename]
                    kind = str(item.get("kind", "unknown"))
                    matches_by_kind.setdefault(kind, {field: 0 for field in EXPECTED_FIELDS})
                    recognized = row["recognized"]
                    for field, source in EXPECTED_FIELDS.items():
                        actual = (recognized.get(source) or {}).get("value")
                        match = actual == item[field]
                        matches[field] += match
                        matches_by_kind[kind][field] += match
                        field_outcomes[field][
                            "exact" if match else "missing" if not actual else "mismatch"
                        ] += 1
                card_quality = {
                    "created": len(cards),
                    "preparation_state": {
                        state: sum(card.preparation_state == state for card in cards)
                        for state in ("prepared", "reviewed")
                    },
                    "exact_display_name": 0,
                    "exact_inn": 0,
                    "exact_address": 0,
                    "exact_case_number": 0,
                    "exact_doc_date": 0,
                    "field_outcomes": {
                        field: {"exact": 0, "missing": 0, "mismatch": 0}
                        for field in ("fio", "inn", "address", "case_number", "doc_date")
                    },
                }
                for card in cards:
                    expected_item = expected[names[card.source_task_id]]
                    card_values = card.values or {}
                    expected_card_values = {
                        "fio": expected_item["fio"],
                        "inn": expected_item["inn"],
                        "address": "101000, вымышленный город Макетный, улица Учебная, дом 1",
                        "case_number": expected_item["case_number"],
                        "doc_date": expected_item["doc_date"],
                    }
                    card_actuals = {
                        "fio": card.display_name,
                        "inn": card.inn,
                        **{
                            field: (card_values.get(field) or {}).get("value")
                            for field in ("address", "case_number", "doc_date")
                        },
                    }
                    for field, expected_value in expected_card_values.items():
                        actual = card_actuals[field]
                        card_quality["field_outcomes"][field][
                            "exact" if actual == expected_value else "missing" if not actual else "mismatch"
                        ] += 1
                    card_quality["exact_display_name"] += card_actuals["fio"] == expected_item["fio"]
                    card_quality["exact_inn"] += card_actuals["inn"] == expected_item["inn"]
                    card_quality["exact_address"] += card_actuals["address"] == expected_card_values["address"]
                    card_quality["exact_case_number"] += card_actuals["case_number"] == expected_card_values["case_number"]
                    card_quality["exact_doc_date"] += card_actuals["doc_date"] == expected_card_values["doc_date"]
                deltas = {name: after_stats[name] - before_stats[name] for name in before_stats}
                result = {
                    "count": count,
                    "workers": workers,
                    "input_kinds": {
                        "text": sum(item.get("kind") == "text" for item in selected[count]),
                        "scan": sum(str(item.get("kind", "")).startswith("scan_") for item in selected[count]),
                    },
                    "upload_requests": len(upload_seconds),
                    "upload_request_seconds": upload_seconds,
                    "upload_wall_seconds": upload_wall,
                    "ocr_completion_after_upload_seconds": ocr_wall,
                    "end_to_end_seconds": total_wall,
                    "docs_per_minute": count * 60 / total_wall,
                    "task_p50_seconds": percentile(
                        [max(0.0, (row["updated_at"] - row["created_at"]).total_seconds()) for row in rows], 50
                    ),
                    "task_p95_seconds": percentile(
                        [max(0.0, (row["updated_at"] - row["created_at"]).total_seconds()) for row in rows], 95
                    ),
                    "task_read_query_mean_ms": statistics.fmean(db_reads) * 1000,
                    "task_read_query_p95_ms": percentile([value * 1000 for value in db_reads], 95),
                    "sampled_worker_cpu_pct_total": cpu,
                    "peak_worker_rss_mib_total": peak_rss / (1024 * 1024),
                    "db_stat_delta": deltas,
                    "terminal": {
                        "needs_review": sum(row["status"] == TaskStatus.needs_review.value for row in rows),
                        "failed": sum(row["status"] == TaskStatus.failed.value for row in rows),
                    },
                    "field_matches": matches,
                    "field_outcomes": field_outcomes,
                    "field_total": count * len(EXPECTED_FIELDS),
                    "field_matches_by_kind": matches_by_kind,
                    "internal_card_quality": card_quality,
                    "created_task_count": len(task_ids),
                    "worker_logs": worker_logs,
                }
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            finally:
                stop_workers(processes)
                for handle in handles:
                    handle.close()

    result = {
        "revision": args.revision,
        "run_id": args.run_id,
        "host": platform.platform(),
        "python": platform.python_version(),
        "app_debug": settings.debug,
        "omp_thread_limit": os.environ.get("OMP_THREAD_LIMIT"),
        "database_target": {"host": "loopback", "name": make_url(settings.database_url).database},
        "redis_target": {"host": "loopback", "port": urlsplit(settings.redis_url).port},
        "base_url": args.base_url,
        "corpus_manifest_sha256": sha256(manifest_path),
        "corpus_generator_sha256": sha256(ROOT / "scripts/dev/nightly/generate_corpus.py"),
        "benchmark_harness_sha256": sha256(Path(__file__).resolve()),
        "source_sha256": source_hashes(),
        "tesseract_version": subprocess.run(["tesseract", "--version"], capture_output=True, text=True).stdout.splitlines()[0],
        "pdftoppm_version": subprocess.run(["pdftoppm", "-v"], capture_output=True, text=True).stderr.splitlines()[0],
        "operator_user_id_synthetic": user_id,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("SUMMARY=" + json.dumps({key: value for key, value in result.items() if key != "results"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
