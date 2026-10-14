#!/usr/bin/env python3
"""Generate a deterministic, synthetic 100-PDF intake workload."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


COUNT = 100
PREFIXES = ("Ладо", "Север", "Вектор", "Контур", "Липа", "Сосна", "Маяк", "Роща", "Полюс", "Гранит")
SUFFIXES = ("берег", "ветер", "свет", "ручей", "туман", "парус", "камень", "предел", "образ", "мир")
GIVEN_NAMES = ("Анна", "Мария", "Елена", "Ирина", "Ольга", "Нина", "Татьяна", "Вера", "Лидия", "Зоя")
PATRONYMICS = ("Петровна", "Ивановна", "Сергеевна", "Андреевна", "Михайловна", "Алексеевна", "Викторовна", "Павловна", "Фёдоровна", "Ильинична")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
BOILERPLATE = (
    "Документ сформирован исключительно для локального синтетического теста. "
    "Все реквизиты, имена и адреса вымышлены. Этот текст не относится к реальному делу. "
    "Значения предназначены для проверки связи между файлом, источником, задачей и полями. "
) * 3


def find_font(explicit: str | None) -> Path:
    candidates = [
        Path(explicit) if explicit else None,
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise SystemExit("Provide --font with a local Cyrillic TrueType font")


def records() -> list[dict[str, str | int]]:
    output = []
    for index in range(COUNT):
        surname = PREFIXES[index // 10] + SUFFIXES[index % 10] + "ова"
        first = GIVEN_NAMES[(index * 3) % len(GIVEN_NAMES)]
        middle = PATRONYMICS[(index * 7) % len(PATRONYMICS)]
        day = index % 28 + 1
        month = MONTHS[index % 12]
        case = f"А99-{900001 + index}/2099"
        digits = [int(char) for char in f"990000{index:04d}"]
        checksum_11 = sum(value * weight for value, weight in zip(digits, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
        checksum_12 = sum(value * weight for value, weight in zip(digits + [checksum_11], (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
        inn = "".join(map(str, digits)) + str(checksum_11) + str(checksum_12)
        output.append(
            {
                "index": index,
                "filename": f"synthetic-{index + 1:03d}.pdf",
                "case_number": case,
                "doc_date": f"{day:02d}.{index % 12 + 1:02d}.2099",
                "doc_date_text": f"{day} {month} 2099 года",
                "fio": f"{surname} {first} {middle}",
                "inn": inn,
                "kind": ("text" if index < 60 else "multipage" if index < 70 else "scan_high" if index < 80 else "scan_medium" if index < 90 else "scan_low" if index < 95 else "mixed"),
                "pages": 3 if 60 <= index < 70 else 2 if index >= 95 else 1,
            }
        )
    return output


def document_lines(record: dict[str, str | int]) -> list[str]:
    return [
        "ОПРЕДЕЛЕНИЕ",
        str(record["doc_date_text"]),
        f"Дело № {record['case_number']}",
        f"должник: {record['fio']}",
        f"ИНН: {record['inn']}",
        "Адрес регистрации: 101000, вымышленный город Макетный, улица Учебная, дом 1",
        BOILERPLATE,
    ]


def draw_text_pdf(path: Path, record: dict[str, str | int], font_name: str) -> None:
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1, pageCompression=1)
    pdf.setTitle(f"Synthetic local test {record['index']}")
    pages = int(record["pages"])
    for page_index in range(pages):
        pdf.setFont(font_name, 13)
        lines = document_lines(record) if page_index == 0 else ["Продолжение синтетического документа", BOILERPLATE]
        y = 740
        for line in lines:
            for chunk_start in range(0, len(line), 78):
                pdf.drawString(52, y, line[chunk_start : chunk_start + 78])
                y -= 24
        pdf.showPage()
    pdf.save()


def scan_image(record: dict[str, str | int], font_path: Path, quality: str) -> Image.Image:
    scale = {"high": 1.0, "medium": 0.9, "low": 0.68}[quality]
    width, height = int(1700 * scale), int(2200 * scale)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path), max(24, int(42 * scale)))
    y = int(150 * scale)
    for line in document_lines(record):
        for start in range(0, len(line), 52):
            draw.text((int(120 * scale), y), line[start : start + 52], fill="black", font=font)
            y += int(64 * scale)
        y += int(12 * scale)
    if quality == "low":
        image = image.convert("L").resize((1156, 1496), Image.Resampling.BILINEAR)
    elif quality == "medium":
        image = image.convert("L")
    return image


def draw_scan_pdf(path: Path, record: dict[str, str | int], font_path: Path, quality: str) -> None:
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1, pageCompression=1)
    pdf.setTitle(f"Synthetic scanned test {record['index']}")
    image = scan_image(record, font_path, quality)
    pdf.drawImage(ImageReader(image), 0, 0, width=letter[0], height=letter[1], preserveAspectRatio=False)
    pdf.showPage()
    pdf.save()


def draw_mixed_pdf(path: Path, record: dict[str, str | int], font_name: str, font_path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1, pageCompression=1)
    pdf.setTitle(f"Synthetic mixed test {record['index']}")
    pdf.setFont(font_name, 13)
    y = 740
    for line in document_lines(record):
        for start in range(0, len(line), 78):
            pdf.drawString(52, y, line[start : start + 78])
            y -= 24
    pdf.showPage()
    second = Image.new("RGB", (1700, 2200), "white")
    draw = ImageDraw.Draw(second)
    draw.text((120, 150), "Продолжение сканированной страницы", fill="black", font=ImageFont.truetype(str(font_path), 42))
    pdf.drawImage(ImageReader(second), 0, 0, width=letter[0], height=letter[1])
    pdf.showPage()
    pdf.save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--font", help="Local Cyrillic TrueType font file")
    args = parser.parse_args()
    font_path = find_font(args.font)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    font_hash = hashlib.sha256(font_path.read_bytes()).hexdigest()
    pdfmetrics.registerFont(TTFont("NightlyCyrillic", str(font_path)))

    manifest = {
        "generator": "scripts/dev/nightly/generate_corpus.py",
        "generator_version": 1,
        "python": platform.python_version(),
        "reportlab": "4.4.4",
        "font_sha256": font_hash,
        "documents": [],
    }
    for record in records():
        path = args.output_dir / str(record["filename"])
        kind = str(record["kind"])
        if kind == "text" or kind == "multipage":
            draw_text_pdf(path, record, "NightlyCyrillic")
        elif kind.startswith("scan_"):
            draw_scan_pdf(path, record, font_path, kind.removeprefix("scan_"))
        else:
            draw_mixed_pdf(path, record, "NightlyCyrillic", font_path)
        entry = dict(record)
        entry["bytes"] = path.stat().st_size
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["documents"].append(entry)

    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"generated={len(manifest['documents'])} distinct_pdf_sha256={len({d['sha256'] for d in manifest['documents']})}")
    print(f"manifest_sha256={hashlib.sha256(manifest_path.read_bytes()).hexdigest()}")
    print(f"font_sha256={font_hash}")
    print(f"total_bytes={sum(int(d['bytes']) for d in manifest['documents'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
