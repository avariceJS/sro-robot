#!/usr/bin/env python3
"""Create or evaluate a private, human-verified local document-quality set.

This command stays offline. Gold labels and source names must remain outside
the repository. Reports contain only aggregate counts and opaque file IDs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.services.assignments import automatic_preparation_reason
from sro_robot.core.extract import parse_file

FIELDS = ("doc_type", "doc_date", "case_number", "fio_raw", "inn", "address")
FIELD_LABELS = {
    "doc_type": "document type",
    "doc_date": "document date (DD.MM.YYYY)",
    "case_number": "case number",
    "fio_raw": "debtor full name; null if absent/ambiguous",
    "inn": "debtor INN; null if absent/ambiguous",
    "address": "debtor address; null if absent/ambiguous",
}
EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def private_outside_repo(path: Path) -> Path:
    if path.expanduser().is_symlink():
        raise SystemExit("Refusing symlinked labels or report path")
    target = path.expanduser().resolve()
    if target == ROOT or ROOT in target.parents:
        raise SystemExit("Refusing to store office labels/reports inside the repository")
    return target


def initialize(directory: Path, labels_path: Path) -> int:
    source = directory.expanduser().resolve()
    if not source.is_dir():
        raise SystemExit("Office document directory was not found")
    target = private_outside_repo(labels_path)
    documents = []
    for path in sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS):
        documents.append({
            "id": digest(path),
            "filename": path.name,
            "sha256": digest(path),
            "confirmed_fields": [],
            "expected": {field: None for field in FIELDS},
            "expected_disposition": None,
        })
    if not documents:
        raise SystemExit("No supported office documents were found")
    if target.exists():
        raise SystemExit("Refusing to overwrite an existing gold-label file")
    target.parent.mkdir(parents=True, exist_ok=True)
    template = {
        "format": "sro-office-quality-gold-v1",
        "reviewer": "",
        "independent_visual_review": False,
        "documents": documents,
        "labeling_guide": FIELD_LABELS,
    }
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(template, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"template_created": True, "documents": len(documents), "labels_path": str(target)}))
    return 0


def evaluate(directory: Path, labels_path: Path, report_path: Path) -> int:
    source = directory.expanduser().resolve()
    labels_file = private_outside_repo(labels_path)
    report_file = private_outside_repo(report_path)
    if not source.is_dir() or labels_file.is_symlink() or not labels_file.is_file():
        raise SystemExit("Source directory or private gold-label file is unavailable")
    if stat.S_IMODE(labels_file.stat().st_mode) & 0o077:
        raise SystemExit("Gold-label file permissions must be 0600 or stricter")
    if report_file.exists() and stat.S_IMODE(report_file.stat().st_mode) & 0o077:
        raise SystemExit("Existing report file permissions must be 0600 or stricter")
    labels = json.loads(labels_file.read_text(encoding="utf-8"))
    if labels.get("format") != "sro-office-quality-gold-v1":
        raise SystemExit("Unsupported office gold-label format")
    if not labels.get("reviewer", "").strip() or labels.get("independent_visual_review") is not True:
        raise SystemExit("Gold labels require a named human reviewer and independent visual confirmation")
    documents = {digest(p): p for p in source.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS}
    field_counts = {field: {"exact": 0, "wrong_nonempty": 0, "missing": 0} for field in FIELDS}
    disposition = {"exact": 0, "mismatch": 0}
    measured = 0
    elapsed = 0.0
    opaque_results = []
    for item in labels.get("documents", []):
        file_id = item.get("id")
        path = documents.get(file_id)
        if path is None or digest(path) != item.get("sha256"):
            raise SystemExit("A labeled source is missing or its SHA-256 changed; no field data was emitted")
        confirmed = set(item.get("confirmed_fields", []))
        if not confirmed.issubset(FIELDS):
            raise SystemExit("Unknown confirmed field in gold labels")
        if not confirmed or item.get("expected_disposition") not in {"auto_prepare", "manual_review"}:
            raise SystemExit("Each document needs confirmed field labels and an expected disposition")
        expected = item.get("expected", {})
        if any(
            field not in expected
            or (expected[field] is not None and not isinstance(expected[field], str))
            for field in confirmed
        ):
            raise SystemExit("Confirmed fields must have a string value or an explicitly confirmed null")
        started = time.perf_counter()
        card = parse_file(path)
        elapsed += time.perf_counter() - started
        present = {}
        for field in confirmed:
            expected_value = expected[field]
            actual_field = getattr(card, field)
            actual = actual_field.value or ""
            present[field] = bool(actual)
            if actual == (expected_value or ""):
                field_counts[field]["exact"] += 1
            elif actual:
                field_counts[field]["wrong_nonempty"] += 1
            else:
                field_counts[field]["missing"] += 1
        actual_disposition = "auto_prepare" if automatic_preparation_reason(card) is None else "manual_review"
        disposition["exact" if actual_disposition == item["expected_disposition"] else "mismatch"] += 1
        measured += 1
        opaque_results.append({"document_id": hashlib.sha256(file_id.encode()).hexdigest()[:12],
                               "confirmed_fields": sorted(confirmed), "values_present": present,
                               "decision": "exact" if actual_disposition == item["expected_disposition"] else "mismatch"})
    if measured != len(labels.get("documents", [])):
        raise SystemExit("Gold-label document list is malformed")
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "format": "sro-office-quality-report-v1",
        "human_reviewed_gold": True,
        "field_counts": field_counts,
        "disposition_counts": disposition,
        "elapsed_seconds": round(elapsed, 3),
        "documents": measured,
        "per_document": opaque_results,
    }
    fd = os.open(report_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"report_written": True, "documents": measured, "field_counts": field_counts,
                      "disposition_counts": disposition, "elapsed_seconds": round(elapsed, 3)}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    init = subparsers.add_parser("init", help="create a private, blank human-label template")
    init.add_argument("--documents-dir", type=Path, required=True)
    init.add_argument("--labels", type=Path, required=True)
    run = subparsers.add_parser("evaluate", help="run a locally verified gold set")
    run.add_argument("--documents-dir", type=Path, required=True)
    run.add_argument("--labels", type=Path, required=True)
    run.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "init":
        return initialize(args.documents_dir, args.labels)
    return evaluate(args.documents_dir, args.labels, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
