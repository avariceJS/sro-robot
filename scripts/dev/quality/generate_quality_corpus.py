#!/usr/bin/env python3
"""Generate a locally reproducible synthetic court-document quality corpus.

The heldout split uses separate fictional names, cases, dates, and layout rules.
Gold labels are declared here independently from OCR/parser output. Generated
PDFs and manifests belong outside Git; this script never reads office documents.
"""

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


ACTS = (
    ("ОПРЕДЕЛЕНИЕ", "определение"),
    ("РЕШЕНИЕ", "решение"),
    ("ПОСТАНОВЛЕНИЕ", "постановление"),
    ("ЗАЯВЛЕНИЕ", "заявление"),
)
SCENARIOS = ("clear_text", "clear_scan", "mixed_scan", "no_inn", "applicant_only", "multiple_debtors")


def valid_inn(index: int, heldout: bool) -> str:
    prefix = ("781" if heldout else "772") + f"{index:07d}"
    digits = [int(char) for char in prefix]
    first = sum(v * w for v, w in zip(digits, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
    second = sum(v * w for v, w in zip(digits + [first], (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
    return prefix + str(first) + str(second)


def record(split: str, act_index: int, scenario_index: int) -> dict[str, str]:
    heldout = split == "heldout"
    act, doc_type = ACTS[act_index]
    scenario = SCENARIOS[scenario_index]
    names = (
        ("Фёдорова Ирина Николаевна", "Смирнова Наталья Владимировна",
         "Орлова Татьяна Васильевна", "Волкова Марина Алексеевна")
        if heldout else
        ("Иванова Анна Сергеевна", "Петрова Мария Ивановна",
         "Сидорова Ольга Петровна", "Кузнецова Елена Александровна")
    )
    suffix = act_index * 6 + scenario_index + 1
    debtor = names[act_index]
    applicant = f"Заявитель: Учебный{suffix:02d} Пётр Иванович, ИНН {valid_inn(suffix + 200, heldout)}"
    inn = valid_inn(suffix + 100, heldout)
    year = "2098" if heldout else "2099"
    date_text = f"14 октября {year} года" if heldout else f"14 октября {year} года"
    case = f"А98-{740000 + suffix}/{year}" if heldout else f"А99-{730000 + suffix}/{year}"
    return {
        "filename": f"{split}-{act_index + 1}-{scenario}.pdf",
        "split": split,
        "act_heading": act,
        "doc_type": doc_type,
        "scenario": scenario,
        "date": f"14.10.{year}",
        "date_text": date_text,
        "case_number": case,
        "debtor_fio": debtor,
        "inn": inn,
        "applicant_line": applicant,
        "layout": "cover_then_scan" if scenario == "mixed_scan" else "single_scan" if scenario == "clear_scan" else "born_text",
        "expected_disposition": "auto_prepare" if scenario in ("clear_text", "clear_scan", "mixed_scan") and doc_type != "заявление" else "manual_review",
    }


def find_font() -> Path:
    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    )
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit("A local Cyrillic TrueType font is required")


def lines(item: dict[str, str]) -> list[str]:
    head = [item["act_heading"], item["date_text"], f"Дело № {item['case_number']}",
            "Арбитражный суд Учебной области"]
    if item["scenario"] == "applicant_only":
        return head + [item["applicant_line"], "В отношении гражданина вопрос не разрешён"]
    if item["scenario"] == "multiple_debtors":
        return head + ["Должник: Иванова Анна Сергеевна, ИНН 772881234508",
                       "Должник: Петров Пётр Петрович, ИНН 500100732259"]
    if item["scenario"] == "no_inn":
        return head + [item["applicant_line"], f"Должник: {item['debtor_fio']}"]
    return head + [f"Должник: {item['debtor_fio']}, ИНН {item['inn']}"]


def draw_text_page(pdf: canvas.Canvas, content: list[str], font_name: str) -> None:
    pdf.setFont(font_name, 15)
    y = 740
    for line in content:
        pdf.drawString(52, y, line)
        y -= 34


def draw_scan_page(pdf: canvas.Canvas, content: list[str], font_path: Path, scale: float = 1.0) -> None:
    image = Image.new("RGB", (1700, 2200), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path), int(42 * scale))
    y = 170
    for line in content:
        draw.text((120, y), line, fill="black", font=font)
        y += int(95 * scale)
    if scale < 1:
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.BILINEAR)
    pdf.drawImage(ImageReader(image), 0, 0, width=letter[0], height=letter[1], preserveAspectRatio=False)
    image.close()


def make_pdf(path: Path, item: dict[str, str], font_name: str, font_path: Path, split: str) -> None:
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    scenario = item["scenario"]
    if scenario == "clear_scan":
        draw_scan_page(pdf, lines(item), font_path, 0.88 if split == "heldout" else 1.0)
        pdf.showPage()
    elif scenario == "mixed_scan":
        cover = ["Синтетическая титульная страница для локальной проверки OCR."] * 14
        draw_text_page(pdf, cover, font_name)
        pdf.showPage()
        draw_scan_page(pdf, lines(item), font_path, 0.9 if split == "heldout" else 1.0)
        pdf.showPage()
    else:
        draw_text_page(pdf, lines(item), font_name)
        pdf.showPage()
    pdf.save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "heldout"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    font_path = find_font()
    font_hash = hashlib.sha256(font_path.read_bytes()).hexdigest()
    font_name = "QualityCyrillic"
    pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
    entries = []
    for act_index in range(len(ACTS)):
        for scenario_index in range(len(SCENARIOS)):
            item = record(args.split, act_index, scenario_index)
            target = args.output_dir / item["filename"]
            make_pdf(target, item, font_name, font_path, args.split)
            item["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
            item["expected"] = {
                "doc_type": item["doc_type"],
                "doc_date": item["date"],
                "case_number": item["case_number"],
                "fio": item["debtor_fio"] if item["scenario"] not in ("applicant_only", "multiple_debtors") else "",
                "inn": item["inn"] if item["scenario"] in ("clear_text", "clear_scan", "mixed_scan") else "",
                "disposition": item["expected_disposition"],
            }
            entries.append(item)
    manifest = {
        "generator": "scripts/dev/quality/generate_quality_corpus.py",
        "version": 1,
        "split": args.split,
        "python": platform.python_version(),
        "font_sha256": font_hash,
        "documents": entries,
    }
    manifest_path = args.output_dir / f"{args.split}-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "split": args.split,
        "documents": len(entries),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "pdf_sha256_unique": len({item["sha256"] for item in entries}),
        "output_dir": str(args.output_dir),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
