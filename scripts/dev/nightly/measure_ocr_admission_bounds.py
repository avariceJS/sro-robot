#!/usr/bin/env python3
"""Measure one reproducible synthetic four-page 300-DPI OCR batch.

This small host-local probe supplies a sizing point for the default PDF page
bound. It is not office throughput or OCR-quality evidence. Only an aggregate
JSON report is retained; the generated PDF lives in a temporary directory.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
from pathlib import Path
import resource
import sys
import tempfile
import time

from PIL import Image, ImageDraw, ImageFont
from pdf2image import pdfinfo_from_path
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from sro_robot.core import ocr


SYNTHETIC_TEXT = (
    "СИНТЕТИЧЕСКИЙ судебный документ. Дело А99-12345/2099. "
    "Должник ООО Учебный пример. ИНН 9900001234. "
    "Все сведения вымышлены и используются только для локальной проверки."
)


def build_pdf(path: Path, font_path: Path) -> None:
    font = ImageFont.truetype(str(font_path), 28)
    document = canvas.Canvas(str(path), pagesize=A4, invariant=1, pageCompression=1)
    for page_index in range(4):
        image = Image.new("RGB", (1240, 1754), "white")
        draw = ImageDraw.Draw(image)
        draw.text((100, 80), f"СИНТЕТИЧЕСКИЙ АКТ — СТРАНИЦА {page_index + 1}", font=font, fill="black")
        y = 170
        for _ in range(30):
            draw.text((100, y), SYNTHETIC_TEXT, font=font, fill="black")
            y += 48
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=75)
        buffer.seek(0)
        document.drawImage(ImageReader(buffer), 0, 0, width=A4[0], height=A4[1])
        document.showPage()
        image.close()
    document.save()


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if platform.system() == "Darwin" else value * 1024)


def find_font(explicit: Path | None) -> Path:
    candidates = [
        explicit,
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    raise SystemExit("Provide --font with a local Cyrillic TrueType font")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--font", type=Path, help="Local Cyrillic TrueType font")
    parser.add_argument("--raster-batch-pages", type=int, choices=(2, 4), default=2)
    args = parser.parse_args()
    ocr.PDF_RASTER_BATCH_SIZE = args.raster_batch_pages
    font_path = find_font(args.font)
    with tempfile.TemporaryDirectory(prefix="sro-ocr-bound-probe-") as temp:
        pdf = Path(temp) / "synthetic-four-page-scan.pdf"
        build_pdf(pdf, font_path)
        start = time.perf_counter()
        variants = ocr.recognize(pdf)
        elapsed = time.perf_counter() - start
        page_count = int(pdfinfo_from_path(str(pdf), timeout=20)["Pages"])
        result = {
            "scenario": "synthetic_scanned_pdf_300_dpi",
            "synthetic_only": True,
            "external_services_contacted": False,
            "office_accuracy_measured": False,
            "pages": page_count,
            "input_bytes": pdf.stat().st_size,
            "input_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
            "font_sha256": hashlib.sha256(font_path.read_bytes()).hexdigest(),
            "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "ocr_variant_count": len(variants),
            "ocr_wall_seconds": round(elapsed, 3),
            "process_peak_rss_bytes": peak_rss_bytes(),
            "raster_batch_pages": args.raster_batch_pages,
            "python": platform.python_version(),
            "host": platform.platform(),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
