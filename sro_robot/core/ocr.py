"""Превращение сканов, фото и PDF в текст.

Фото с телефона Tesseract читает заметно хуже скана, поэтому каждый документ
прогоняется несколькими режимами сегментации, а выбор между результатами
делает парсер голосованием.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from math import ceil
from pathlib import Path

import pytesseract
from PIL import Image, ImageFilter, ImageOps
from pdf2image import convert_from_path, pdfinfo_from_path
from pdf2image.exceptions import (
    PDFInfoNotInstalledError,
    PDFPageCountError,
    PDFPopplerTimeoutError,
)

# psm 3 и 4 обычно лучшие на плотном тексте, 11 выручает на перекошенных фото.
PSM_MODES = (3, 4, 6, 11)
LOW_RESOLUTION_SCAN_PPI = 150
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

# The defaults are tied to the current worker envelope, not to legal document
# semantics: a four-page synthetic 300-DPI scan took 17.1s locally, so 80 pages
# reserve roughly 43% of the 600s RQ timeout for variance, extraction and commit.
DEFAULT_MAX_PDF_PAGES = 80
DEFAULT_OCR_DEADLINE_SECONDS = 540
DEFAULT_PDFINFO_TIMEOUT_SECONDS = 20
DEFAULT_PDFTEXT_TIMEOUT_SECONDS = 60
DEFAULT_TESSERACT_TIMEOUT_SECONDS = 45
DEFAULT_MAX_IMAGE_PIXELS = 12_000_000
PDF_RASTER_BATCH_SIZE = 2


class PdfPageLimitExceeded(RuntimeError):
    """A PDF exceeds the configured per-document OCR admission bound."""

    def __init__(self, pages: int, maximum: int) -> None:
        super().__init__(f"PDF has {pages} pages; configured maximum is {maximum}")
        self.pages = pages
        self.maximum = maximum

    @property
    def employee_message(self) -> str:
        return (
            f"В PDF {self.pages} стр., текущий предел распознавания — "
            f"{self.maximum}. Разделите документ или попросите администратора "
            "изменить OCR_MAX_PDF_PAGES. Исходный файл сохранён."
        )


class PdfPagePixelLimitExceeded(RuntimeError):
    """A PDF page would exceed the configured raster-memory admission bound."""

    def __init__(self, page: int, pixels: int, maximum: int) -> None:
        super().__init__(f"PDF page {page} raster has {pixels} pixels; configured maximum is {maximum}")
        self.page = page
        self.pixels = pixels
        self.maximum = maximum

    @property
    def employee_message(self) -> str:
        return (
            f"В странице PDF {self.page} после подготовки будет {self.pixels:,} пикс., "
            f"предел — {self.maximum:,}. Уменьшите страницу или попросите "
            "администратора изменить OCR_MAX_IMAGE_PIXELS. Исходный файл сохранён."
        ).replace(",", " ")


class PdfPageSizeUnavailable(RuntimeError):
    """Page dimensions could not be established before a PDF raster pass."""

    def __init__(self, page: int) -> None:
        super().__init__(f"PDF page {page} size is unavailable")
        self.page = page

    @property
    def employee_message(self) -> str:
        return (
            f"Не удалось безопасно определить размер страницы PDF {self.page}. "
            "Проверьте файл или сохраните его заново; исходный документ сохранён."
        )


class ImagePixelLimitExceeded(RuntimeError):
    """A raster image exceeds the configured per-image memory admission bound."""

    def __init__(self, pixels: int, maximum: int) -> None:
        super().__init__(f"Image has {pixels} pixels; configured maximum is {maximum}")
        self.pixels = pixels
        self.maximum = maximum

    @property
    def employee_message(self) -> str:
        return (
            f"В изображении {self.pixels:,} пикс., предел — {self.maximum:,}. "
            "Уменьшите разрешение или попросите администратора изменить "
            "OCR_MAX_IMAGE_PIXELS. Исходный файл сохранён."
        ).replace(",", " ")


class ImageDecompressionLimitExceeded(RuntimeError):
    """Pillow's hard decompression-bomb guard rejected the image header."""

    employee_message = (
        "Разрешение изображения превышает предел безопасного чтения. "
        "Уменьшите разрешение или сохраните страницы в PDF; исходный файл сохранён."
    )


class UnsupportedMultiFrameImage(RuntimeError):
    """A multi-frame raster must be split or supplied as a bounded PDF."""

    employee_message = (
        "Многостраничное изображение пока не поддерживается. Сохраните его "
        "страницы в PDF или загрузите отдельными изображениями. Исходный файл сохранён."
    )


class OcrProcessingTimeout(TimeoutError):
    """A bounded local PDF/OCR subprocess or total OCR budget expired."""

    def __init__(self, stage: str) -> None:
        super().__init__(f"OCR time limit exceeded during {stage}")
        self.stage = stage

    @property
    def employee_message(self) -> str:
        stages = {
            "PDF metadata": "чтении свойств PDF",
            "PDF text extraction": "извлечении текстового слоя PDF",
            "page rasterization": "подготовке изображений страниц",
            "Tesseract OCR": "распознавании страницы",
            "overall OCR": "обработке документа",
        }
        return f"Превышено ограничение времени при {stages.get(self.stage, 'обработке документа')}. Исходный файл сохранён; проверьте его и повторите обработку."


@dataclass
class OcrBudget:
    deadline_seconds: int = DEFAULT_OCR_DEADLINE_SECONDS
    pdfinfo_timeout_seconds: int = DEFAULT_PDFINFO_TIMEOUT_SECONDS
    pdftext_timeout_seconds: int = DEFAULT_PDFTEXT_TIMEOUT_SECONDS
    tesseract_timeout_seconds: int = DEFAULT_TESSERACT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        self._deadline = time.monotonic() + self.deadline_seconds

    def timeout(self, maximum: int, stage: str) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise OcrProcessingTimeout("overall OCR")
        return min(float(maximum), remaining)


def preprocess(image: Image.Image, scale: int = 2) -> Image.Image:
    """Серый, увеличенный и подрезанный по контрасту кадр читается точнее."""
    g = ImageOps.grayscale(image)
    if scale != 1:
        g = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
    g = ImageOps.autocontrast(g)
    return g.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))


def pdf_text_layer(path: Path, budget: OcrBudget | None = None) -> str:
    """У PDF из «Мой арбитр» есть текстовый слой — OCR тогда не нужен."""
    budget = budget or OcrBudget()
    try:
        res = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=budget.timeout(
                budget.pdftext_timeout_seconds,
                "PDF text extraction",
            ),
        )
    except (subprocess.TimeoutExpired, PDFPopplerTimeoutError):
        raise OcrProcessingTimeout("PDF text extraction") from None
    except OSError:
        return ""
    return res.stdout if res.returncode == 0 else ""


def load_pages(
    path: Path, dpi: int = 300, *, max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS
) -> list[Image.Image]:
    if path.suffix.lower() == ".pdf":
        return convert_from_path(str(path), dpi=dpi)
    if path.suffix.lower() in IMAGE_SUFFIXES:
        try:
            with Image.open(path) as source:
                frame_count = getattr(source, "n_frames", 1)
                if frame_count > 1:
                    raise UnsupportedMultiFrameImage(f"Image contains {frame_count} frames")
                pixels = source.width * source.height
                if pixels > max_image_pixels:
                    raise ImagePixelLimitExceeded(pixels, max_image_pixels)
                return [source.copy()]
        except Image.DecompressionBombError:
            raise ImageDecompressionLimitExceeded(
                "Image dimensions exceed Pillow's hard safety limit"
            ) from None
    raise ValueError(f"Неподдерживаемый формат файла: {path.suffix}")


def _load_pdf_pages(
    path: Path,
    first_page: int,
    last_page: int,
    dpi: int,
    budget: OcrBudget | None = None,
) -> list[Image.Image]:
    """Rasterize a bounded 1-based page range to cap per-worker image memory."""
    budget = budget or OcrBudget()
    try:
        return convert_from_path(
            str(path), dpi=dpi, first_page=first_page, last_page=last_page,
            timeout=budget.timeout(
                budget.pdfinfo_timeout_seconds,
                "page rasterization",
            ),
        )
    except PDFPopplerTimeoutError:
        raise OcrProcessingTimeout("page rasterization") from None


def _recognize_legacy_pages(
    path: Path, pages: list[Image.Image], budget: OcrBudget | None = None
) -> list[str]:
    """Existing upscale/sharpen plus original-image OCR used for photographs."""
    prepared = [preprocess(p) for p in pages]

    variants: list[str] = []
    for psm in PSM_MODES:
        config = f"--psm {psm}"
        chunks = []
        for page in prepared:
            try:
                chunks.append(_tesseract(page, config, budget))
            except pytesseract.TesseractError:
                chunks.append("")
        text = "\n".join(chunks).strip()
        if text:
            variants.append(text)

    # Оригинал без обработки иногда точнее на чистых сканах.
    for page in pages:
        try:
            text = _tesseract(page, "--psm 3", budget).strip()
        except pytesseract.TesseractError:
            text = ""
        if text:
            variants.append(text)

    if not variants:
        raise RuntimeError(f"Не удалось распознать текст в {path}")
    return variants


def _tesseract(image: Image.Image, config: str, budget: OcrBudget | None) -> str:
    budget = budget or OcrBudget()
    try:
        return pytesseract.image_to_string(
            image, lang="rus", config=config,
            timeout=budget.timeout(budget.tesseract_timeout_seconds, "Tesseract OCR"),
        )
    except RuntimeError as exc:
        if "timeout" in str(exc).lower():
            raise OcrProcessingTimeout("Tesseract OCR") from None
        raise


def _recognize_scanned_pdf(
    path: Path,
    pages: list[Image.Image],
    cutoff: int = 215,
    budget: OcrBudget | None = None,
) -> list[str]:
    """Use a bounded binary pass for PDF scans; fall back to the legacy pass."""
    variants: list[str] = []
    for page in pages:
        gray = ImageOps.autocontrast(ImageOps.grayscale(page))
        binary = gray.point(lambda pixel: 255 if pixel >= cutoff else 0)
        for psm in PSM_MODES:
            try:
                text = _tesseract(binary, f"--psm {psm}", budget).strip()
            except pytesseract.TesseractError:
                text = ""
            if text:
                variants.append(text)
    if variants:
        return variants
        return _recognize_legacy_pages(path, pages, budget)


def _pdf_text_pages(text: str) -> list[str]:
    pages = text.split("\f")
    # pdftotext normally emits one form feed per page, including a trailing
    # separator. Drop only that sentinel: an empty preceding page is an image
    # page and must remain available for OCR.
    if text.endswith("\f") and len(pages) > 1:
        pages.pop()
    return pages or [text]


def _recognize_pdf_pages(
    path: Path, text_pages: list[str], dpi: int, budget: OcrBudget | None = None
) -> list[str]:
    """Keep usable PDF text and OCR other pages in bounded raster batches."""
    # A short hidden layer often contains only stray glyphs from a scanned page.
    # Prefer a complete OCR rendering for that page, falling back to the text
    # layer only when a Tesseract mode returns no content.
    page_text = [page.strip() for page in text_pages]
    budget = budget or OcrBudget()
    scan_pages = [index for index, page in enumerate(page_text) if len(page) < 120]
    if not scan_pages:
        return ["\n\f\n".join(page_text)]

    by_mode: dict[int, list[str]] = {psm: list(page_text) for psm in PSM_MODES}
    # Peak RSS measured about 574 MiB for a four-page synthetic scan at 300 DPI.
    # Two pages reduce retained raster memory while avoiding per-page Poppler runs.
    batch_size = PDF_RASTER_BATCH_SIZE
    batch_start = 0
    while batch_start < len(scan_pages):
        batch = [scan_pages[batch_start]]
        while (
            batch_start + len(batch) < len(scan_pages)
            and len(batch) < batch_size
            and scan_pages[batch_start + len(batch)] == batch[-1] + 1
        ):
            batch.append(scan_pages[batch_start + len(batch)])
        images = _load_pdf_pages(path, batch[0] + 1, batch[-1] + 1, dpi, budget)
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
                        recognized = _tesseract(binary, f"--psm {psm}", budget).strip()
                    except pytesseract.TesseractError:
                        recognized = ""
                    page_recognized = page_recognized or bool(recognized)
                    by_mode[psm][index] = recognized or page_text[index]
                if not page_recognized:
                    fallback = _recognize_legacy_pages(path, [image], budget)
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


def recognize(
    path: Path,
    dpi: int = 300,
    *,
    max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES,
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
    budget: OcrBudget | None = None,
) -> list[str]:
    """Return OCR variants, keeping text-layer PDFs and photo OCR local."""
    path = Path(path)
    budget = budget or OcrBudget()

    if path.suffix.lower() == ".pdf":
        page_count, page_sizes = _pdf_page_metadata(path, budget, max_pdf_pages)
        if page_count > max_pdf_pages:
            raise PdfPageLimitExceeded(page_count, max_pdf_pages)
        embedded = pdf_text_layer(path, budget)
        text_pages = _pdf_text_pages(embedded)
        # A failed/empty text extraction has no page separators. Ask Poppler
        # for the authoritative page count so an all-scan PDF is not truncated
        # to its first page by the fallback OCR path.
        if "\f" not in embedded and len(embedded.strip()) <= 400:
            if page_count > len(text_pages):
                text_pages.extend("" for _ in range(page_count - len(text_pages)))
        # Fast path for ordinary text PDFs. In multi-page files, a blank or
        # fragmentary page can be a scan even when the document has a long text
        # layer elsewhere, so recover those pages independently.
        if len(embedded.strip()) > 400 and all(len(page.strip()) >= 120 for page in text_pages):
            return [embedded]
        scan_pages = [index for index, page in enumerate(text_pages) if len(page.strip()) < 120]
        if scan_pages:
            _check_pdf_page_pixel_limits(page_sizes, scan_pages, dpi, max_image_pixels)
        return _recognize_pdf_pages(path, text_pages, dpi, budget)

    pages = load_pages(path, dpi=dpi, max_image_pixels=max_image_pixels)
    return _recognize_legacy_pages(path, pages, budget)


def _pdf_page_metadata(
    path: Path, budget: OcrBudget, max_pages: int,
) -> tuple[int, list[tuple[float, float] | None]]:
    try:
        info = pdfinfo_from_path(
            str(path), timeout=budget.timeout(budget.pdfinfo_timeout_seconds, "PDF metadata"),
            first_page=1, last_page=max_pages,
        )
        count = int(info["Pages"])
    except (subprocess.TimeoutExpired, PDFPopplerTimeoutError):
        raise OcrProcessingTimeout("PDF metadata") from None
    except (PDFInfoNotInstalledError, PDFPageCountError):
        raise RuntimeError("Unable to read PDF page count") from None
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("PDF page count is unavailable") from exc
    if count < 1:
        raise RuntimeError("PDF page count is invalid")
    normalized_info = {re.sub(r"\s+", " ", key.strip()): value for key, value in info.items()}
    sizes: list[tuple[float, float] | None] = []
    for page in range(1, min(count, max_pages) + 1):
        raw_size = normalized_info.get(f"Page {page} size")
        if raw_size is None and page == 1:
            raw_size = normalized_info.get("Page size")
        match = re.match(r"\s*([\d.]+)\s+x\s+([\d.]+)\s+pts\b", str(raw_size or ""))
        if match is None:
            sizes.append(None)
            continue
        try:
            width, height = float(match.group(1)), float(match.group(2))
        except ValueError:
            sizes.append(None)
            continue
        sizes.append((width, height) if width > 0 and height > 0 else None)
    return count, sizes


def _pdf_page_count(path: Path, budget: OcrBudget) -> int:
    return _pdf_page_metadata(path, budget, DEFAULT_MAX_PDF_PAGES)[0]


def _check_pdf_page_pixel_limits(
    page_sizes: list[tuple[float, float] | None],
    page_indices: list[int],
    dpi: int,
    max_image_pixels: int,
) -> None:
    for index in page_indices:
        if index >= len(page_sizes) or page_sizes[index] is None:
            raise PdfPageSizeUnavailable(index + 1)
        width_points, height_points = page_sizes[index]
        pixels = ceil(width_points * dpi / 72) * ceil(height_points * dpi / 72)
        if pixels > max_image_pixels:
            raise PdfPagePixelLimitExceeded(index + 1, pixels, max_image_pixels)


def _first_page_has_low_resolution_image(path: Path, budget: OcrBudget) -> bool:
    """Use Poppler's embedded-image geometry to gate a page-one date cross-check."""
    try:
        result = subprocess.run(
            ["pdfimages", "-list", "-f", "1", "-l", "1", str(path)],
            capture_output=True,
            text=True,
            timeout=budget.timeout(budget.pdfinfo_timeout_seconds, "PDF image metadata"),
        )
    except (OcrProcessingTimeout, subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 14 or columns[0] != "1" or columns[2] != "image":
            continue
        try:
            x_ppi, y_ppi = float(columns[12]), float(columns[13])
        except ValueError:
            continue
        if 0 < x_ppi <= LOW_RESOLUTION_SCAN_PPI and 0 < y_ppi <= LOW_RESOLUTION_SCAN_PPI:
            return True
    return False


def recover_low_resolution_header(
    path: Path,
    *,
    budget: OcrBudget | None = None,
    max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES,
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
) -> list[str]:
    """Return first-page 200-DPI OCR only for scans with a low embedded PPI."""
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        return []
    budget = budget or OcrBudget()
    if not _first_page_has_low_resolution_image(path, budget):
        return []
    page_count, page_sizes = _pdf_page_metadata(path, budget, max_pdf_pages)
    if page_count > max_pdf_pages:
        raise PdfPageLimitExceeded(page_count, max_pdf_pages)
    if page_count < 1:
        return []
    _check_pdf_page_pixel_limits(page_sizes, [0], 200, max_image_pixels)
    images = _load_pdf_pages(path, 1, 1, 200, budget)
    if not images:
        return []
    image = images[0]
    gray = ImageOps.autocontrast(ImageOps.grayscale(image))
    binary = gray.point(lambda pixel: 255 if pixel >= 215 else 0)
    variants: list[str] = []
    try:
        for psm in PSM_MODES:
            try:
                text = _tesseract(binary, f"--psm {psm}", budget).strip()
            except pytesseract.TesseractError:
                text = ""
            if text:
                variants.append(text)
    finally:
        binary.close()
        gray.close()
        image.close()
    return variants


def recover_role_bound_inn(
    path: Path,
    variants: list[str],
    dpi: int = 300,
    *,
    budget: OcrBudget | None = None,
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
) -> list[str]:
    """Offer one cautious grayscale OCR suggestion when a labeled INN was damaged.

    This pass is called only after extraction finds a debtor role and a damaged
    INN token. It remains a separate OCR variant so the parser retains the
    candidate as partial evidence and the automatic-card gate stays closed.
    """
    path = Path(path)
    budget = budget or OcrBudget()
    recovered: list[str] = []
    if path.suffix.lower() == ".pdf":
        page_count, page_sizes = _pdf_page_metadata(path, budget, DEFAULT_MAX_PDF_PAGES)
        if page_count > DEFAULT_MAX_PDF_PAGES:
            raise PdfPageLimitExceeded(page_count, DEFAULT_MAX_PDF_PAGES)
        embedded = pdf_text_layer(path, budget)
        text_pages = _pdf_text_pages(embedded)
        if "\f" not in embedded and len(embedded.strip()) <= 400:
            text_pages.extend("" for _ in range(max(0, page_count - len(text_pages))))
        variant_pages = [variant.split("\f") for variant in variants]
        candidate_pages = []
        for index, page in enumerate(text_pages):
            if len(page.strip()) >= 120:
                continue
            page_text = "\n".join(
                pages[index] for pages in variant_pages if index < len(pages)
            )
            if re.search(r"ИНН", page_text, re.I):
                candidate_pages.append(index)
        _check_pdf_page_pixel_limits(page_sizes, candidate_pages, dpi, max_image_pixels)
        recovered_pages = [""] * len(text_pages)
        for index in candidate_pages:
            images = _load_pdf_pages(path, index + 1, index + 1, dpi, budget)
            if not images:
                continue
            image = images[0]
            gray = None
            try:
                gray = ImageOps.autocontrast(ImageOps.grayscale(image))
                recovered_pages[index] = _tesseract(gray, "--psm 6", budget).strip()
            except pytesseract.TesseractError:
                recovered_pages[index] = ""
            finally:
                if gray is not None:
                    gray.close()
                image.close()
        if any(recovered_pages):
            recovered.append("\n\f\n".join(recovered_pages))
        return recovered

    if path.suffix.lower() in IMAGE_SUFFIXES:
        pages = load_pages(path, dpi=dpi, max_image_pixels=max_image_pixels)
        for image in pages:
            gray = None
            try:
                gray = ImageOps.autocontrast(ImageOps.grayscale(image))
                text = pytesseract.image_to_string(gray, lang="rus", config="--psm 6").strip()
                if text:
                    recovered.append(text)
            except pytesseract.TesseractError:
                continue
            finally:
                if gray is not None:
                    gray.close()
                image.close()
    return ["\n".join(recovered)] if recovered else []
