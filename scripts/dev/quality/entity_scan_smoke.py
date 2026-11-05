#!/usr/bin/env python3
"""Run entity/IP parser checks on locally rendered synthetic scanned PDFs.

Requires the optional pinned benchmark dependency set and local Tesseract. PDFs
are generated inside a temporary directory and contain only synthetic values.
The summary reports aggregate outcomes, never extracted text or identifiers.
"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from sro_robot.core.entity_extract import EntityDebtorCard
from sro_robot.core.extract import parse_file


FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
)
CASES = (
    {
        "name": "legal_entity",
        "lines": (
            "ОПРЕДЕЛЕНИЕ", "Дело А99-12345/2099", "14 октября 2099 года",
            "Арбитражный суд Учебной области",
            "Должник: ООО «Синтетическая организация»", "ИНН 7700000016",
            "ОГРН 1027700132195 КПП 770001001",
            "Адрес регистрации: 101000, вымышленный город, дом 1",
        ),
        "expected_kind": "legal_entity", "expected_inn": "7700000016",
        "expected_case": "А99-12345/2099",
        "expected_date": "14.10.2099",
    },
    {
        "name": "sole_proprietor",
        "lines": (
            "ОПРЕДЕЛЕНИЕ", "Дело А99-12346/2099", "14 октября 2099 года",
            "Арбитражный суд Учебной области",
            "Должник: ИП Иванов Иван Иванович", "ИНН 500100001146",
            "ОГРНИП 319500100000001",
            "Фактический адрес: 140000, вымышленный город, дом 2",
        ),
        "expected_kind": "sole_proprietor", "expected_inn": "500100001146",
        "expected_case": "А99-12346/2099",
        "expected_date": "14.10.2099",
    },
)


def font_path() -> Path:
    for candidate in FONT_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise RuntimeError("No local Cyrillic font is available for synthetic scan generation")


def write_scanned_pdf(path: Path, lines: tuple[str, ...], font: Path) -> None:
    # Font selection is also used by the core verifier without PDF-generation extras.
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    image = Image.new("RGB", (1654, 2339), "white")
    draw = ImageDraw.Draw(image)
    face = ImageFont.truetype(str(font), 44)
    y = 180
    for line in lines:
        draw.text((140, y), line, fill="black", font=face)
        y += 105
    pdf = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    pdf.drawImage(ImageReader(image), 0, 0, width=width, height=height)
    pdf.showPage()
    pdf.save()


def main() -> int:
    successes = 0
    with tempfile.TemporaryDirectory(prefix="sro-entity-scan-") as temporary:
        root = Path(temporary)
        face = font_path()
        for case in CASES:
            path = root / f"{case['name']}.pdf"
            write_scanned_pdf(path, case["lines"], face)
            result = parse_file(path)
            if not isinstance(result, EntityDebtorCard):
                raise AssertionError(f"{case['name']}: scan did not route to entity review")
            if result.entity_kind.value != case["expected_kind"]:
                raise AssertionError(f"{case['name']}: synthetic type was not recognized")
            if result.inn.value != case["expected_inn"]:
                raise AssertionError(f"{case['name']}: synthetic INN was not recognized")
            if result.entity_kind.source_variant is None or result.inn.source_start is None:
                raise AssertionError(f"{case['name']}: OCR provenance was not retained")
            successes += 1
    print(f"synthetic_scanned_pdfs={successes}/{len(CASES)}; office_accuracy=not_measured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
