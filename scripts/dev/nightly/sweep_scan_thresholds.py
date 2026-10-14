#!/usr/bin/env python3
"""Compare fixed local OCR thresholds on the generated synthetic scan corpus.

The report contains aggregate expected-field matches and timings only. OCR
variants and expected identifiers are never printed or written to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from sro_robot.core.extract import parse
from sro_robot.core.ocr import _recognize_scanned_pdf, load_pages


FIELDS = ("case_number", "doc_date", "fio", "inn")


def fields(card) -> dict[str, str]:
    return {
        "case_number": card.case_number.value or "",
        "doc_date": card.doc_date.value or "",
        "fio": card.fio_raw.value or "",
        "inn": card.inn.value or "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True,
                        help="Pin the exact synthetic input set before starting OCR")
    parser.add_argument("--cutoffs", nargs="+", type=int, default=[155, 170, 185, 200, 215])
    args = parser.parse_args()
    if any(not 1 <= cutoff <= 254 for cutoff in args.cutoffs) or len(set(args.cutoffs)) != len(args.cutoffs):
        raise SystemExit("Choose unique grayscale cutoffs from 1 to 254")
    manifest_bytes = args.manifest.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != args.expected_manifest_sha256:
        raise SystemExit("Refusing: manifest hash does not match the explicitly pinned synthetic input set")
    manifest = json.loads(manifest_bytes)
    if manifest.get("generator") != "scripts/dev/nightly/generate_corpus.py":
        raise SystemExit("Refusing: expected the generated nightly synthetic corpus manifest")
    records = [item for item in manifest.get("documents", []) if str(item.get("kind", "")).startswith("scan_")]
    if len(records) != 25 or len({item["sha256"] for item in records}) != 25:
        raise SystemExit("Refusing: expected 25 distinct generated synthetic scan PDFs")
    results = []
    for cutoff in args.cutoffs:
        counts = {field: 0 for field in FIELDS}
        exact = 0
        exact_by_kind = {kind: [0, 0] for kind in sorted({item["kind"] for item in records})}
        elapsed = []
        for record in records:
            path = args.manifest.parent / str(record["filename"])
            started = time.perf_counter()
            variants = _recognize_scanned_pdf(path, load_pages(path), cutoff=cutoff)
            observed = fields(parse(variants))
            elapsed.append(time.perf_counter() - started)
            expected = {field: str(record[field]) for field in FIELDS}
            for field in FIELDS:
                counts[field] += observed[field] == expected[field]
            matched = observed == expected
            exact += matched
            exact_by_kind[record["kind"]][0] += int(matched)
            exact_by_kind[record["kind"]][1] += 1
        results.append({
            "cutoff": cutoff,
            "documents": len(records),
            "exact_documents": exact,
            "field_matches": counts,
            "field_match_total": sum(counts.values()),
            "exact_by_kind": {kind: {"exact": values[0], "documents": values[1]}
                              for kind, values in exact_by_kind.items()},
            "elapsed_seconds": round(sum(elapsed), 3),
            "document_latency_p50_seconds": round(statistics.median(elapsed), 3),
            "document_latency_p95_seconds": round(sorted(elapsed)[23], 3),
        })
    print(json.dumps({"corpus_manifest_sha256": manifest_sha256,
                      "documents": len(records), "thresholds": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
