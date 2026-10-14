#!/usr/bin/env python3
"""Compare the current local OCR path with a thresholded synthetic scan pass.

Only aggregate field accuracy and timings are emitted; OCR text is never saved.
The input manifest must be the generated nightly synthetic corpus manifest.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from sro_robot.core.extract import parse
from sro_robot.core.ocr import _recognize_legacy_pages, load_pages, recognize


def output_fields(card) -> dict[str, str]:
    return {
        "case_number": card.case_number.value or "",
        "doc_date": card.doc_date.value or "",
        "fio": card.fio_raw.value or "",
        "inn": card.inn.value or "",
    }


def summarize(records: list[dict], actual: list[dict[str, str]], elapsed: list[float]) -> dict:
    matches = {key: 0 for key in ("case_number", "doc_date", "fio", "inn")}
    exact = 0
    for expected, observed in zip(records, actual, strict=True):
        expected_fields = {
            "case_number": str(expected["case_number"]),
            "doc_date": str(expected["doc_date"]),
            "fio": str(expected["fio"]),
            "inn": str(expected["inn"]),
        }
        for key in matches:
            matches[key] += observed[key] == expected_fields[key]
        exact += observed == expected_fields
    return {
        "documents": len(records),
        "exact_documents": exact,
        "field_matches": matches,
        "field_total": len(records) * len(matches),
        "elapsed_seconds": round(sum(elapsed), 3),
        "document_latency_p50_seconds": round(statistics.median(elapsed), 3),
        "document_latency_p95_seconds": round(sorted(elapsed)[math.ceil(0.95 * len(elapsed)) - 1], 3),
    }


def run(records: list[dict], corpus: Path, mode: str) -> dict:
    outputs = []
    elapsed = []
    for record in records:
        path = corpus / str(record["filename"])
        started = time.perf_counter()
        if mode == "legacy":
            variants = _recognize_legacy_pages(path, load_pages(path))
        else:
            variants = recognize(path)
        outputs.append(output_fields(parse(variants)))
        elapsed.append(time.perf_counter() - started)
    return summarize(records, outputs, elapsed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--kinds", nargs="+", choices=("scan_high", "scan_medium", "scan_low"), default=["scan_low"])
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("generator") != "scripts/dev/nightly/generate_corpus.py":
        raise SystemExit("Refusing: expected the nightly synthetic corpus manifest")
    records = [item for item in manifest["documents"] if item["kind"] in args.kinds]
    if (
        not records
        or any(not str(item["filename"]).startswith("synthetic-") for item in records)
        or len({item["sha256"] for item in records}) != len(records)
    ):
        raise SystemExit("Refusing: select unique generated synthetic scan PDFs")

    results = {mode: run(records, args.manifest.parent, mode) for mode in ("legacy", "scan_pdf_binary")}
    print(json.dumps({"corpus_manifest": str(args.manifest), "kinds": args.kinds, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
