#!/usr/bin/env python3
"""Read back one 100-file synthetic batch and check source-to-field isolation.

Only aggregate counts are printed; recognized OCR text and individual values stay in memory.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db.base import SessionLocal
from app.db.models import Task, TaskStatus

FIELD_TO_RECOGNIZED = {
    "case_number": "case_number",
    "doc_date": "doc_date",
    "fio": "fio_raw",
    "inn": "inn",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--created-after-utc", required=True, help="ISO-8601 timestamp with timezone")
    args = parser.parse_args()
    created_after = datetime.fromisoformat(args.created_after_utc.replace("Z", "+00:00"))
    if created_after.tzinfo is None:
        raise SystemExit("--created-after-utc must include a timezone")

    manifest = json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))
    records = manifest.get("documents", [])
    if (
        manifest.get("generator") != "scripts/dev/nightly/generate_corpus.py"
        or len(records) != 100
        or len({record.get("sha256") for record in records}) != 100
        or any(not str(record.get("filename", "")).startswith("synthetic-") for record in records)
    ):
        raise SystemExit("Refusing: expected the pinned 100-file synthetic corpus")
    expected = {record["sha256"]: record for record in records}
    expected_values: dict[str, dict[str, set[str]]] = {
        field: defaultdict(set) for field in FIELD_TO_RECOGNIZED
    }
    for digest, record in expected.items():
        for field in FIELD_TO_RECOGNIZED:
            expected_values[field][str(record[field])].add(digest)

    with SessionLocal() as db:
        tasks = db.scalars(
            select(Task)
            .options(joinedload(Task.document), joinedload(Task.intake_item))
            .where(Task.created_at >= created_after, Task.intake_operation_id.is_not(None))
            .order_by(Task.created_at, Task.id)
        ).unique().all()

    by_operation: dict[int, list[Task]] = defaultdict(list)
    for task in tasks:
        if task.intake_operation_id is not None:
            by_operation[task.intake_operation_id].append(task)
    batches = [
        (operation_id, batch)
        for operation_id, batch in by_operation.items()
        if len(batch) == 20 and all(task.document.content_hash in expected for task in batch)
    ]
    if len(batches) != 5:
        raise SystemExit(f"Expected exactly five 20-file operations after the timestamp; found {len(batches)}")

    selected = [task for _, batch in batches for task in batch]
    hashes = [task.document.content_hash for task in selected]
    linked = sum(
        task.intake_item is not None
        and task.intake_item.document_id == task.document_id
        and task.intake_item.operation_id == task.intake_operation_id
        for task in selected
    )
    operation_indexes_ok = True
    exact_documents = 0
    field_matches = 0
    cross_document_matches = 0
    for _operation_id, batch in batches:
        indexes = [task.intake_item.item_index if task.intake_item else None for task in batch]
        operation_indexes_ok &= sorted(indexes) == list(range(20))
    for task in selected:
        digest = task.document.content_hash
        record = expected[digest]
        recognized = task.recognized or {}
        document_exact = True
        for expected_name, recognized_name in FIELD_TO_RECOGNIZED.items():
            actual = str((recognized.get(recognized_name) or {}).get("value") or "")
            matches = actual == str(record[expected_name])
            field_matches += matches
            document_exact &= matches
            if actual and not matches and any(
                other_digest != digest
                for other_digest in expected_values[expected_name].get(actual, set())
            ):
                cross_document_matches += 1
        exact_documents += document_exact

    result = {
        "corpus_manifest_sha256": __import__("hashlib").sha256((args.corpus / "manifest.json").read_bytes()).hexdigest(),
        "operations": len(batches),
        "tasks": len(selected),
        "distinct_source_hashes": len(set(hashes)),
        "source_links": linked,
        "operation_indexes_0_to_19": operation_indexes_ok,
        "all_needs_review": all(task.status == TaskStatus.needs_review for task in selected),
        "exact_documents": exact_documents,
        "field_matches": field_matches,
        "field_total": len(selected) * len(FIELD_TO_RECOGNIZED),
        "cross_document_expected_value_matches": cross_document_matches,
    }
    print(json.dumps(result, sort_keys=True))
    if (
        result["tasks"] != 100
        or result["distinct_source_hashes"] != 100
        or linked != 100
        or not operation_indexes_ok
        or not result["all_needs_review"]
        or cross_document_matches
    ):
        raise AssertionError("Synthetic batch source/field isolation check failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
