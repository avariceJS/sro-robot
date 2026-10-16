#!/usr/bin/env python3
"""Profile local OCR and extraction stages on five pinned synthetic PDFs.

This harness imports the requested source snapshot in a fresh process, reads
only files named and hashed in the generated corpus manifest, and has no app,
database, queue, or external-service dependencies.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_KINDS = {"text": 2, "scan_high": 1, "scan_medium": 1, "scan_low": 1}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0] if result.returncode == 0 else None


def select_inputs(corpus: Path) -> tuple[list[dict], str]:
    manifest_path = corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_kind: dict[str, list[dict]] = {}
    for item in manifest["documents"]:
        by_kind.setdefault(item["kind"], []).append(item)
    selected = []
    for kind, count in SAMPLE_KINDS.items():
        items = by_kind.get(kind, [])
        if len(items) < count:
            raise SystemExit(f"Pinned corpus lacks {count} inputs of kind {kind}")
        selected.extend(items[:count])
    for item in selected:
        path = corpus / item["filename"]
        if path.is_symlink() or not path.is_file() or sha256(path) != item["sha256"]:
            raise SystemExit(f"Refusing non-pinned or missing synthetic input: {path}")
    return selected, sha256(manifest_path)


def load_source(source_root: Path):
    source_root = source_root.resolve(strict=True)
    if not (source_root / "sro_robot/core/ocr.py").is_file():
        raise SystemExit("Source root must contain sro_robot/core/ocr.py")
    # The profiler itself can run from the working checkout while importing an
    # archived baseline. Force Python to resolve package imports from one tree.
    sys.path[:] = [str(source_root)] + [
        entry for entry in sys.path
        if not entry or Path(entry).resolve() != ROOT.resolve()
    ]
    ocr = importlib.import_module("sro_robot.core.ocr")
    extract = importlib.import_module("sro_robot.core.extract")
    loaded_from = Path(ocr.__file__).resolve()
    if source_root not in loaded_from.parents:
        raise SystemExit(f"Imported OCR module escaped requested source root: {loaded_from}")
    return ocr, extract


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=300)
    args = parser.parse_args()
    if args.dpi != 300:
        raise SystemExit("The reproducible component profile is pinned to 300 DPI")
    source_root = args.source_root.resolve(strict=True)
    selected, corpus_hash = select_inputs(args.corpus.resolve(strict=True))
    ocr, extract = load_source(source_root)
    component_names = ("ocr.py", "extract.py", "morph.py", "validators.py", "models.py")
    source_hashes = {
        f"sro_robot/core/{name}": sha256(source_root / "sro_robot/core" / name)
        for name in component_names
    }
    rows = []
    for item in selected:
        path = args.corpus / item["filename"]
        before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
        start_wall = time.perf_counter()
        variants = ocr.recognize(path, dpi=args.dpi)
        ocr_wall = time.perf_counter() - start_wall
        after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
        parse_cpu_start = time.process_time()
        parse_start = time.perf_counter()
        card = extract.parse(variants)
        parse_wall = time.perf_counter() - parse_start
        parse_cpu = time.process_time() - parse_cpu_start
        actual = {
            "fio": card.fio_raw.value,
            "inn": card.inn.value,
            "case_number": card.case_number.value,
            "doc_date": card.doc_date.value,
        }
        expected = {name: item[name] for name in actual}
        rows.append({
            "filename": item["filename"],
            "kind": item["kind"],
            "pages": item["pages"],
            "input_sha256": item["sha256"],
            "ocr_wall_seconds": round(ocr_wall, 6),
            "ocr_child_cpu_seconds": round(
                (after_children.ru_utime + after_children.ru_stime)
                - (before_children.ru_utime + before_children.ru_stime), 6
            ),
            "ocr_variant_count": len(variants),
            "ocr_character_count": sum(map(len, variants)),
            "extraction_wall_seconds": round(parse_wall, 6),
            "extraction_process_cpu_seconds": round(parse_cpu, 6),
            "field_exact_counts": {
                name: {"exact": int(actual[name] == expected[name]), "expected": expected[name], "actual": actual[name]}
                for name in actual
            },
            "total_local_wall_seconds": round(ocr_wall + parse_wall, 6),
        })

    aggregate = {}
    for kind in SAMPLE_KINDS:
        group = [row for row in rows if row["kind"] == kind]
        aggregate[kind] = {
            "documents": len(group),
            "ocr_wall_seconds": round(sum(row["ocr_wall_seconds"] for row in group), 6),
            "extraction_wall_seconds": round(sum(row["extraction_wall_seconds"] for row in group), 6),
            "total_local_wall_seconds": round(sum(row["total_local_wall_seconds"] for row in group), 6),
            "exact_fields": {
                field: sum(row["field_exact_counts"][field]["exact"] for row in group)
                for field in ("fio", "inn", "case_number", "doc_date")
            },
        }
    result = {
        "profile_kind": "isolated_ocr_and_extraction_components",
        "label": args.label,
        "synthetic_only": True,
        "source_root": str(source_root),
        "source_sha256": source_hashes,
        "profiler_sha256": sha256(Path(__file__).resolve()),
        "dpi": args.dpi,
        "host": platform.platform(),
        "python": platform.python_version(),
        "tesseract": version(["tesseract", "--version"]),
        "poppler_pdftoppm": version(["pdftoppm", "-v"]),
        "corpus_manifest_sha256": corpus_hash,
        "sample_plan": SAMPLE_KINDS,
        "documents": rows,
        "aggregate_by_kind": aggregate,
        "scope_note": "OCR includes local PDF text-layer extraction or local pdftoppm/Tesseract subprocesses; extraction is parse(variants) only. No app, DB, RQ, or card-write transaction is included.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
