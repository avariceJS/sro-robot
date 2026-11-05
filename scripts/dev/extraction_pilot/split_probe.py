"""Matched Tesseract PSM 3 split-only probe for the 3588 two-page spread.

This diagnostic never contacts or starts a model runtime, DB, or SRO service.
It writes only privacy-safe counters/booleans; OCR text stays in process memory.
Run: .venv/bin/python scripts/dev/extraction_pilot/split_probe.py
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

from PIL import Image
from pillow_heif import register_heif_opener

ROOT = Path(__file__).resolve().parents[3]
PILOT = ROOT / ".local-run/extraction-pilot-20261105"
MANIFEST = PILOT / "corpus/manifest-private.json"
MARKERS = PILOT / "split-probe/markers-private.json"
OUT = PILOT / "split-probe/privacy-safe-summary.json"
SOURCE_ID = "IMG_3588"
PSM = 3


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def page_markers(text: str, markers: dict) -> dict:
    """Return only privacy-safe marker booleans, never matched text."""
    folded = " ".join(text.casefold().replace("ё", "е").split())
    digits = re.sub(r"\D", "", text)
    name_markers = markers["name_stem_markers"]
    address_terms = markers["address_terms_required"]
    date_terms = markers["date_terms"]
    case_terms = markers["case_digits_terms"]
    snils_digits = markers["snils_digits"]
    inn_digits = markers["inn_digits_disputed"]
    role_name_stem = markers["role_name_stem"]
    return {
        "primary_name_stem_markers_present": bool(name_markers) and all(x in folded for x in name_markers),
        "document_date_marker_present": all(x in folded for x in date_terms),
        "case_number_marker_present_disputed": all(x in digits for x in case_terms),
        "inn_digits_present_disputed": bool(inn_digits) and inn_digits in digits,
        "snils_digits_present": bool(snils_digits) and snils_digits in digits,
        "address_distinctive_markers_present": bool(address_terms) and all(x in folded for x in address_terms),
        "address_markers": {
            "street_name_present": markers["address_street_marker"] in folded,
            "building_unit_label_present": markers["address_unit_marker"] in folded,
            "building_number_marker_present": bool(re.search(markers["building_number_regex"], folded)),
        },
        "address_kind_marker_present": markers["generic_address_kind_stem"] in folded,
        "primary_role_marker_present": role_name_stem in folded and markers["generic_debtor_word"] in folded,
        "score_eligible": {
            "case_number_marker_present_disputed": False,
            "inn_digits_present_disputed": False,
        },
    }


def line_metrics(tsv: str, width: int, height: int, *, full_spread: bool = True) -> dict:
    rows = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    lines: dict[tuple[int, int, int, int], list[dict]] = defaultdict(list)
    for row in rows:
        if int(row.get("level") or 0) != 5 or not (row.get("text") or "").strip():
            continue
        try:
            key = tuple(int(row[k]) for k in ("page_num", "block_num", "par_num", "line_num"))
            left, top = int(row["left"]), int(row["top"])
            w, h = int(row["width"]), int(row["height"])
        except (KeyError, ValueError):
            continue
        lines[key].append({"cx": left + w / 2, "cy": top + h / 2, "top": top, "height": h})
    ordered = []
    for words in lines.values():
        ordered.append({
            "cx": sum(x["cx"] for x in words) / len(words),
            "cy": sum(x["cy"] for x in words) / len(words),
            "top": min(x["top"] for x in words),
            "height": max(x["height"] for x in words),
        })
    if not full_spread:
        vertical_backsteps = sum(
            ordered[i + 1]["top"] < ordered[i]["top"] - max(16, ordered[i]["height"])
            for i in range(len(ordered) - 1)
        )
        return {
            "recognized_line_count": len(ordered),
            "within_page_vertical_backsteps": vertical_backsteps,
            "page_dimensions": [width, height],
        }
    # For the full spread only, classify lines around the fixed photographed
    # seam. The 160-pixel overlap band is deliberately ambiguous.
    seam_mid = width / 2
    seam_half = 80
    sides = []
    for item in ordered:
        if item["cx"] < seam_mid - seam_half:
            sides.append("left")
        elif item["cx"] > seam_mid + seam_half:
            sides.append("right")
        else:
            sides.append("seam")
    compact = []
    for side in sides:
        if not compact or compact[-1] != side:
            compact.append(side)
    transitions = sum(a != b for a, b in zip(compact, compact[1:]))
    nonseam = [x for x in sides if x != "seam"]
    nonseam_compact = []
    for side in nonseam:
        if not nonseam_compact or nonseam_compact[-1] != side:
            nonseam_compact.append(side)
    direction_breaks = sum(a == "right" and b == "left" for a, b in zip(nonseam, nonseam[1:]))
    by_side = defaultdict(list)
    for side, item in zip(sides, ordered):
        if side != "seam":
            by_side[side].append(item)
    vertical_backsteps = {}
    for side, values in by_side.items():
        vertical_backsteps[side] = sum(
            values[i + 1]["top"] < values[i]["top"] - max(16, values[i]["height"])
            for i in range(len(values) - 1)
        )
    return {
        "recognized_line_count": len(ordered),
        "side_sequence_collapsed": compact,
        "side_transition_count": transitions,
        "nonseam_side_sequence_collapsed": nonseam_compact,
        "nonseam_side_transition_count": sum(a != b for a, b in zip(nonseam_compact, nonseam_compact[1:])),
        "right_to_left_backstep_count": direction_breaks,
        "within_side_vertical_backsteps": vertical_backsteps,
        "seam_ambiguous_line_count": sides.count("seam"),
        "page_dimensions": [width, height],
    }


def run_psm3(image: Image.Image) -> tuple[str, str, float]:
    from sro_robot.core.ocr import OcrBudget, _tesseract_with_source
    started = time.perf_counter()
    text, tsv = _tesseract_with_source(
        image, f"--psm {PSM}", OcrBudget(tesseract_timeout_seconds=90)
    )
    return text.strip(), tsv, time.perf_counter() - started


def map_point(matrix: list[list[int]], point: tuple[float, float]) -> tuple[float, float]:
    x, y = point
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2],
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2],
    )


def validate_geometry(source: dict) -> dict:
    width, height = source["stored_dimensions"]
    upright_width, upright_height = source["upright_dimensions"]
    pages = source["pages"]
    mapped_bounds = []
    for page in pages:
        left, top, right, bottom = page["crop"]
        page_w, page_h = right - left, bottom - top
        matrix = page["transform_to_original"]
        corners = [map_point(matrix, p) for p in ((0, 0), (page_w, 0), (0, page_h), (page_w, page_h))]
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        bounds = [min(xs), min(ys), max(xs), max(ys)]
        mapped_bounds.append(bounds)
    a, b = pages[0]["crop"], pages[1]["crop"]
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    upright_union = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
    return {
        "source_dimensions": [width, height],
        "upright_dimensions": [upright_width, upright_height],
        "crop_bounds_upright_pixels": [p["crop"] for p in pages],
        "crop_to_original_bounds": mapped_bounds,
        "upright_seam_overlap_pixels": overlap,
        "upright_union_bounds": upright_union,
        "outer_margins_preserved": upright_union == [0, 0, upright_width, upright_height],
        "mapped_bounds_inside_original": all(
            0 <= x0 <= x1 <= width and 0 <= y0 <= y1 <= height
            for x0, y0, x1, y1 in mapped_bounds
        ),
        "source_sha256": source["sha256"],
        "crop_image_sha256": [p["image_sha256"] for p in pages],
    }


def main() -> None:
    register_heif_opener()
    sys.path.insert(0, str(ROOT))
    manifest = json.loads(MANIFEST.read_text())
    markers = json.loads(MARKERS.read_text())
    source = next(x for x in manifest["sources"] if x["source_id"] == SOURCE_ID)
    if markers.get("source_id") != SOURCE_ID:
        raise ValueError("Marker file source_id does not match the bounded diagnostic")
    markers_sha256 = sha256(MARKERS)
    input_path = ROOT / source["path"]

    decode_started = time.perf_counter()
    original = Image.open(input_path).convert("RGB")
    decode_seconds = time.perf_counter() - decode_started
    rotation_started = time.perf_counter()
    upright = original.rotate(source["rotation_ccw"], expand=True)
    # Match the frozen crop JPEG settings before preprocessing the whole frame.
    full_buffer = io.BytesIO()
    upright.save(full_buffer, format="JPEG", quality=94, optimize=True)
    full_jpeg = full_buffer.getvalue()
    full_hash = hashlib.sha256(full_jpeg).hexdigest()
    rotation_encode_seconds = time.perf_counter() - rotation_started
    prep_started = time.perf_counter()
    from sro_robot.core.ocr import preprocess
    full_ready = preprocess(Image.open(io.BytesIO(full_jpeg)).convert("RGB"), scale=1)
    full_preprocess_seconds = time.perf_counter() - prep_started

    full_text, full_tsv, full_ocr_seconds = run_psm3(full_ready)
    full_stats = line_metrics(full_tsv, *upright.size)

    crop_runs = []
    split_texts = []
    crop_ocr_seconds = 0.0
    crop_preprocess_seconds = 0.0
    for page in source["pages"]:
        crop_path = PILOT / page["image_path"]
        t0 = time.perf_counter()
        crop_ready = preprocess(Image.open(crop_path).convert("RGB"), scale=1)
        prep_s = time.perf_counter() - t0
        text, tsv, ocr_s = run_psm3(crop_ready)
        crop_preprocess_seconds += prep_s
        crop_ocr_seconds += ocr_s
        split_texts.append(text)
        metrics = line_metrics(tsv, *crop_ready.size, full_spread=False)
        crop_runs.append({
            "page_id": page["page_id"],
            "preprocess_seconds": round(prep_s, 4),
            "ocr_seconds": round(ocr_s, 4),
            "recognized_line_count": metrics["recognized_line_count"],
            "within_page_vertical_backsteps": metrics["within_page_vertical_backsteps"],
        })
    combined_split_text = "\n\n".join(split_texts)
    full_markers = page_markers(full_text, markers)
    split_markers = page_markers(combined_split_text, markers)
    geometry = validate_geometry(source)

    summary = {
        "diagnostic": "matched PSM3 unsplit-full-frame vs two frozen spread crops",
        "source_id": SOURCE_ID,
        "source_sha256": source["sha256"],
        "marker_config_sha256": markers_sha256,
        "engine": "Tesseract via sro_robot.core.ocr._tesseract_with_source; lang=rus; psm=3; scale=1 preprocess",
        "tesseract_version": "5.5.3",
        "preprocessing": {
            "decode_seconds": round(decode_seconds, 4),
            "rotate_and_full_jpeg_encode_seconds": round(rotation_encode_seconds, 4),
            "full_frame_preprocess_seconds": round(full_preprocess_seconds, 4),
            "frozen_crops_generation_seconds_from_manifest": source["preparation_seconds"],
            "frozen_crops_generation_scope": source["preparation_scope"],
            "experimental_crop_read_preprocess_seconds_sum": round(crop_preprocess_seconds, 4),
        },
        "ocr_seconds": {
            "unsplit_full_frame": round(full_ocr_seconds, 4),
            "two_crop_sum": round(crop_ocr_seconds, 4),
            "unsplit_total_seconds": round(decode_seconds + rotation_encode_seconds + full_preprocess_seconds + full_ocr_seconds, 4),
            "split_comparison_total_seconds_excluding_original_corpus_generation": round(crop_preprocess_seconds + crop_ocr_seconds, 4),
            "split_total_seconds_including_frozen_corpus_generation": round(source["preparation_seconds"] + crop_preprocess_seconds + crop_ocr_seconds, 4),
        },
        "text_quality_markers": {
            "unsplit_full_frame": full_markers,
            "two_crops_concatenated_left_then_right": split_markers,
            "fields_note": "All booleans only. Exact case number and INN are visually disputed in the frozen pre-Qwen reference and are marked score-ineligible.",
        },
        "read_order": {
            "unsplit_full_frame": full_stats,
            "split_page_order": ["left_page_p1", "right_page_p2"],
            "split_pages": crop_runs,
            "interpretation": "side transitions and vertical backsteps are geometric OCR-order indicators; no OCR text is retained in this summary.",
        },
        "original_margin_and_transform_check": geometry,
        "privacy": "No OCR text, names, addresses, or identifier values are written to this report.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    OUT.chmod(0o600)
    print(json.dumps({
        "source_id": SOURCE_ID,
        "summary_path": str(OUT.relative_to(ROOT)),
        "unsplit_ocr_s": summary["ocr_seconds"]["unsplit_full_frame"],
        "split_ocr_s": summary["ocr_seconds"]["two_crop_sum"],
        "unsplit_side_sequence": full_stats["side_sequence_collapsed"],
        "outer_margins_preserved": geometry["outer_margins_preserved"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
