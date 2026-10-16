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
from pdf2image import convert_from_path, pdfinfo_from_path

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


def _load_pdf_pages(path: Path, first_page: int, last_page: int, dpi: int) -> list[Image.Image]:
    """Rasterize a bounded 1-based page range to cap per-worker image memory."""
    return convert_from_path(
        str(path), dpi=dpi, first_page=first_page, last_page=last_page
    )


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


def _pdf_text_pages(text: str) -> list[str]:
    pages = text.split("\f")
    # pdftotext normally emits one form feed per page, including a trailing
    # separator. Drop only that sentinel: an empty preceding page is an image
    # page and must remain available for OCR.
    if text.endswith("\f") and len(pages) > 1:
        pages.pop()
    return pages or [text]


def _recognize_pdf_pages(path: Path, text_pages: list[str], dpi: int) -> list[str]:
    """Keep usable PDF text and OCR other pages in bounded raster batches."""
    # A short hidden layer often contains only stray glyphs from a scanned page.
    # Prefer a complete OCR rendering for that page, falling back to the text
    # layer only when a Tesseract mode returns no content.
    page_text = [page.strip() for page in text_pages]
    scan_pages = [index for index, page in enumerate(page_text) if len(page) < 120]
    if not scan_pages:
        return ["\n\f\n".join(page_text)]

    by_mode: dict[int, list[str]] = {psm: list(page_text) for psm in PSM_MODES}
    # At most four letter pages (about 100 MiB RGB at 300 DPI) are held per
    # worker. This bounds long scans without paying one Poppler startup per page.
    batch_size = 4
    batch_start = 0
    while batch_start < len(scan_pages):
        batch = [scan_pages[batch_start]]
        while (
            batch_start + len(batch) < len(scan_pages)
            and len(batch) < batch_size
            and scan_pages[batch_start + len(batch)] == batch[-1] + 1
        ):
            batch.append(scan_pages[batch_start + len(batch)])
        images = _load_pdf_pages(path, batch[0] + 1, batch[-1] + 1, dpi)
        if len(images) != len(batch):
            raise RuntimeError(f"Не удалось прочитать страницы {batch[0] + 1}–{batch[-1] + 1} в {path}")
        for index, image in zip(batch, images, strict=True):
            gray = ImageOps.autocontrast(ImageOps.grayscale(image))
            histogram = gray.histogram()
            # A genuinely blank separator page does not need four Tesseract
            # calls. The threshold is tiny so faint text still reaches OCR.
            if sum(histogram[:245]) > 25:
                binary = gray.point(lambda pixel: 255 if pixel >= 215 else 0)
                page_recognized = False
                for psm in PSM_MODES:
                    try:
                        recognized = pytesseract.image_to_string(
                            binary, lang="rus", config=f"--psm {psm}"
                        ).strip()
                    except pytesseract.TesseractError:
                        recognized = ""
                    page_recognized = page_recognized or bool(recognized)
                    by_mode[psm][index] = recognized or page_text[index]
                if not page_recognized:
                    fallback = _recognize_legacy_pages(path, [image])
                    if fallback:
                        for psm in PSM_MODES:
                            by_mode[psm][index] = fallback[0]
            image.close()
        batch_start += len(batch)

    variants = ["\n\f\n".join(by_mode[psm]).strip() for psm in PSM_MODES]
    variants = [variant for variant in variants if variant]
    if variants:
        return variants
    # Preserve embedded text when the scanned pages yield no OCR output. The
    # parser will mark missing debtor evidence for employee review.
    embedded = "\n\f\n".join(page_text).strip()
    if embedded:
        return [embedded]
    raise RuntimeError(f"Не удалось распознать текст в {path}")


def recognize(path: Path, dpi: int = 300) -> list[str]:
    """Return OCR variants, keeping text-layer PDFs and photo OCR local."""
    path = Path(path)

    if path.suffix.lower() == ".pdf":
        embedded = pdf_text_layer(path)
        text_pages = _pdf_text_pages(embedded)
        # A failed/empty text extraction has no page separators. Ask Poppler
        # for the authoritative page count so an all-scan PDF is not truncated
        # to its first page by the fallback OCR path.
        if "\f" not in embedded and len(embedded.strip()) <= 400:
            page_count = int(pdfinfo_from_path(str(path))["Pages"])
            if page_count > len(text_pages):
                text_pages.extend("" for _ in range(page_count - len(text_pages)))
        # Fast path for ordinary text PDFs. In multi-page files, a blank or
        # fragmentary page can be a scan even when the document has a long text
        # layer elsewhere, so recover those pages independently.
        if len(embedded.strip()) > 400 and all(len(page.strip()) >= 120 for page in text_pages):
            return [embedded]
        return _recognize_pdf_pages(path, text_pages, dpi)

    pages = load_pages(path, dpi=dpi)
    return _recognize_legacy_pages(path, pages)
