#!/usr/bin/env python3
"""Local parser-only quality checks. Never runs OCR or treats OCR as truth."""
from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import os
from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def use_revision(revision):
    """Load a verified Git revision in this disposable CLI process only."""
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"], cwd=ROOT, text=True,
    ).strip()
    package = importlib.import_module("sro_robot.core")
    for leaf in ("address", "person_attributes", "extract", "entity_extract"):
        name = "sro_robot.core." + leaf
        module = types.ModuleType(name)
        module.__package__ = "sro_robot.core"
        module.__file__ = str(ROOT / "sro_robot/core" / (leaf + ".py"))
        sys.modules[name] = module
        setattr(package, leaf, module)
        source = subprocess.check_output(
            ["git", "show", f"{revision}:sro_robot/core/{leaf}.py"], cwd=ROOT,
        )
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    return revision


def check(records):
    from sro_robot.core import extract, entity_extract, person_attributes
    counts = dict(correct=0, wrong=0, missing=0, correct_absence=0,
                  metadata_correct=0, metadata_wrong=0, unverified_documents=0)
    results = []
    started = time.perf_counter()
    for index, record in enumerate(records):
        variants = record.get("variants") or [record["text"]]
        kind = record.get("kind", "auto")
        if kind == "attributes":
            result = person_attributes.detect_person_attributes(variants[0], 0, len(variants[0]))
        elif kind == "entity" or (kind == "auto" and any(
            entity_extract.has_explicit_entity_marker(text) for text in variants
        )):
            result = entity_extract.parse_entity(variants)
        else:
            result = extract.parse(variants)
        snapshot = dataclasses.asdict(result)
        snapshot.pop("raw_text", None)
        values = {key: value.get("value") if isinstance(value, dict) and "value" in value else value
                  for key, value in snapshot.items()}
        expected = record.get("expected")
        # Only an explicit reference may contribute to quality counts. A
        # previous OCR/card is merely a comparison snapshot, never an oracle.
        if expected is None:
            counts["unverified_documents"] += 1
        else:
            if record.get("reference") != "synthetic-text":
                raise ValueError("Only independently authored synthetic text references are supported")
            for key, wanted in expected.items():
                actual = values.get(key)
                if isinstance(wanted, bool):
                    counts["metadata_correct" if actual is wanted else "metadata_wrong"] += 1
                elif wanted is None and actual is None:
                    counts["correct_absence"] += 1
                elif actual == wanted:
                    counts["correct"] += 1
                elif actual is None:
                    counts["missing"] += 1
                else:
                    counts["wrong"] += 1
        results.append(dict(index=index, snapshot=snapshot))
    return dict(documents=len(results), parser_seconds=time.perf_counter() - started,
                counts=counts, results=results)


def compare(before, after):
    if len(before["results"]) != len(after["results"]):
        raise ValueError("Snapshot document count differs")
    changed = lost = warning_removed = source_changed = lost_suggestions = metadata_changed = added_suggestions = 0
    for old, new in zip(before["results"], after["results"]):
        a, b = old["snapshot"], new["snapshot"]
        for key in set(a) | set(b):
            if isinstance(a.get(key), bool):
                metadata_changed += a.get(key) != b.get(key)
            if key.endswith("_location"):
                source_changed += a.get(key) != b.get(key)
            if (key in {"inn", "snils", "address", "address_kind", "birth_date", "birth_place"}
                    and not isinstance(a.get(key), dict) and not isinstance(b.get(key), dict)):
                changed += a.get(key) != b.get(key)
                lost += bool(a.get(key)) and not bool(b.get(key))
            if not isinstance(a.get(key), dict) or "value" not in a[key]:
                continue
            left, right = a[key], b.get(key, {})
            changed += left.get("value") != right.get("value")
            lost += bool(left.get("value")) and not bool(right.get("value"))
            warning_removed += bool(left.get("warning")) and not bool(right.get("warning"))
            source_changed += any(left.get(k) != right.get(k) for k in (
                "source", "source_variant", "source_start", "source_end", "source_page",
            ))
        # Full dictionaries include owner identity, all useful fields,
        # copyability, warnings and source spans. Do not compare only counts.
        old_suggestions = {json.dumps(x, sort_keys=True) for x in a.get("review_suggestions", [])}
        new_suggestions = {json.dumps(x, sort_keys=True) for x in b.get("review_suggestions", [])}
        lost_suggestions += len(old_suggestions - new_suggestions)
        added_suggestions += len(new_suggestions - old_suggestions)
    return dict(changed_fields=changed, lost_fields=lost, warnings_removed=warning_removed,
                source_changes=source_changed, changed_or_lost_suggestions=lost_suggestions,
                added_or_changed_suggestions=added_suggestions, metadata_changes=metadata_changed)


def capture_one(path):
    from sro_robot.core import extract, entity_extract, ocr
    import pytesseract
    attempts = []
    calls = 0
    original_run = pytesseract.pytesseract.run_tesseract
    def counted(*args, **kwargs):
        nonlocal calls
        result = original_run(*args, **kwargs)
        calls += 1
        return result
    pytesseract.pytesseract.run_tesseract = counted
    originals = [(module, name, getattr(module, name)) for module, name in (
        (extract, "parse"), (entity_extract, "parse_entity"),
    )]
    for module, name, original in originals:
        def wrapped(function):
            def invoke(texts):
                result = function(texts)
                attempts.append((result, list(texts)))
                return result
            return invoke
        setattr(module, name, wrapped(original))
    budget = ocr.OcrBudget(deadline_seconds=50, tesseract_timeout_seconds=20,
                           allow_native_tesseract=True)
    started = time.perf_counter()
    try:
        card = extract.parse_file(path, budget=budget)
    finally:
        pytesseract.pytesseract.run_tesseract = original_run
        for module, name, original in originals:
            setattr(module, name, original)
    seconds = time.perf_counter() - started
    observed = dataclasses.asdict(card)
    observed.pop("raw_text", None)
    accepted = [texts for parsed, texts in attempts if parsed is card]
    if not calls or len(accepted) != 1:
        raise ValueError("Sample capture did not complete real OCR")
    return dict(variants=accepted[0], capture=dict(seconds=seconds, completed_ocr=calls,
                parser_attempts=len(attempts), accepted_parser_identity=True),
                observed_pipeline=observed)


def capture_samples(output, revision):
    # Reuse the existing bounded process supervisor; no DB/Redis/app imports.
    from scripts.dev.tesseract_native import run_owned, resources, admit, LIMIT_ENV, write_json
    samples = sorted(p for p in (ROOT / "samples").rglob("*") if p.suffix.lower() in {".png", ".heic"})
    start_swap = resources()[1] if sys.platform == "darwin" else 0
    deadline = time.monotonic() + 180
    records = []
    for path in samples:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Local sample capture budget exhausted")
        if sys.platform == "darwin":
            admit(start_swap)
        with tempfile.TemporaryDirectory(prefix="capture-", dir=output.parent) as temporary:
            root = Path(temporary)
            target = root / ("input" + path.suffix.lower())
            shutil.copyfile(path, target)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("Sample copy hash mismatch")
            result_path = root / "result.json"
            command = [sys.executable, str(Path(__file__).resolve()), "--capture-one", "--input", str(target),
                       "--output", str(result_path)]
            if revision:
                command += ["--revision", revision]
            environment = {key: os.environ[key] for key in (
                "PATH", "HOME", "LANG", "TESSDATA_PREFIX", "SRO_TESSERACT_NATIVE",
            ) if key in os.environ}
            environment.update(LIMIT_ENV, TMPDIR=temporary)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Local sample capture budget exhausted")
            run_owned(command, timeout=min(60, remaining), env=environment,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      health=(lambda: admit(start_swap)) if sys.platform == "darwin" else None)
            record = json.loads(result_path.read_text())
            record["original"] = dict(path=str(path.relative_to(ROOT)), sha256=digest)
            records.append(record)
            # Preserve completed work if a later document reaches a limit.
            write_json(output, records)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "tests/fixtures/extraction_quality_cases.json")
    parser.add_argument("--revision", help="Read parser source from this commit, without changing Git")
    parser.add_argument("--output", type=Path, required=True, help="Private JSON under .local-run")
    parser.add_argument("--compare", type=Path, help="Earlier private snapshot of the identical input")
    parser.add_argument("--capture-samples", action="store_true", help="Fresh local OCR of existing samples, sequential, <=180 s")
    parser.add_argument("--capture-one", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / ".local-run").resolve()):
        parser.error("Output must stay under ignored .local-run; it can contain personal data")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    revision = use_revision(args.revision) if args.revision else "working-tree"
    if args.capture_samples:
        result = capture_samples(output, args.revision)
    elif args.capture_one:
        result = capture_one(args.input)
    else:
        data = args.input.read_bytes()
        if len(data) > 32 * 1024 * 1024:
            parser.error("Input exceeds local corpus bound")
        identity = hashlib.sha256(data).hexdigest()
        result = check(json.loads(data))
        result.update(input_sha256=identity, revision=revision, mode="parser-only; not OCR timing")
    if args.compare and not (args.capture_samples or args.capture_one):
        previous = json.loads(args.compare.read_text())
        if previous["input_sha256"] != identity:
            parser.error("Comparison input bytes differ")
        result["comparison"] = compare(previous, result)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=output.parent, delete=False, encoding="utf-8") as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    # Never print values, source excerpts, document paths or identifiers.
    if args.capture_samples:
        print(json.dumps(dict(documents=len(result), completed_ocr=sum(r["capture"]["completed_ocr"] for r in result),
                              processing_seconds=sum(r["capture"]["seconds"] for r in result), reference="unverified")))
    elif not args.capture_one:
        print(json.dumps({key: value for key, value in result.items() if key != "results"}))


if __name__ == "__main__":
    main()
