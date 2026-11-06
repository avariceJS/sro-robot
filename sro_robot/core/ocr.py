"""Bounded local document reading with one primary and selective recovery OCR.

Useful PDF text bypasses OCR. Alternative readings are requested only for an
observable defect and remain distinct evidence for the existing parsers.
"""

from __future__ import annotations

import os
import re
import logging
import subprocess
import time
from dataclasses import dataclass, field
from contextlib import contextmanager
from math import ceil
from pathlib import Path

import pytesseract
from PIL import Image, ImageFilter, ImageOps
from pillow_heif import HeifImagePlugin as _HeifImagePlugin  # register Pillow's HEIF decoder
from pdf2image import convert_from_path, pdfinfo_from_path
from pdf2image.exceptions import (
    PDFInfoNotInstalledError,
    PDFPageCountError,
    PDFPopplerTimeoutError,
)

# Tesseract's OpenMP pool is slower than a single thread for these small local
# OCR jobs and multiplies contention across concurrent worker processes. Set
# the process-wide default inherited by Tesseract children, preserving overrides.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

# PSM 3 is primary; other segmentation modes are problem-driven recovery.
PRIMARY_PSM = 3
LOW_RESOLUTION_SCAN_PPI = 150
HEIC_OCR_MAX_SIDE = 3400
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic"}

# The defaults are tied to the current worker envelope, not to legal document
# semantics: a four-page synthetic 300-DPI scan took 17.1s locally, so 80 pages
# reserve roughly 43% of the 600s RQ timeout for variance, extraction and commit.
DEFAULT_MAX_PDF_PAGES = 80
DEFAULT_OCR_DEADLINE_SECONDS = 540
DEFAULT_PDFINFO_TIMEOUT_SECONDS = 20
DEFAULT_PDFTEXT_TIMEOUT_SECONDS = 60
DEFAULT_TESSERACT_TIMEOUT_SECONDS = 45
DEFAULT_MAX_IMAGE_PIXELS = 12_000_000
# Native phone photographs may exceed the OCR target by a small sensor-size
# rounding margin (for example 4032x3024). Keep that bounded excess and resize
# proportionally; larger images retain the existing fail-closed pixel limit.
HEIC_RESIZE_ALLOWANCE_PIXELS = 1_000_000
DEFAULT_MAX_TIFF_FRAMES = 80
MAX_CONFIGURED_TIFF_FRAMES = 1_000
PDF_RASTER_BATCH_SIZE = 2
logger = logging.getLogger(__name__)


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


class TiffFrameLimitExceeded(RuntimeError):
    """A TIFF contains more frames than the configured OCR admission bound."""

    def __init__(self, frames: int, maximum: int) -> None:
        super().__init__(f"TIFF has at least {frames} frames; configured maximum is {maximum}")
        self.frames = frames
        self.maximum = maximum

    @property
    def employee_message(self) -> str:
        return (
            f"В TIFF не менее {self.frames} страниц, текущий предел распознавания — "
            f"{self.maximum}. Разделите документ или попросите администратора "
            "изменить OCR_MAX_TIFF_FRAMES. Исходный файл сохранён."
        )


class TiffPagePixelLimitExceeded(RuntimeError):
    """A TIFF frame exceeds the configured per-page raster memory bound."""

    def __init__(self, page: int, pixels: int, maximum: int) -> None:
        super().__init__(f"TIFF page {page} has {pixels} pixels; configured maximum is {maximum}")
        self.page = page
        self.pixels = pixels
        self.maximum = maximum

    @property
    def employee_message(self) -> str:
        return (
            f"В странице документа {self.page} TIFF {self.pixels:,} пикс., "
            f"предел — {self.maximum:,}. Уменьшите страницу или попросите "
            "администратора изменить OCR_MAX_IMAGE_PIXELS. Исходный файл сохранён."
        ).replace(",", " ")


class TiffPageOcrFailed(RuntimeError):
    """Every Tesseract attempt failed for one TIFF page."""

    def __init__(self, page: int) -> None:
        super().__init__(f"Tesseract failed for every OCR mode on TIFF page {page}")
        self.page = page

    @property
    def employee_message(self) -> str:
        return (
            f"Не удалось распознать страницу документа {self.page} TIFF. "
            "Проверьте страницу и повторите обработку; исходный файл сохранён."
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
            "TIFF frame decoding": "чтении страницы TIFF",
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
    ocr_pages: set[int] = field(default_factory=set, init=False)
    text_layer_pages: set[int] = field(default_factory=set, init=False)
    recovery_reasons: list[str] = field(default_factory=list, init=False)

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


def _photo_rotation(image: Image.Image, budget: OcrBudget | None) -> int:
    """Return a confident clockwise correction when photo EXIF is insufficient."""
    if min(image.size) < 1600:
        return 0
    budget = budget or OcrBudget()
    try:
        osd = pytesseract.image_to_osd(
            image, config="--psm 0", timeout=budget.timeout(8, "photo orientation"),
        )
    except (pytesseract.TesseractError, RuntimeError):
        return 0
    rotation = re.search(r"(?m)^Rotate:\s*(0|90|180|270)\s*$", osd)
    confidence = re.search(r"(?m)^Orientation confidence:\s*([\d.]+)\s*$", osd)
    if rotation is None or confidence is None or float(confidence.group(1)) < 15:
        return 0
    return int(rotation.group(1))


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
                if path.suffix.lower() == ".heic":
                    return [decode_heic_page(source, max_image_pixels=max_image_pixels)]
                frame_count = getattr(source, "n_frames", 1)
                if frame_count > 1:
                    raise UnsupportedMultiFrameImage(f"Image contains {frame_count} frames")
                # Orientation changes shape but not pixel count; enforce the
                # memory bound before Pillow creates a transformed copy.
                pixels = source.width * source.height
                if pixels > max_image_pixels:
                    raise ImagePixelLimitExceeded(pixels, max_image_pixels)
                oriented = ImageOps.exif_transpose(source)
                return [oriented]
        except Image.DecompressionBombError:
            raise ImageDecompressionLimitExceeded(
                "Image dimensions exceed Pillow's hard safety limit"
            ) from None
    raise ValueError(f"Неподдерживаемый формат файла: {path.suffix}")


def decode_heic_page(source: Image.Image, *, max_image_pixels: int) -> Image.Image:
    """Decode one HEIC primary image, apply its orientation, and bound OCR pixels.

    pillow-heif retains the stripped EXIF/XMP orientation as
    ``original_orientation`` but does not expose it through Pillow's EXIF
    object. ISO-BMFF rotation/mirroring properties are already applied by
    libheif; apply EXIF orientation only when the header has no such transform.
    OCR boxes and browser preview therefore share one oriented pixel frame.
    """
    if source.format != "HEIF":
        raise OSError("HEIC filename does not contain a HEIF image")
    if getattr(source, "n_frames", 1) > 1:
        raise UnsupportedMultiFrameImage("HEIC contains multiple images")
    source_pixels = source.width * source.height
    if source_pixels > max_image_pixels + HEIC_RESIZE_ALLOWANCE_PIXELS:
        raise ImagePixelLimitExceeded(source_pixels, max_image_pixels)

    orientation = source.info.get("original_orientation")
    # libheif has already applied ISO-BMFF irot/imir properties to the pixels
    # it gives Pillow. pillow-heif records EXIF separately as
    # ``original_orientation``, so blindly applying that tag would rotate real
    # iPhone images twice. Its pinned Pillow adapter exposes the native
    # transformation tuple on the current HEIF frame.
    heif_file = getattr(source, "_heif_file", None)
    frame = None
    if heif_file is not None:
        try:
            frame = heif_file[source.tell()]
        except (IndexError, TypeError, ValueError):
            frame = None
    native = getattr(frame, "_c_image", None)
    transformations = getattr(native, "transformations", None)
    if transformations is None and orientation in range(2, 9):
        raise OSError("HEIC header orientation could not be verified")
    header_orientation_applied = False
    for transformation in transformations or ():
        if not transformation:
            continue
        kind, *values = transformation
        if kind == "irot" and values and values[0] not in (None, 0):
            header_orientation_applied = True
            break
        if kind == "imir":
            header_orientation_applied = True
            break
    transpose = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }
    if header_orientation_applied:
        oriented = source.copy()
    else:
        oriented = source.transpose(transpose[orientation]) if orientation in transpose else source.copy()
    oriented_pixels = oriented.width * oriented.height
    if oriented_pixels > max_image_pixels:
        scale = (max_image_pixels / oriented_pixels) ** 0.5
        width = max(1, int(oriented.width * scale))
        height = max(1, int(oriented.height * scale))
        while width * height > max_image_pixels:
            if width >= height:
                width -= 1
            else:
                height -= 1
        resized = oriented.resize((width, height), Image.Resampling.LANCZOS)
        oriented.close()
        return resized
    return oriented


def _iter_tiff_pages(
    path: Path,
    *,
    max_frames: int,
    max_image_pixels: int,
    budget: OcrBudget,
):
    """Yield one oriented TIFF page at a time after checking its header bounds."""
    if max_frames < 1:
        raise ValueError("Maximum TIFF frame count must be positive")
    if max_image_pixels < 1:
        raise ValueError("Maximum image pixel count must be positive")
    try:
        source_context = Image.open(path)
    except Image.DecompressionBombError:
        raise ImageDecompressionLimitExceeded(
            "TIFF dimensions exceed Pillow's hard safety limit"
        ) from None

    with source_context as source:
        if source.format != "TIFF":
            raise RuntimeError("TIFF file format could not be verified")
        for frame_index in range(max_frames + 1):
            budget.timeout(1, "TIFF frame decoding")
            try:
                source.seek(frame_index)
            except EOFError:
                return
            if frame_index >= max_frames:
                raise TiffFrameLimitExceeded(frame_index + 1, max_frames)
            width, height = source.size
            pixels = width * height
            if pixels > max_image_pixels:
                raise TiffPagePixelLimitExceeded(frame_index + 1, pixels, max_image_pixels)
            try:
                oriented = ImageOps.exif_transpose(source)
            except Image.DecompressionBombError:
                raise ImageDecompressionLimitExceeded(
                    "TIFF dimensions exceed Pillow's hard safety limit"
                ) from None
            try:
                yield frame_index + 1, oriented
            finally:
                oriented.close()


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


def _recovery_choice(text: str, decider=None) -> tuple[int, bool, str] | None:
    if len(text.strip()) < 32:
        return (11, False, "sparse_or_empty_text")
    return decider([text]) if decider is not None else None


def _recognize_pages(
    path: Path, pages: list[Image.Image], budget: OcrBudget | None = None,
    source_location_collector=None, *, photo: bool = False,
    original_pages: list[Image.Image] | None = None, recovery_decider=None, allow_empty=False,
) -> list[str]:
    """One primary pass; at most one problem-driven secondary pass per page.

    No copied primary text is introduced as an independent corroborating vote.
    Original detail is read only for an identified damaged identifier/apartment.
    TIFF keeps both per-page slots, including empty text, until page assembly.
    """
    budget = budget or OcrBudget()
    prepared = []
    outputs = []
    secondary = []
    rotations = []
    successes = 0
    timeouts = 0
    try:
        for index, page in enumerate(pages):
            scale = 1 if photo and min(page.size) >= 2000 else 2
            processed = preprocess(page, scale=scale)
            prepared.append(processed)
            def run(image, psm):
                nonlocal successes, timeouts
                try:
                    if source_location_collector is not None:
                        result = _tesseract_with_source(image, f"--psm {psm}", budget)
                    else:
                        result = _tesseract(image, f"--psm {psm}", budget), ""
                    successes += 1
                    return result
                except pytesseract.TesseractError:
                    return "", ""
                except OcrProcessingTimeout as exc:
                    if exc.stage != "Tesseract OCR":
                        raise
                    timeouts += 1
                    return "", ""
            text, tsv = run(processed, PRIMARY_PSM)
            outputs.append((text, tsv, processed.size))
            choice = _recovery_choice(text, recovery_decider)
            recovery_text, recovery_tsv, recovery_size, rotation = "", "", processed.size, 0
            if choice is not None:
                psm, use_original, reason = choice
                budget.recovery_reasons.append(reason)
                # Orientation detection is a recovery operation, not a mandatory
                # extra process for already readable phone documents.
                rotation = _photo_rotation(page, budget) if photo and reason == "sparse_or_empty_text" else 0
                if rotation:
                    oriented = processed.rotate(-rotation, expand=True)
                    try:
                        recovery_text, recovery_tsv = run(oriented, PRIMARY_PSM)
                        recovery_size = oriented.size
                    finally:
                        oriented.close()
                else:
                    original = (original_pages or pages)[index] if use_original else processed
                    recovery_text, recovery_tsv = run(original, psm)
                    recovery_size = original.size
            secondary.append((recovery_text, recovery_tsv, recovery_size))
            rotations.append(rotation)
        variants = []
        for page_outputs, angles in ((outputs, [0] * len(pages)), (secondary, rotations)):
            text = "\n\f\n".join(output[0] for output in page_outputs).strip()
            if not text:
                if allow_empty:
                    variants.append("")
                continue
            if source_location_collector is not None:
                # A photograph is one page. Multi-page raster source alignment
                # retains its existing page/line limitation rather than faking boxes.
                if len(pages) == 1:
                    source_location_collector.record_variant(len(variants), page_outputs, rotations=angles)
            variants.append(text)
        if not any(variant.strip() for variant in variants):
            if allow_empty and successes:
                return ["", ""]
            if timeouts:
                raise OcrProcessingTimeout("Tesseract OCR")
            raise RuntimeError("No usable OCR text")
        return variants
    finally:
        for image in prepared:
            # Test doubles may return the original; caller owns that image.
            if all(image is not page for page in pages):
                image.close()


@contextmanager
def _temporary_png_encoder(image: Image.Image):
    """Avoid compressing lossless OCR scratch PNGs; restore caller options."""
    previous = getattr(image, "encoderinfo", None)
    if image.format not in (None, "PNG"):
        yield
        return
    image.encoderinfo = {**(previous or {}), "compress_level": 0}
    try:
        yield
    finally:
        if previous is None:
            del image.encoderinfo
        else:
            image.encoderinfo = previous


def _tesseract_with_source(image: Image.Image, config: str, budget: OcrBudget | None) -> tuple[str, str]:
    """Return text and word TSV from one Tesseract invocation."""
    budget = budget or OcrBudget()
    try:
        with _temporary_png_encoder(image), pytesseract.pytesseract.save(image) as (temp_name, input_filename):
            pytesseract.pytesseract.run_tesseract(
                input_filename, temp_name, "txt tsv", "rus",
                config=f"{config} -c tessedit_create_tsv=1",
                timeout=budget.timeout(budget.tesseract_timeout_seconds, "Tesseract OCR"),
            )
            text = pytesseract.pytesseract._read_output(f"{temp_name}.txt", False)
            try:
                tsv = pytesseract.pytesseract._read_output(f"{temp_name}.tsv", False)
            except (OSError, UnicodeError):
                tsv = ""
            return text, tsv
    except RuntimeError as exc:
        if "timeout" in str(exc).lower():
            raise OcrProcessingTimeout("Tesseract OCR") from None
        raise


def _recognize_tiff_page_variants(
    page: Image.Image, budget: OcrBudget, *, page_number: int, recovery_decider=None,
) -> list[str]:
    try:
        return _recognize_pages(Path("page.tiff"), [page], budget,
                                recovery_decider=recovery_decider, allow_empty=True)
    except OcrProcessingTimeout:
        raise
    except RuntimeError as exc:
        raise TiffPageOcrFailed(page_number) from exc


def _recognize_tiff(
    path: Path,
    *,
    max_tiff_frames: int,
    max_image_pixels: int,
    budget: OcrBudget, recovery_decider=None,
) -> list[str]:
    """Recognize every TIFF page sequentially into complete document variants."""
    variant_pages: list[list[str]] = [[], []]
    page_count = 0
    pages = _iter_tiff_pages(
        path,
        max_frames=max_tiff_frames,
        max_image_pixels=max_image_pixels,
        budget=budget,
    )
    try:
        for page_number, page in pages:
            page_count += 1
            try:
                recognized = _recognize_tiff_page_variants(
                    page, budget, page_number=page_number, recovery_decider=recovery_decider,
                )
            finally:
                page.close()
            budget.ocr_pages.add(page_number)
            for variant_index, document_variant in enumerate(variant_pages):
                document_variant.append(recognized[variant_index] if variant_index < len(recognized) else "")
    finally:
        pages.close()
    if page_count == 0:
        raise RuntimeError("TIFF contains no readable pages")

    variants = [
        "\n\f\n".join(page_texts)
        for page_texts in variant_pages
        if any(page_text.strip() for page_text in page_texts)
    ]
    if not variants:
        raise RuntimeError(f"Не удалось распознать текст в {path}")
    return variants


def _tesseract(image: Image.Image, config: str, budget: OcrBudget | None) -> str:
    budget = budget or OcrBudget()
    try:
        with _temporary_png_encoder(image):
            return pytesseract.image_to_string(
                image, lang="rus", config=config,
                timeout=budget.timeout(budget.tesseract_timeout_seconds, "Tesseract OCR"),
            )
    except RuntimeError as exc:
        if "timeout" in str(exc).lower():
            raise OcrProcessingTimeout("Tesseract OCR") from None
        raise



def _pdf_text_pages(text: str) -> list[str]:
    pages = text.split("\f")
    # pdftotext normally emits one form feed per page, including a trailing
    # separator. Drop only that sentinel: an empty preceding page is an image
    # page and must remain available for OCR.
    if text.endswith("\f") and len(pages) > 1:
        pages.pop()
    return pages or [text]


def _recognize_pdf_pages(
    path: Path, text_pages: list[str], dpi: int, budget: OcrBudget | None = None,
    recovery_decider=None,
) -> list[str]:
    """Keep usable PDF text and OCR other pages in bounded raster batches."""
    # A short hidden layer often contains only stray glyphs from a scanned page.
    # Prefer a complete OCR rendering for that page, falling back to the text
    # layer only when a Tesseract mode returns no content.
    page_text = [page.strip() for page in text_pages]
    budget = budget or OcrBudget()
    budget.text_layer_pages.update(
        index + 1 for index, text in enumerate(page_text) if len(text) >= 120
    )
    scan_pages = [index for index, page in enumerate(page_text) if len(page) < 120]
    if not scan_pages:
        return ["\n\f\n".join(page_text)]

    by_mode: dict[int, list[str]] = {3: list(page_text), 6: ["" for _ in page_text]}
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
            # A genuinely blank separator page does not need Tesseract
            # calls. The threshold is tiny so faint text still reaches OCR.
            if sum(histogram[:245]) > 25:
                binary = gray.point(lambda pixel: 255 if pixel >= 215 else 0)
                budget.ocr_pages.add(index + 1)
                try:
                    recognized = _tesseract(binary, "--psm 3", budget).strip()
                except pytesseract.TesseractError:
                    recognized = ""
                by_mode[3][index] = recognized or page_text[index]
                choice = _recovery_choice(recognized, recovery_decider)
                if choice is not None:
                    psm, original, reason = choice
                    budget.recovery_reasons.append(reason)
                    try:
                        by_mode[6][index] = _tesseract(image if original else binary, f"--psm {psm}", budget).strip()
                    except pytesseract.TesseractError:
                        by_mode[6][index] = ""
                    except OcrProcessingTimeout as exc:
                        if exc.stage != "Tesseract OCR":
                            raise
                        by_mode[6][index] = ""
            image.close()
        batch_start += len(batch)

    variants = ["\n\f\n".join(by_mode[psm]) for psm in (3, 6)]
    variants = [variant for variant in variants if variant.strip()]
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
    max_tiff_frames: int = DEFAULT_MAX_TIFF_FRAMES,
    budget: OcrBudget | None = None,
    source_location_collector=None, recovery_decider=None,
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
            if source_location_collector is not None:
                try:
                    boxes = subprocess.run(
                        ["pdftotext", "-bbox-layout", str(path), "-"],
                        capture_output=True, text=True,
                        timeout=budget.timeout(budget.pdftext_timeout_seconds, "PDF source locations"),
                    )
                    if boxes.returncode == 0:
                        source_location_collector.record_pdf_text(0, embedded, boxes.stdout)
                except Exception as exc:
                    logger.warning("Optional PDF source locations unavailable error_type=%s", type(exc).__name__)
            return [embedded]
        scan_pages = [index for index, page in enumerate(text_pages) if len(page.strip()) < 120]
        if scan_pages:
            _check_pdf_page_pixel_limits(page_sizes, scan_pages, dpi, max_image_pixels)
        return _recognize_pdf_pages(path, text_pages, dpi, budget, recovery_decider=recovery_decider)

    if path.suffix.lower() in {".tif", ".tiff"}:
        return _recognize_tiff(
            path,
            max_tiff_frames=max_tiff_frames,
            max_image_pixels=max_image_pixels,
            budget=budget, recovery_decider=recovery_decider,
        )

    pages = load_pages(path, dpi=dpi, max_image_pixels=max_image_pixels)
    ocr_pages = pages
    if path.suffix.lower() == ".heic":
        # The bounded 12-MP decoder retains detail for previews. The current
        # Tesseract pass is more stable on phone documents at this smaller
        # raster size; proportional scaling preserves normalized word boxes.
        ocr_pages = [
            page.resize(
                (max(1, round(page.width * HEIC_OCR_MAX_SIDE / max(page.size))),
                 max(1, round(page.height * HEIC_OCR_MAX_SIDE / max(page.size)))),
                Image.Resampling.LANCZOS,
            ) if max(page.size) > HEIC_OCR_MAX_SIDE else page
            for page in pages
        ]
    try:
        budget.ocr_pages.add(1)
        return _recognize_pages(
            path, ocr_pages, budget,
            source_location_collector=(source_location_collector
                                       if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic"}
                                       else None),
            photo=True,
            original_pages=pages if ocr_pages is not pages else None,
            recovery_decider=recovery_decider,
        )
    finally:
        for original, resized in zip(pages, ocr_pages, strict=True):
            if resized is not original:
                resized.close()
            original.close()


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
        for psm in (3, 4, 6, 11):
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
    max_tiff_frames: int = DEFAULT_MAX_TIFF_FRAMES,
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
        if path.suffix.lower() in {".tif", ".tiff"}:
            variant_pages = [variant.split("\f") for variant in variants]
            recovered_pages: list[str] = []
            any_recovered = False
            pages = _iter_tiff_pages(
                path,
                max_frames=max_tiff_frames,
                max_image_pixels=max_image_pixels,
                budget=budget,
            )
            try:
                for page_number, image in pages:
                    try:
                        page_index = page_number - 1
                        page_text = "\n".join(
                            page_variants[page_index]
                            for page_variants in variant_pages
                            if page_index < len(page_variants)
                        )
                        text = ""
                        if re.search(r"ИНН", page_text, re.I):
                            gray = None
                            try:
                                gray = ImageOps.autocontrast(ImageOps.grayscale(image))
                                text = _tesseract(gray, "--psm 6", budget).strip()
                            except pytesseract.TesseractError:
                                text = ""
                            finally:
                                if gray is not None:
                                    gray.close()
                        recovered_pages.append(text)
                        any_recovered = any_recovered or bool(text)
                    finally:
                        image.close()
            finally:
                pages.close()
            if any_recovered:
                return ["\n\f\n".join(recovered_pages)]
            return []

        pages = load_pages(path, dpi=dpi, max_image_pixels=max_image_pixels)
        for image in pages:
            gray = None
            try:
                gray = ImageOps.autocontrast(ImageOps.grayscale(image))
                text = _tesseract(gray, "--psm 6", budget).strip()
                if text:
                    recovered.append(text)
            except pytesseract.TesseractError:
                continue
            finally:
                if gray is not None:
                    gray.close()
                image.close()
    return ["\n".join(recovered)] if recovered else []
