"""Превращение сканов, фото и PDF в текст.

Фото с телефона Tesseract читает заметно хуже скана, поэтому каждый документ
прогоняется несколькими режимами сегментации, а выбор между результатами
делает парсер голосованием.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytesseract
from PIL import Image, ImageFilter, ImageOps
from pdf2image import convert_from_path

# psm 3 и 4 обычно лучшие на плотном тексте, 11 выручает на перекошенных фото.
PSM_MODES = (3, 4, 6, 11)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


def preprocess(image: Image.Image, scale: int = 2) -> Image.Image:
    """Серый, увеличенный и подрезанный по контрасту кадр читается точнее."""
    g = ImageOps.grayscale(image)
    if scale != 1:
        g = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
    g = ImageOps.autocontrast(g)
    return g.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))


def pdf_text_layer(path: Path) -> str:
    """У PDF из «Мой арбитр» есть текстовый слой — OCR тогда не нужен."""
    try:
        res = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout if res.returncode == 0 else ""


def load_pages(path: Path, dpi: int = 300) -> list[Image.Image]:
    if path.suffix.lower() == ".pdf":
        return convert_from_path(str(path), dpi=dpi)
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return [Image.open(path)]
    raise ValueError(f"Неподдерживаемый формат файла: {path.suffix}")


def _recognize_legacy_pages(path: Path, pages: list[Image.Image]) -> list[str]:
    """Existing upscale/sharpen plus original-image OCR used for photographs."""
    prepared = [preprocess(p) for p in pages]

    variants: list[str] = []
    for psm in PSM_MODES:
        config = f"--psm {psm}"
        chunks = []
        for page in prepared:
            try:
                chunks.append(pytesseract.image_to_string(page, lang="rus", config=config))
            except pytesseract.TesseractError:
                chunks.append("")
        text = "\n".join(chunks).strip()
        if text:
            variants.append(text)

    # Оригинал без обработки иногда точнее на чистых сканах.
    for page in pages:
        try:
            text = pytesseract.image_to_string(page, lang="rus", config="--psm 3").strip()
        except pytesseract.TesseractError:
            text = ""
        if text:
            variants.append(text)

    if not variants:
        raise RuntimeError(f"Не удалось распознать текст в {path}")
    return variants


def _recognize_scanned_pdf(path: Path, pages: list[Image.Image], cutoff: int = 215) -> list[str]:
    """Use a bounded binary pass for PDF scans; fall back to the legacy pass."""
    variants: list[str] = []
    for page in pages:
        gray = ImageOps.autocontrast(ImageOps.grayscale(page))
        binary = gray.point(lambda pixel: 255 if pixel >= cutoff else 0)
        for psm in PSM_MODES:
            try:
                text = pytesseract.image_to_string(binary, lang="rus", config=f"--psm {psm}").strip()
            except pytesseract.TesseractError:
                text = ""
            if text:
                variants.append(text)
    if variants:
        return variants
    return _recognize_legacy_pages(path, pages)


def recognize(path: Path, dpi: int = 300) -> list[str]:
    """Return OCR variants, keeping text-layer PDFs and photo OCR local."""
    path = Path(path)

    if path.suffix.lower() == ".pdf":
        embedded = pdf_text_layer(path)
        # Короткий текстовый слой означает скан внутри PDF, а не настоящий текст.
        if len(embedded.strip()) > 400:
            return [embedded]
        pages = load_pages(path, dpi=dpi)
        return _recognize_scanned_pdf(path, pages)

    pages = load_pages(path, dpi=dpi)
    return _recognize_legacy_pages(path, pages)
