#!/usr/bin/env python3
"""Bounded private PDF raster OCR experiments; stdout is aggregate-only.

Embedded PDF text is a qualified *weak reference*, never human gold. Rendered
pages and controlled distortions are synthetic strata, never camera photographs.
No application, database, acquisition client or remote recognizer is started.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
VARIANTS = ("clean", "tilt", "shadow", "blur", "perspective", "lighting")
MAX_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 12_000_000
MAX_RESEARCH_PAGES = 600
SCHEMA = 1
RECOGNIZERS = {"baseline": "--psm 3", "sauvola": "--psm 3 -c thresholding_method=2"}


def normalize(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def edit_distance(reference, observed):
    """Exact unit-cost Levenshtein via a bounded Python-integer bit vector.

    Unlike a quadratic Python matrix, this also handles full act pages without
    silently sampling text. Tokens may be characters, words, or whole lines.
    """
    if max(len(reference), len(observed)) > 100_000:
        raise ValueError("research_metric_work_bound")
    if len(reference) > len(observed):
        reference, observed = observed, reference
    size = len(reference)
    if not size:
        return len(observed)
    positions = {}
    for index, token in enumerate(reference):
        positions[token] = positions.get(token, 0) | (1 << index)
    positive, negative, score = (1 << size) - 1, 0, size
    high = 1 << (size - 1)
    mask = (1 << size) - 1
    for token in observed:
        equal = positions.get(token, 0)
        vertical = equal | negative
        horizontal = (((equal & positive) + positive) ^ positive) | equal
        plus = negative | ~(horizontal | positive)
        minus = positive & horizontal
        score += bool(plus & high) - bool(minus & high)
        plus, minus = (plus << 1) | 1, minus << 1
        positive = (minus | ~(vertical | plus)) & mask
        negative = plus & vertical
    return score


def measure(reference, observed):
    a, b = normalize(reference), normalize(observed)
    streams = {"characters": (a, b), "words": (a.split(), b.split()),
               "digits": ("".join(c for c in a if c.isdigit()), "".join(c for c in b if c.isdigit())),
               "letters": ("".join(c for c in a if c.isalpha()), "".join(c for c in b if c.isalpha())),
               "lines": ([normalize(s) for s in reference.splitlines() if normalize(s)],
                         [normalize(s) for s in observed.splitlines() if normalize(s)])}
    return {key: {"edits": edit_distance(left, right), "reference_units": len(left),
                  "observed_units": len(right)} for key, (left, right) in streams.items()}


def distort(image, variant):
    from PIL import Image, ImageChops, ImageEnhance, ImageFilter
    source = image.convert("RGB")
    if variant == "clean":
        return source
    if variant == "tilt":
        return source.rotate(2.5, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    if variant == "blur":
        return source.filter(ImageFilter.GaussianBlur(1.2))
    if variant == "shadow":
        gradient = Image.new("L", (256, 1))
        gradient.putdata([int(255 * (.35 + .65 * x / 255)) for x in range(256)])
        return ImageChops.multiply(source, gradient.resize(source.size).convert("RGB"))
    if variant == "lighting":
        return ImageEnhance.Contrast(ImageEnhance.Brightness(source).enhance(.65)).enhance(.60)
    if variant == "perspective":
        width, height = source.size
        return source.transform(source.size, Image.Transform.PERSPECTIVE,
                                (.94, .02, .02 * width, -.01, .93, .025 * height,
                                 .025 / width, -.025 / height),
                                resample=Image.Resampling.BICUBIC, fillcolor="white")
    raise ValueError("research_variant_invalid")


def bbox_reference(xml):
    tree = ET.fromstring(xml)
    pages = [node for node in tree.iter() if node.tag.rsplit("}", 1)[-1] == "page"]
    if len(pages) != 1:
        raise ValueError("research_page_identity")
    page = pages[0]
    width, height = float(page.attrib["width"]), float(page.attrib["height"])
    if not (1 <= width <= 14400 and 1 <= height <= 14400):
        raise ValueError("research_page_geometry")
    lines, boxes = [], []
    for line in page.iter():
        if line.tag.rsplit("}", 1)[-1] != "line":
            continue
        words = []
        for word in line:
            if word.tag.rsplit("}", 1)[-1] != "word":
                continue
            value = "".join(word.itertext())
            words.append(value)
            boxes.append(tuple(float(word.attrib[key]) for key in ("xMin", "yMin", "xMax", "yMax")))
        if words:
            lines.append(" ".join(words))
    return "\n".join(lines), boxes, (width, height)


def qualify(reference, boxes, page_size, image):
    """Reject unusable layers; ink overlap is alignment evidence, not truth."""
    text = normalize(reference)
    alnum = sum(c.isalnum() for c in text)
    invalid = sum(c == "\ufffd" or (unicodedata.category(c).startswith("C") and not c.isspace()) for c in text)
    width, height = page_size
    valid = [box for box in boxes if all(math.isfinite(x) for x in box)
             and 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height]
    samples = valid[::max(1, math.ceil(len(valid) / 64))]
    ink = 0
    gray = image.convert("L")
    for left, top, right, bottom in samples:
        crop = gray.crop((int(left * image.width / width), int(top * image.height / height),
                          math.ceil(right * image.width / width), math.ceil(bottom * image.height / height)))
        histogram = crop.histogram()
        dark_fraction = sum(histogram[:180]) / max(1, crop.width * crop.height)
        ink += .008 <= dark_fraction <= .85
    alignment = ink / len(samples) if samples else 0
    reasons = []
    if len(text) < 120 or len(text) > 100_000 or alnum / max(1, len(text)) < .55 or invalid:
        reasons.append("unusable_text")
    if len(valid) < 20 or len(valid) / max(1, len(boxes)) < .98:
        reasons.append("invalid_geometry")
    if alignment < .80:
        reasons.append("weak_visible_alignment")
    return {"eligible": not reasons, "reasons": reasons, "characters": len(text),
            "word_boxes": len(boxes), "sampled_boxes": len(samples), "ink_overlap_fraction": alignment,
            "reference_kind": "embedded-text-weak", "human_verified": False}


def fingerprint():
    from scripts.dev.quality.dataset_capture import digest, runtime_identity
    from sro_robot.core.native_tesseract import configure_native_tesseract
    import pytesseract
    configure_native_tesseract()
    scope = [Path(__file__), ROOT / "sro_robot/core/ocr.py", ROOT / "sro_robot/core/native_tesseract.py",
             ROOT / "scripts/dev/quality/dataset_capture.py", ROOT / "scripts/dev/tesseract_native.py"]
    return {"source_sha256": hashlib.sha256("".join(digest(p) for p in scope).encode()).hexdigest(),
            "runtime": runtime_identity([str(pytesseract.pytesseract.tesseract_cmd)])}


def capture_page(source, page, dpi, variants, recognizers=("baseline",)):
    from pdf2image import convert_from_path
    from sro_robot.core import ocr
    from scripts.dev.quality.dataset_capture import digest
    before = digest(source)
    with tempfile.TemporaryDirectory() as temporary:
        layer = Path(temporary) / "layer.xml"
        subprocess.run(["pdftotext", "-f", str(page), "-l", str(page), "-bbox-layout", str(source), str(layer)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True)
        if layer.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("research_text_layer_bound")
        reference, boxes, geometry = bbox_reference(layer.read_bytes())
    if math.ceil(geometry[0] * dpi / 72) * math.ceil(geometry[1] * dpi / 72) > MAX_PIXELS:
        raise ValueError("research_raster_bound")
    images = convert_from_path(source, dpi=dpi, first_page=page, last_page=page,
                               thread_count=1, timeout=15)
    if len(images) != 1 or images[0].width * images[0].height > MAX_PIXELS:
        raise ValueError("research_raster_bound")
    image = images[0]
    qualification = qualify(reference, boxes, geometry, image)
    result = {"schema": SCHEMA, "input_sha256": before, "page": page,
              "qualification": qualification, "reference": reference, "readings": [], "fingerprint": fingerprint(),
              "raster_sha256": hashlib.sha256(str(image.size).encode() + image.convert("RGB").tobytes()).hexdigest()}
    if qualification["eligible"]:
        budget = ocr.OcrBudget(deadline_seconds=50, tesseract_timeout_seconds=20, allow_native_tesseract=True)
        for variant in variants:
            raster = distort(image, variant)
            for recognizer in recognizers:
                started = time.monotonic()
                reading = ocr._tesseract(raster, RECOGNIZERS[recognizer], budget)
                result["readings"].append({"variant": variant, "recognizer": recognizer,
                                           "config": RECOGNIZERS[recognizer],
                                           "provenance": "rendered-pdf" if variant == "clean" else "synthetic-distortion",
                                           "seconds": time.monotonic() - started, "text": reading,
                                           "metrics": measure(reference, reading)})
    if before != digest(source):
        raise ValueError("research_input_changed")
    return result


def aggregate(records):
    strata = {}
    excluded = Counter()
    unique, seen = [], set()
    for index, record in enumerate(records):
        key = record.get("raster_sha256", ("missing-raster-id", index))
        if key not in seen:
            seen.add(key)
            unique.append(record)
    for record in unique:
        if not record.get("qualification", {}).get("eligible"):
            excluded.update(record.get("qualification", {}).get("reasons", ["processing_failed"]))
        for reading in record.get("readings", []):
            key = reading["variant"]
            if reading.get("recognizer", "baseline") != "baseline":
                key += "+" + reading["recognizer"]
            group = strata.setdefault(key, {"pages": 0, "seconds": 0., "metrics": {}})
            group["pages"] += 1
            group["seconds"] += reading["seconds"]
            for name, metric in reading["metrics"].items():
                combined = group["metrics"].setdefault(name, Counter())
                combined.update(metric)
    for group in strata.values():
        group["seconds"] = round(group["seconds"], 3)
        for metric in group["metrics"].values():
            metric["rate"] = metric["edits"] / metric["reference_units"] if metric["reference_units"] else None
    paired = {}
    for record in unique:
        readings = {(r["variant"], r.get("recognizer", "baseline")): r for r in record.get("readings", [])}
        for variant in VARIANTS:
            if (variant, "baseline") in readings and (variant, "sauvola") in readings:
                before = readings[variant, "baseline"]["metrics"]["characters"]["edits"]
                after = readings[variant, "sauvola"]["metrics"]["characters"]["edits"]
                counts = paired.setdefault(variant, Counter())
                counts.update(pages=1, baseline_edits=before, candidate_edits=after,
                              reference_characters=readings[variant, "baseline"]["metrics"]["characters"]["reference_units"],
                              improved_pages=int(after < before), regressed_pages=int(after > before), equal_pages=int(after == before))
    return {"reference_kind": "embedded-text-weak", "human_verified_pages": 0,
            "population_accuracy_qualified": False, "real_photo_pages": 0, "page_records": len(records),
            "unique_raster_pages": len(unique), "duplicate_raster_pages": len(records) - len(unique),
            "independent_cases": None, "case_identity_status": "unreviewed",
            "excluded_reasons": dict(excluded), "strata": strata, "paired_sauvola": paired}


def record_digest(record):
    payload = {key: value for key, value in record.items() if key != "payload_sha256"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_record(record, sha, page, config):
    if (record.get("schema") != SCHEMA or record.get("input_sha256") != sha or record.get("page") != page
            or record.get("fingerprint") != config["fingerprint"]
            or record.get("payload_sha256") != record_digest(record)
            or record.get("qualification", {}).get("reference_kind") != "embedded-text-weak"
            or record.get("qualification", {}).get("human_verified") is not False):
        raise ValueError("research_checkpoint_identity")
    expected = {(variant, recognizer) for variant in config["variants"] for recognizer in config["recognizers"]}
    readings = record.get("readings", [])
    observed = {(reading["variant"], reading["recognizer"]) for reading in readings}
    if ((record["qualification"].get("eligible") and (observed != expected or len(readings) != len(expected)))
            or (not record["qualification"].get("eligible") and readings)):
        raise ValueError("research_checkpoint_readings")
    return record


def run_batch(args, root):
    from scripts.dev.quality.court_dataset import dataset_lock, write_json
    from scripts.dev.quality.dataset_capture import digest
    from scripts.dev.tesseract_native import LIMIT_ENV, run_owned, resources, admit
    source_root = args.input_dir.expanduser().resolve(strict=True)
    if (source_root.is_relative_to(ROOT) or ROOT.is_relative_to(source_root)
            or source_root == Path(source_root.anchor) or not source_root.is_dir()):
        raise ValueError("research_source_boundary")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.output_name):
        raise ValueError("research_output_name")
    if (not 1 <= args.limit_documents <= 200 or not 1 <= args.pages_per_document <= 60
            or not 72 <= args.dpi <= 300 or not 1 <= args.batch_seconds <= 240):
        raise ValueError("research_limit_invalid")
    if args.recognizers not in ("baseline", "baseline,sauvola"):
        raise ValueError("research_recognizers_invalid")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name == args.output_name for name in args.exclude_run):
        raise ValueError("research_excluded_run_invalid")
    deadline = time.monotonic() + args.batch_seconds
    target = root / args.output_name
    target.mkdir(mode=0o700, exist_ok=True)
    with dataset_lock(root):
        config_path = target / "cohort.json"
        frozen = json.loads(config_path.read_text()) if config_path.exists() else None
        excluded_hashes = set()
        for name in args.exclude_run:
            prior = json.loads((root / name / "cohort.json").read_text())
            excluded_hashes.update(entry["sha256"] for entry in prior["inputs"])
        if frozen:
            # A live acquisition directory may grow; resume exactly the original
            # input plan rather than admitting newly downloaded acts mid-run.
            inputs = frozen["inputs"]
            excluded_count = frozen["excluded_input_files"]
            paths = [Path(entry["path"]) for entry in inputs]
        else:
            inputs, paths, seen = [], [], set(excluded_hashes)
            excluded_count = 0
            for path in source_root.rglob("*.pdf"):
                if len(paths) >= args.limit_documents or time.monotonic() >= deadline:
                    break
                if (path.is_symlink() or not path.resolve().is_relative_to(source_root)
                        or path.stat().st_size > MAX_BYTES):
                    raise ValueError("research_input_bound")
                sha = digest(path)
                if sha in seen:
                    excluded_count += 1
                    continue
                seen.add(sha)
                inputs.append({"path": str(path), "sha256": sha})
                paths.append(path)
        if not paths:
            raise ValueError("research_inputs_missing")
        if len(paths) * args.pages_per_document > MAX_RESEARCH_PAGES:
            raise ValueError("research_total_page_bound")
        for path, entry in zip(paths, inputs):
            if (path.is_symlink() or not path.resolve().is_relative_to(source_root)
                    or path.stat().st_size > MAX_BYTES or digest(path) != entry["sha256"]):
                raise ValueError("research_frozen_input_changed")
        config = {"schema": SCHEMA, "dpi": args.dpi, "pages_per_document": args.pages_per_document,
                  "variants": list(VARIANTS), "recognizers": args.recognizers.split(","), "fingerprint": fingerprint(),
                  "source_root": str(source_root), "limit_documents": args.limit_documents, "excluded_runs": args.exclude_run,
                  "excluded_input_files": excluded_count, "inputs": inputs}
        if frozen and frozen != config:
            raise ValueError("research_cohort_changed_new_output_required")
        write_json(config_path, config)
        start_swap = resources()[1] if sys.platform == "darwin" else 0
        completed = failed = 0
        for source, entry in zip(paths, config["inputs"]):
            if time.monotonic() >= deadline:
                break
            info = subprocess.run(["pdfinfo", str(source)], capture_output=True, timeout=10, check=True).stdout
            match = re.search(rb"(?m)^Pages:\s*(\d+)\s*$", info)
            if not match or not 1 <= int(match[1]) <= 60:
                raise ValueError("research_page_count_bound")
            for page in range(1, min(int(match[1]), args.pages_per_document) + 1):
                output = target / (entry["sha256"] + f"-{page}.json")
                if output.exists():
                    validate_record(json.loads(output.read_text()), entry["sha256"], page, config)
                    continue
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    break
                with tempfile.TemporaryDirectory(dir=root) as temporary:
                    scratch = Path(temporary)
                    environment = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TESSDATA_PREFIX", "SRO_TESSERACT_NATIVE") if k in os.environ}
                    environment.update(LIMIT_ENV, TMPDIR=temporary)
                    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--private-root", str(root),
                               "--input", str(source), "--page", str(page), "--dpi", str(args.dpi),
                               "--worker-output", str(scratch / "page.json"), "--recognizers", args.recognizers]
                    try:
                        run_owned(command, timeout=min(65, remaining), env=environment,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  health=(lambda: admit(start_swap)) if sys.platform == "darwin" else None)
                        result = json.loads((scratch / "page.json").read_text())
                        validate_record(result, entry["sha256"], page, config)
                        write_json(output, result)
                        completed += 1
                    except (RuntimeError, TimeoutError, subprocess.SubprocessError):
                        failed += 1
                        # Failure is retryable in a later bounded invocation; never
                        # replace a successful private page checkpoint.
                        write_json(target / "last-failure.json", {"status": "research_page_failed"})
        records = []
        allowed = {entry["sha256"] for entry in inputs}
        for checkpoint in target.glob("*-*.json"):
            match = re.fullmatch(r"([0-9a-f]{64})-(\d+)", checkpoint.stem)
            if not match:
                continue
            sha, page = match[1], int(match[2])
            if sha not in allowed or not 1 <= page <= args.pages_per_document:
                raise ValueError("research_unplanned_checkpoint")
            records.append(validate_record(json.loads(checkpoint.read_text()), sha, page, config))
        summary = aggregate(records)
        summary.update(completed_this_batch=completed, failed_this_batch=failed,
                       selected_documents=len(paths), excluded_input_files=config["excluded_input_files"],
                       documents_with_page_records=len({r["input_sha256"] for r in records}))
        write_json(target / "metrics.json", summary)
        return summary


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("research_arguments_invalid")


def main(argv=None):
    parser = PrivateParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--output-name", default="raster-weak-v1")
    parser.add_argument("--limit-documents", type=int, default=200)
    parser.add_argument("--pages-per-document", type=int, default=1)
    parser.add_argument("--exclude-run", action="append", default=[],
                        help="Exclude byte-identical inputs from an earlier named run in this private root")
    parser.add_argument("--batch-seconds", type=float, default=240)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--recognizers", choices=("baseline", "baseline,sauvola"), default="baseline",
                        help="Optional paired built-in adaptive-thresholding experiment")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--input", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--page", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    previous = logging.root.manager.disable
    try:
        args = parser.parse_args(argv)
        from scripts.dev.quality.court_dataset import private_root, write_json
        root = private_root(args.private_root)
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            if args.worker:
                output = args.worker_output.resolve()
                source = args.input.resolve(strict=True)
                if (not output.is_relative_to(root) or output == source or source.is_relative_to(ROOT)
                        or not source.is_file() or source.suffix.lower() != ".pdf" or source.stat().st_size > MAX_BYTES
                        or not 1 <= args.page <= 60 or not 72 <= args.dpi <= 300):
                    raise ValueError("research_worker_boundary")
                result = capture_page(source, args.page, args.dpi, VARIANTS, args.recognizers.split(","))
                result["payload_sha256"] = record_digest(result)
                write_json(output, result)
                return 0
            summary = run_batch(args, root)
        print(json.dumps(summary, sort_keys=True))
        return 0
    except Exception:
        print("ocr_research_failed", file=sys.stderr)
        return 1
    finally:
        logging.disable(previous)


if __name__ == "__main__":
    raise SystemExit(main())
