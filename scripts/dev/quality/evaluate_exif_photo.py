#!/usr/bin/env python3
"""Compare local OCR on one deterministic synthetic EXIF-oriented phone photo.

The report contains aggregate matches only. The image is generated in a
temporary directory; no source document or OCR text is saved or printed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont
from unittest.mock import patch

from scripts.dev.quality.generate_quality_corpus import find_font, valid_inn
from sro_robot.core.extract import parse_file
from sro_robot.core import ocr


EXPECTED = {
    "doc_type": "определение",
    "doc_date": "14.10.2098",
    "case_number": "А98-745321/2098",
    "fio": "Фёдорова Ирина Николаевна",
}
FIELDS = (*EXPECTED, "inn")
EXPECTED_INN = valid_inn(777, heldout=True)
EXPECTED_IMAGE_SHA256 = "0410269e581b2a6928261471c54e13c8e1394699bbd9164e3aaba717bd42cf76"
EXPECTED_FONT_SHA256 = "525979822591a3447cfc49d943d6f7683508e25543407871c0ed8fed05fd2bd9"


def _measure(path: Path, *, apply_orientation: bool) -> dict[str, object]:
    started = perf_counter()
    if apply_orientation:
        card = parse_file(path)
    else:
        # Reproduce the pre-fix load_pages behavior without maintaining an old
        # product copy: the previous loader returned the raw pixel orientation.
        with patch.object(ocr.ImageOps, "exif_transpose", side_effect=lambda image: image):
            card = parse_file(path)
    elapsed = perf_counter() - started
    observed = {
        "doc_type": card.doc_type.value or "",
        "doc_date": card.doc_date.value or "",
        "case_number": card.case_number.value or "",
        "fio": card.fio_raw.value or "",
        "inn": card.inn.value or "",
    }
    expected = {**EXPECTED, "inn": EXPECTED_INN}
    counts = {"exact": 0, "wrong_nonempty": 0, "missing": 0}
    for field_name, expected_value in expected.items():
        actual = observed[field_name]
        if actual == expected_value:
            counts["exact"] += 1
        elif actual:
            counts["wrong_nonempty"] += 1
        else:
            counts["missing"] += 1
    return {**counts, "fields": len(expected), "elapsed_seconds": round(elapsed, 3)}


def main() -> int:
    font_path = find_font()
    font_sha256 = hashlib.sha256(font_path.read_bytes()).hexdigest()
    if font_sha256 != EXPECTED_FONT_SHA256:
        raise SystemExit("Local Cyrillic font differs from the pinned synthetic input")

    with TemporaryDirectory(prefix="sro-exif-photo-") as temporary:
        path = Path(temporary) / "synthetic-exif-orientation-6.jpg"
        image = Image.new("RGB", (1700, 2200), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(str(font_path), 48)
        lines = (
            "ОПРЕДЕЛЕНИЕ",
            "14 октября 2098 года",
            "Дело № А98-745321/2098",
            "Арбитражный суд Учебной области",
            f"Должник: {EXPECTED['fio']}, ИНН {EXPECTED_INN}",
        )
        for index, line in enumerate(lines):
            draw.text((110, 180 + index * 150), line, fill="black", font=font)
        rotated = image.rotate(90, expand=True)
        exif = Image.Exif()
        exif[274] = 6
        rotated.save(path, "JPEG", quality=93, exif=exif)
        rotated.close()
        image.close()

        image_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if image_sha256 != EXPECTED_IMAGE_SHA256:
            raise SystemExit(
                "Generated synthetic photo differs from the pinned input: "
                f"{image_sha256}"
            )
        results = {
            "raw_orientation_baseline": _measure(path, apply_orientation=False),
            "exif_transposed": _measure(path, apply_orientation=True),
        }

    report = {
        "synthetic_only": True,
        "input_sha256": EXPECTED_IMAGE_SHA256,
        "font_sha256": font_sha256,
        "documents": 1,
        "fields_per_document": len(FIELDS),
        "results": results,
        "claim_limit": "One generated page; not representative photo accuracy",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
