#!/usr/bin/env python3
"""Research-only illumination gate and frozen weak-reference selector replay.

This does not change the product recognizer. Replays reuse observed Tesseract
readings; no new OCR calls, field truth, or real-camera qualification is created.
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

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
ALGORITHM = "Pillow grayscale BOX256; 8x8 tiles; p95 tile brightness; (p90-p10)/255 > 0.20 => Sauvola; else baseline"
THRESHOLD = .20


def quantile(histogram, fraction):
    total, accumulated = sum(histogram), 0
    for value, count in enumerate(histogram):
        accumulated += count
        if accumulated >= fraction * total:
            return value
    return 255


def background_range(image):
    from PIL import Image
    gray = image.convert("L").resize((256, 256), Image.Resampling.BOX)
    values = sorted(quantile(gray.crop((x, y, x + 32, y + 32)).histogram(), .95)
                    for y in range(0, 256, 32) for x in range(0, 256, 32))
    return (values[math.floor(.9 * (len(values) - 1))] - values[math.floor(.1 * (len(values) - 1))]) / 255


def choose_recognizer(image):
    """Decide from raster lighting only, without references or predictions."""
    return "sauvola" if background_range(image) > THRESHOLD else "baseline"


def validate_plan(plan):
    if plan.get("schema") != 1 or plan.get("algorithm") != ALGORITHM or plan.get("threshold") != THRESHOLD:
        raise ValueError("illumination_plan_algorithm")
    controls = plan["control_inputs"]
    cases = {item["case_id"] for item in controls}
    if (not 1 <= len(controls) <= 200 or len({item["sha256"] for item in controls}) != len(controls)
            or any(not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in controls)
            or cases & set(plan["excluded_exposed_case_ids"]) or cases & set(plan["fit_case_ids"])):
        raise ValueError("illumination_case_separation")
    return {item["sha256"] for item in controls}


def validate_cohort(config, producer):
    from scripts.dev.quality.ocr_research import VARIANTS
    if (config.get("schema") != 1 or type(config.get("dpi")) is not int or not 72 <= config["dpi"] <= 300
            or config.get("variants") != list(VARIANTS) or config.get("recognizers") != ["baseline", "sauvola"]
            or config.get("fingerprint") != producer):
        raise ValueError("illumination_producer_or_config_identity")


def replay(root, plan_path):
    from pdf2image import convert_from_path
    from scripts.dev.quality.ocr_research import VARIANTS, MAX_PIXELS, MAX_BYTES, distort, fingerprint, validate_record
    from sro_robot.core import ocr
    from scripts.dev.quality.dataset_capture import digest
    plan = json.loads(plan_path.read_text())
    wanted = validate_plan(plan)
    producer = fingerprint()
    result = dict(experiment="frozen-background-range-gate-control", status="offline-selector-replay",
                  algorithm_sha256=digest(Path(__file__)), plan_sha256=digest(plan_path),
                  candidate_threshold=THRESHOLD, actual_ocr_calls=0, human_verified_pages=0,
                  real_photo_pages=0, population_accuracy_qualified=False, reference_kind="embedded-text-weak",
                  source_guid_controls=len({item["case_id"] for item in plan["control_inputs"]}),
                  transform_identity="current-producer-fingerprint-equals-capture",
                  control_status="case-disjoint frozen-selector weak-label check", strata={})
    completed, rejected, seen_rasters = set(), set(), set()
    for cohort_path in sorted(root.glob("*/cohort.json")):
        config = json.loads(cohort_path.read_text())
        sources = {item["sha256"]: Path(item["path"]) for item in config["inputs"]}
        if not wanted.intersection(sources):
            continue
        validate_cohort(config, producer)
        for checkpoint in sorted(cohort_path.parent.glob("*.json")):
            match = re.fullmatch(r"([0-9a-f]{64})-(\d+)", checkpoint.stem)
            if not match or match[1] not in wanted:
                continue
            record = validate_record(json.loads(checkpoint.read_text()), match[1], int(match[2]), config)
            if not record["qualification"]["eligible"]:
                rejected.add(match[1])
                continue
            # Keep one physical page per requested act in this selector check.
            if match[1] in completed:
                continue
            source = sources[match[1]]
            if (source.is_symlink() or not source.resolve().is_relative_to(Path(config["source_root"]))
                    or source.resolve().is_relative_to(ROOT) or source.stat().st_size > MAX_BYTES
                    or source.suffix.lower() != ".pdf" or digest(source) != match[1]):
                raise ValueError("illumination_original_identity")
            budget = ocr.OcrBudget(deadline_seconds=30, pdfinfo_timeout_seconds=10)
            count, page_sizes = ocr._pdf_page_metadata(source, budget, 60)
            if not 1 <= record["page"] <= count <= 60:
                raise ValueError("illumination_page_bound")
            ocr._check_pdf_page_pixel_limits(page_sizes, [record["page"] - 1], config["dpi"], MAX_PIXELS)
            images = convert_from_path(source, dpi=config["dpi"], first_page=record["page"], last_page=record["page"],
                                       thread_count=1, timeout=15)
            if len(images) != 1 or images[0].width * images[0].height > MAX_PIXELS:
                raise ValueError("illumination_raster_bound")
            image = images[0]
            raster_id = hashlib.sha256(str(image.size).encode() + image.convert("RGB").tobytes()).hexdigest()
            if raster_id != record["raster_sha256"] or raster_id in seen_rasters:
                raise ValueError("illumination_raster_identity_or_duplicate")
            seen_rasters.add(raster_id)
            readings = {(item["variant"], item["recognizer"]): item for item in record["readings"]}
            for variant in VARIANTS:
                selected = choose_recognizer(distort(image, variant))
                before = readings[variant, "baseline"]["metrics"]["characters"]
                after = readings[variant, selected]["metrics"]["characters"]
                group = result["strata"].setdefault(variant, Counter())
                group.update(pages=1, candidate_selected=int(selected == "sauvola"), baseline_edits=before["edits"],
                             gated_edits=after["edits"], reference_characters=before["reference_units"],
                             improved_pages=int(after["edits"] < before["edits"]),
                             regressed_pages=int(after["edits"] > before["edits"]),
                             equal_pages=int(after["edits"] == before["edits"]))
            completed.add(match[1])
    result.update(completed_inputs=len(completed), rejected_weak_reference_inputs=len(rejected - completed),
                  pending_inputs=len(wanted - completed - rejected))
    return result


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("illumination_arguments_invalid")


def main(argv=None):
    parser = PrivateParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--plan-name", default="background-gate-control-plan.json")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    previous = logging.root.manager.disable
    try:
        args = parser.parse_args(argv)
        from scripts.dev.quality.court_dataset import dataset_lock, private_root, write_json
        from scripts.dev.tesseract_native import LIMIT_ENV, admit, resources, run_owned
        root = private_root(args.private_root)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}\.json", args.plan_name):
            raise ValueError("illumination_plan_name")
        plan_path = root / args.plan_name
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            if args.worker:
                output = args.worker_output.resolve()
                if not output.is_relative_to(root) or output.exists():
                    raise ValueError("illumination_output_boundary")
                write_json(output, replay(root, plan_path))
                return 0
            with dataset_lock(root), tempfile.TemporaryDirectory(dir=root) as temporary:
                output = Path(temporary) / "result.json"
                environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TESSDATA_PREFIX", "SRO_TESSERACT_NATIVE") if key in os.environ}
                environment.update(LIMIT_ENV, TMPDIR=temporary)
                start_swap = resources()[1] if sys.platform == "darwin" else 0
                run_owned([sys.executable, str(Path(__file__).resolve()), "--private-root", str(root),
                           "--plan-name", args.plan_name, "--worker", "--worker-output", str(output)],
                          timeout=240, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          health=(lambda: admit(start_swap)) if sys.platform == "darwin" else None)
                summary = json.loads(output.read_text())
                write_json(root / (Path(args.plan_name).stem + "-replay.json"), summary)
        print(json.dumps(summary, sort_keys=True))
        return 0
    except Exception:
        print("ocr_illumination_replay_failed", file=sys.stderr)
        return 1
    finally:
        logging.disable(previous)


if __name__ == "__main__":
    raise SystemExit(main())
