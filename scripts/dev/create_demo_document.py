#!/usr/bin/env python3
"""Create a scanned, fully synthetic Russian court-ruling PDF for local demos."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font_path(*candidates: str) -> Path | None:
    return next((Path(path) for path in candidates if Path(path).is_file()), None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("out/DEMO_ONLY_synthetic_ruling.pdf"),
        help="output PDF path (default: out/DEMO_ONLY_synthetic_ruling.pdf)",
    )
    args = parser.parse_args()

    font_path = _font_path(
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    bold_path = _font_path(
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    )
    if font_path is None or bold_path is None:
        parser.error("A system font with Cyrillic support is required to build the demo PDF")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    regular = ImageFont.truetype(str(font_path), 60)
    small = ImageFont.truetype(str(font_path), 45)
    bold = ImageFont.truetype(str(bold_path), 68)
    image = Image.new("RGB", (2480, 3508), "white")
    draw = ImageDraw.Draw(image)
    lines = [
        ("Арбитражный суд Демонстрационной области", bold, 700),
        ("ОПРЕДЕЛЕНИЕ", bold, 700),
        ("12 марта 2026 г.", regular, 700),
        ("Дело № А99-12345/2099", bold, 700),
        ("Рассмотрев заявление Иванов Иван Иванович", regular, 300),
        ("о признании гражданина несостоятельным (банкротом),", regular, 300),
        ("суд установил:", regular, 300),
        ("Иванов Иван Иванович обратился в суд с заявлением.", regular, 300),
        (
            "Адрес регистрации: 000000, Демонстрационный край, г. Примерск, ул. Учебная, д. 1.",
            small,
            300,
        ),
        ("Руководствуясь законом, суд определил:", regular, 300),
        ("Принять заявление к производству.", regular, 300),
        ("Документ и сведения в нём полностью синтетические.", regular, 300),
        ("Создан только для локальной демонстрации SRO Robot.", regular, 300),
    ]
    y = 250
    for index, (text, font, x) in enumerate(lines):
        if index == 4:
            y += 110
        draw.text((x, y), text, font=font, fill="black")
        y += 120

    image.save(args.output, "PDF", resolution=300)
    args.output.chmod(0o600)
    print(f"Created synthetic demo PDF: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
