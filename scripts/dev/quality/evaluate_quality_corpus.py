#!/usr/bin/env python3
"""Evaluate a generated synthetic corpus and emit only aggregate evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.services.assignments import automatic_preparation_reason
from sro_robot.core.extract import parse_file


FIELDS = ("doc_type", "doc_date", "case_number", "fio", "inn")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, help="Optional path for compact JSON results")
    args = parser.parse_args()
    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("generator") != "scripts/dev/quality/generate_quality_corpus.py":
        raise SystemExit("Refusing: manifest was not produced by the local synthetic generator")
    field_counts = {field: {"exact": 0, "wrong_nonempty": 0, "missing": 0} for field in FIELDS}
    disposition_counts = {"exact": 0, "mismatch": 0}
    auto_counts = {"actual": 0, "expected": 0, "false_positive": 0, "false_negative": 0}
    incorrect_preparations = 0
    mismatches = []
    elapsed = []
    for item in manifest["documents"]:
        started = time.perf_counter()
        card = parse_file(args.manifest.parent / item["filename"])
        elapsed.append(time.perf_counter() - started)
        observed = {
            "doc_type": card.doc_type.value or "",
            "doc_date": card.doc_date.value or "",
            "case_number": card.case_number.value or "",
            "fio": card.fio_raw.value or "",
            "inn": card.inn.value or "",
        }
        for field in FIELDS:
            expected = item["expected"][field]
            actual = observed[field]
            if actual == expected:
                field_counts[field]["exact"] += 1
            elif actual:
                field_counts[field]["wrong_nonempty"] += 1
                mismatches.append({"file": item["filename"], "field": field, "result": "wrong_nonempty"})
            else:
                field_counts[field]["missing"] += 1
                if expected:
                    mismatches.append({"file": item["filename"], "field": field, "result": "missing"})
        disposition = "auto_prepare" if automatic_preparation_reason(card) is None else "manual_review"
        auto_counts["actual"] += disposition == "auto_prepare"
        auto_counts["expected"] += item["expected"]["disposition"] == "auto_prepare"
        auto_counts["false_positive"] += (
            disposition == "auto_prepare" and item["expected"]["disposition"] != "auto_prepare"
        )
        auto_counts["false_negative"] += (
            disposition != "auto_prepare" and item["expected"]["disposition"] == "auto_prepare"
        )
        if disposition == "auto_prepare" and (
            item["expected"]["disposition"] != "auto_prepare"
            or any(observed[field] != item["expected"][field] for field in FIELDS)
        ):
            incorrect_preparations += 1
            mismatches.append({"file": item["filename"], "field": "incorrect_auto_preparation", "result": "unsafe"})
        if disposition == item["expected"]["disposition"]:
            disposition_counts["exact"] += 1
        else:
            disposition_counts["mismatch"] += 1
            mismatches.append({"file": item["filename"], "field": "disposition", "result": "mismatch"})
    ordered = sorted(elapsed)
    report = {
        "split": manifest["split"],
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "documents": len(elapsed),
        "field_counts": field_counts,
        "disposition_counts": disposition_counts,
        "automatic_preparation_counts": auto_counts,
        "incorrect_auto_preparations": incorrect_preparations,
        "elapsed_seconds": round(sum(elapsed), 3),
        "latency_p50_seconds": round(statistics.median(elapsed), 3),
        "latency_p95_seconds": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
        "mismatches": mismatches,
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return int(bool(mismatches))


if __name__ == "__main__":
    raise SystemExit(main())
