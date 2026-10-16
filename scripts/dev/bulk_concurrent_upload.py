#!/usr/bin/env python3
"""Exercise 20 concurrent synthetic employees uploading two PDFs each."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from itsdangerous import TimestampSigner
from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models import DebtorCardDraft, IntakeOperation, Task, TaskStatus, User
from app.services import auth
from app.settings import settings
from scripts.dev.bulk_benchmark import (
    multipart,
    post_upload,
    require_empty_queue,
    require_safe_runtime,
)


def employee_cookie(index: int) -> tuple[str, int]:
    login = f"synthetic-bulk-employee-{index:02d}"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.login == login))
        if user is None:
            user = auth.create_user(
                db, login, f"synthetic-only-password-{index:02d}",
                f"Synthetic Bulk Employee {index:02d}", "",
            )
        token = secrets.token_urlsafe(32)
        row = auth.new_login_session(db, user.id, token)
        state = json.dumps(
            {
                "cookies": [{
                    "name": "synthetic-benchmark-only", "value": "no-external-login",
                    "domain": "sroarbitr.sro-service.ru", "path": "/", "expires": -1,
                    "httpOnly": True, "secure": True, "sameSite": "Lax",
                }],
                "origins": [],
            },
            separators=(",", ":"),
        ).encode()
        auth.attach_external_session(db, row, state, settings.secret_key, auth.SESSION_MAX_AGE_SECONDS)
        db.commit()
        user_id = user.id
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_token": token}).encode())
    return TimestampSigner(settings.secret_key).sign(payload).decode("ascii"), user_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18084")
    parser.add_argument("--employees", type=int, default=20)
    parser.add_argument("--documents-per-employee", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_safe_runtime(args.base_url)
    if (args.employees, args.documents_per_employee) != (20, 2):
        raise SystemExit("This bounded concurrency check is defined as 20 employees × 2 documents")
    require_empty_queue()
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(Task)) or db.scalar(
            select(func.count()).select_from(IntakeOperation)
        ):
            raise SystemExit("Concurrent employee benchmark requires a fresh database")

    manifest_path = args.corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    docs = manifest["documents"]
    texts = [item for item in docs if item.get("kind") == "text"][:20]
    scans = [item for item in docs if str(item.get("kind", "")).startswith("scan_")][:20]
    selected = [item for pair in zip(texts, scans) for item in pair]
    if len(selected) != 40:
        raise SystemExit("Corpus cannot supply 20 unique text/scan employee pairs")

    actors = [employee_cookie(index) for index in range(1, args.employees + 1)]
    before = time.monotonic()
    futures = {}
    with ThreadPoolExecutor(max_workers=args.employees) as executor:
        for employee_index, (cookie, user_id) in enumerate(actors, start=1):
            batch = selected[(employee_index - 1) * 2 : employee_index * 2]
            operation_key = hashlib.sha256(f"synthetic-employee-{employee_index}".encode()).hexdigest()[:48]
            body, content_type = multipart(batch, args.corpus, operation_key)
            future = executor.submit(post_upload, args.base_url, cookie, body, content_type)
            futures[future] = (employee_index, user_id)
        responses = [
            {"employee": futures[future][0], "user_id": futures[future][1], "http_status": future.result()}
            for future in as_completed(futures)
        ]
    upload_seconds = time.monotonic() - before
    if any(item["http_status"] != 200 for item in responses):
        raise RuntimeError(f"Concurrent upload HTTP failures: {responses}")

    deadline = time.monotonic() + 1200
    rows: list[Task] = []
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            rows = db.scalars(select(Task).order_by(Task.id)).all()
            if len(rows) == 40 and all(row.status in {TaskStatus.needs_review, TaskStatus.failed} for row in rows):
                break
        time.sleep(0.25)
    if len(rows) != 40 or any(row.status not in {TaskStatus.needs_review, TaskStatus.failed} for row in rows):
        raise TimeoutError("Not all employee batch tasks reached terminal OCR status")

    with SessionLocal() as db:
        operations = db.scalars(select(IntakeOperation).order_by(IntakeOperation.id)).all()
        cards = db.scalars(select(DebtorCardDraft)).all()
        tasks_by_user = {
            user_id: db.scalar(
                select(func.count()).select_from(Task).join(IntakeOperation).where(
                    IntakeOperation.user_id == user_id
                )
            )
            for _cookie, user_id in actors
        }
        task_data = db.scalars(select(Task).order_by(Task.id)).all()
        field_counts = {name: {"exact": 0, "missing": 0, "mismatch": 0} for name in ("fio_raw", "inn", "case_number", "doc_date")}
        by_hash = {item["sha256"]: item for item in selected}
        from app.db.models import Document
        documents = {
            task.id: db.get(Document, task.document_id)
            for task in task_data
        }
        for task in task_data:
            expected = by_hash[documents[task.id].content_hash]
            for name in field_counts:
                value = ((task.recognized or {}).get(name) or {}).get("value")
                expected_name = {"fio_raw": "fio", "inn": "inn", "case_number": "case_number", "doc_date": "doc_date"}[name]
                field_counts[name]["exact" if value == expected[expected_name] else "missing" if not value else "mismatch"] += 1
        card_fields = {name: {"exact": 0, "missing": 0, "mismatch": 0} for name in ("fio", "inn", "address", "case_number", "doc_date")}
        by_task_id = {task.id: task for task in task_data}
        expected_card_address = "101000, вымышленный город Макетный, улица Учебная, дом 1"
        for card in cards:
            source_task = by_task_id[card.source_task_id]
            expected = by_hash[documents[source_task.id].content_hash]
            actuals = {
                "fio": card.display_name,
                "inn": card.inn,
                "address": ((card.values or {}).get("address") or {}).get("value"),
                "case_number": ((card.values or {}).get("case_number") or {}).get("value"),
                "doc_date": ((card.values or {}).get("doc_date") or {}).get("value"),
            }
            expected_values = {
                "fio": expected["fio"], "inn": expected["inn"], "address": expected_card_address,
                "case_number": expected["case_number"], "doc_date": expected["doc_date"],
            }
            for name, value in actuals.items():
                expected_value = expected_values[name]
                card_fields[name]["exact" if value == expected_value else "missing" if not value else "mismatch"] += 1

    elapsed = time.monotonic() - before
    result = {
        "synthetic_only": True,
        "employees": args.employees,
        "documents_per_employee": args.documents_per_employee,
        "documents": len(rows),
        "unique_owners": len({operation.user_id for operation in operations}),
        "task_counts_per_employee": sorted(set(tasks_by_user.values())),
        "concurrent_upload_http_requests": len(responses),
        "http_status_counts": {str(code): sum(item["http_status"] == code for item in responses) for code in (200, 400, 409, 429, 500)},
        "upload_wall_seconds": upload_seconds,
        "end_to_end_seconds": elapsed,
        "docs_per_minute": len(rows) * 60 / elapsed,
        "terminal": {
            "needs_review": sum(row.status == TaskStatus.needs_review for row in rows),
            "failed": sum(row.status == TaskStatus.failed for row in rows),
        },
        "recognized_field_outcomes": field_counts,
        "auto_cards_created": len(cards),
        "prepared_cards": sum(card.preparation_state == "prepared" for card in cards),
        "auto_card_field_outcomes": card_fields,
        "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "database_name": settings.database_url.rsplit("/", 1)[-1],
        "host": platform.platform(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
