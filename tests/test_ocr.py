from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zlib

from PIL import Image

from sro_robot.core import ocr


def pdfinfo_with_pages(page_count: int) -> dict[str, object]:
    info: dict[str, object] = {"Pages": str(page_count)}
    for page in range(1, page_count + 1):
        info[f"Page {page} size"] = "72 x 72 pts"
    return info


class OCRPathTests(unittest.TestCase):
    def test_text_layer_pdf_skips_rasterization_and_ocr(self) -> None:
        path = Path("synthetic-text.pdf")
        embedded = "synthetic text layer " * 30
        with (
            patch.object(ocr, "pdf_text_layer", return_value=embedded),
            patch.object(ocr, "pdfinfo_from_path", return_value=pdfinfo_with_pages(1)),
            patch.object(ocr, "load_pages") as load_pages,
            patch.object(ocr.pytesseract, "image_to_string") as image_to_string,
        ):
            self.assertEqual(ocr.recognize(path), [embedded])
        load_pages.assert_not_called()
        image_to_string.assert_not_called()

    def test_mixed_pdf_ocr_recovers_a_blank_text_layer_page(self) -> None:
        cover = "synthetic searchable cover " * 28
        text_layer = cover + "\f\f"
        with (
            patch.object(ocr, "pdf_text_layer", return_value=text_layer),
            patch.object(ocr, "pdfinfo_from_path", return_value=pdfinfo_with_pages(2)),
            patch.object(ocr, "_load_pdf_pages", return_value=[Image.new("L", (100, 100))]) as raster,
            patch.object(ocr.pytesseract, "image_to_string", return_value="Должник: Иванова Анна Сергеевна") as recognize,
        ):
            variants = ocr.recognize(Path("synthetic-mixed.pdf"))

        self.assertEqual(len(variants), len(ocr.PSM_MODES))
        self.assertTrue(all(cover.strip() in variant for variant in variants))
        self.assertTrue(all("Должник: Иванова Анна Сергеевна" in variant for variant in variants))
        self.assertEqual(raster.call_args.args[:4], (Path("synthetic-mixed.pdf"), 2, 2, 300))
        self.assertEqual(recognize.call_count, len(ocr.PSM_MODES))

    def test_searchable_multipage_pdf_keeps_fast_path(self) -> None:
        embedded = ("synthetic searchable page " * 12) + "\f" + ("second searchable page " * 12)
        with (
            patch.object(ocr, "pdf_text_layer", return_value=embedded),
            patch.object(ocr, "pdfinfo_from_path", return_value=pdfinfo_with_pages(2)),
            patch.object(ocr, "_load_pdf_pages") as raster,
            patch.object(ocr.pytesseract, "image_to_string") as image_to_string,
        ):
            self.assertEqual(ocr.recognize(Path("synthetic-text.pdf")), [embedded])
        raster.assert_not_called()
        image_to_string.assert_not_called()

    def test_low_resolution_header_recovery_rasters_only_page_one_at_200_dpi(self) -> None:
        path = Path("synthetic-low-resolution.pdf")
        listing = "page num type width height color comp bpc enc interp object ID x-ppi y-ppi size ratio\n1 0 image 1156 1496 gray 1 8 image no 3 0 136 136 69.7K 4.1%\n"
        image = Image.new("L", (100, 100), 255)
        with (
            patch.object(
                ocr.subprocess,
                "run",
                return_value=ocr.subprocess.CompletedProcess([], 0, listing, ""),
            ) as pdfimages,
            patch.object(ocr, "_pdf_page_metadata", return_value=(1, [(612, 792)])),
            patch.object(ocr, "_load_pdf_pages", return_value=[image]) as raster,
            patch.object(ocr, "_tesseract", return_value="ОПРЕДЕЛЕНИЕ 7 июля 2099 года") as recognize,
        ):
            variants = ocr.recover_low_resolution_header(path)

        self.assertEqual(variants, ["ОПРЕДЕЛЕНИЕ 7 июля 2099 года"] * len(ocr.PSM_MODES))
        pdfimages.assert_called_once()
        raster.assert_called_once()
        self.assertEqual(raster.call_args.args[:4], (path, 1, 1, 200))
        self.assertEqual(recognize.call_count, len(ocr.PSM_MODES))

    def test_low_resolution_header_recovery_skips_images_over_ppi_threshold(self) -> None:
        path = Path("synthetic-standard-scan.pdf")
        listing = "1 0 image 1530 1980 gray 1 8 image no 3 0 180 180 101K 3.4%\n"
        with (
            patch.object(
                ocr.subprocess,
                "run",
                return_value=ocr.subprocess.CompletedProcess([], 0, listing, ""),
            ),
            patch.object(ocr, "_pdf_page_metadata") as metadata,
            patch.object(ocr, "_load_pdf_pages") as raster,
        ):
            self.assertEqual(ocr.recover_low_resolution_header(path), [])

        metadata.assert_not_called()
        raster.assert_not_called()

    def test_blank_page_in_mixed_pdf_does_not_run_tesseract(self) -> None:
        cover = "synthetic searchable cover " * 28
        with (
            patch.object(ocr, "pdf_text_layer", return_value=cover + "\f\f"),
            patch.object(ocr, "pdfinfo_from_path", return_value=pdfinfo_with_pages(2)),
            patch.object(ocr, "_load_pdf_pages", return_value=[Image.new("L", (20, 20), 255)]),
            patch.object(ocr.pytesseract, "image_to_string") as image_to_string,
        ):
            variants = ocr.recognize(Path("synthetic-mixed.pdf"))

        self.assertTrue(variants)
        image_to_string.assert_not_called()

    def test_multipage_pdf_rasterization_is_bounded_to_two_page_batches(self) -> None:
        text_pages = ["searchable text " * 10] + [""] * 9
        calls = []

        def raster(_path, first_page, last_page, _dpi, _budget=None):
            calls.append((first_page, last_page))
            return [Image.new("L", (20, 20), 255) for _ in range(last_page - first_page + 1)]

        with patch.object(ocr, "_load_pdf_pages", side_effect=raster):
            variants = ocr._recognize_pdf_pages(Path("synthetic-long.pdf"), text_pages, 300)

        self.assertEqual(calls, [(2, 3), (4, 5), (6, 7), (8, 9), (10, 10)])
        self.assertEqual(len(variants), len(ocr.PSM_MODES))

    def test_scanned_pdf_uses_four_binary_psm_passes(self) -> None:
        path = Path("synthetic-scan.pdf")
        page = Image.new("L", (100, 100), 255)
        page.putpixel((0, 0), 0)
        page.putpixel((1, 0), 210)
        page.putpixel((2, 0), 220)
        for x in range(4, 31):
            page.putpixel((x, 0), 0)
        seen_images = []

        def recognize_image(image, *, lang: str, config: str, timeout: float) -> str:
            seen_images.append(([image.getpixel((x, 0)) for x in range(image.width)], lang, config, timeout))
            return "synthetic OCR"

        with (
            patch.object(ocr, "pdf_text_layer", return_value=""),
            patch.object(ocr, "pdfinfo_from_path", return_value=pdfinfo_with_pages(1)),
            patch.object(ocr, "_load_pdf_pages", return_value=[page]),
            patch.object(ocr.pytesseract, "image_to_string", side_effect=recognize_image),
            patch.object(ocr, "preprocess") as preprocess,
        ):
            variants = ocr.recognize(path)

        self.assertEqual(variants, ["synthetic OCR"] * 4)
        self.assertEqual([row[2] for row in seen_images], [f"--psm {psm}" for psm in ocr.PSM_MODES])
        self.assertTrue(all(row[1] == "rus" for row in seen_images))
        self.assertTrue(all(0 < row[3] <= ocr.DEFAULT_TESSERACT_TIMEOUT_SECONDS for row in seen_images))
        self.assertEqual([row[0][:4] for row in seen_images], [[0, 0, 255, 255]] * 4)
        preprocess.assert_not_called()

    def test_scanned_pdf_falls_back_to_legacy_pass_when_binary_ocr_is_empty(self) -> None:
        path = Path("synthetic-empty-scan.pdf")
        page = Image.new("L", (100, 100), 255)
        with (
            patch.object(ocr, "_tesseract", return_value=""),
            patch.object(ocr, "_recognize_legacy_pages", return_value=["legacy synthetic text"]) as legacy,
        ):
            variants = ocr._recognize_scanned_pdf(path, [page])

        self.assertEqual(variants, ["legacy synthetic text"])
        legacy.assert_called_once_with(path, [page], None)

    def test_image_upload_keeps_legacy_preprocessed_and_original_passes(self) -> None:
        path = Path("synthetic-photo.png")
        page = Image.new("L", (100, 100), 255)
        for x in range(30):
            page.putpixel((x, 0), 0)
        with (
            patch.object(ocr, "load_pages", return_value=[page]),
            patch.object(ocr, "preprocess", return_value=page) as preprocess,
            patch.object(ocr.pytesseract, "image_to_string", return_value="photo text") as ocr_call,
        ):
            variants = ocr.recognize(path)

        self.assertEqual(len(variants), 5)
        self.assertEqual(preprocess.call_count, 1)
        self.assertEqual(ocr_call.call_count, 5)

    def test_image_upload_applies_exif_orientation_before_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic-phone-photo.jpg"
            image = Image.new("RGB", (40, 20), "white")
            exif = Image.Exif()
            exif[274] = 6
            image.save(path, "JPEG", exif=exif)

            transposed_images = []
            exif_transpose = ocr.ImageOps.exif_transpose

            def track_transpose(source):
                transposed = exif_transpose(source)
                transposed_images.append(transposed)
                return transposed

            with patch.object(ocr.ImageOps, "exif_transpose", side_effect=track_transpose):
                pages = ocr.load_pages(path)

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].size, (20, 40))
        self.assertIs(pages[0], transposed_images[0])

    def test_image_without_exif_remains_readable_after_source_context_closes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic-plain-photo.png"
            Image.new("RGB", (12, 8), (18, 36, 54)).save(path, "PNG")

            pages = ocr.load_pages(path)

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].size, (12, 8))
        self.assertEqual(pages[0].getpixel((4, 3)), (18, 36, 54))

    def test_failed_binary_pass_falls_back_to_legacy_scan_pipeline(self) -> None:
        path = Path("synthetic-scan.pdf")
        page = Image.new("L", (100, 100), 255)
        for x in range(30):
            page.putpixel((x, 0), 0)
        responses = ["", "", "", "", "legacy text", "", "", "", ""]
        with (
            patch.object(ocr, "pdf_text_layer", return_value=""),
            patch.object(ocr, "pdfinfo_from_path", return_value=pdfinfo_with_pages(1)),
            patch.object(ocr, "_load_pdf_pages", return_value=[page]),
            patch.object(ocr, "preprocess", return_value=page),
            patch.object(ocr.pytesseract, "image_to_string", side_effect=responses) as ocr_call,
        ):
            variants = ocr.recognize(path)

        self.assertEqual(variants, ["legacy text"] * len(ocr.PSM_MODES))
        self.assertEqual(ocr_call.call_count, 9)

    def test_empty_text_layer_uses_pdf_page_count_for_all_scan_pages(self) -> None:
        calls = []

        def raster(_path, first_page, last_page, _dpi, _budget=None):
            calls.append((first_page, last_page))
            return [Image.new("L", (100, 100), 0) for _ in range(last_page - first_page + 1)]

        with (
            patch.object(ocr, "pdf_text_layer", return_value=""),
            patch.object(ocr, "pdfinfo_from_path", return_value=pdfinfo_with_pages(2)),
            patch.object(ocr, "_load_pdf_pages", side_effect=raster),
            patch.object(ocr.pytesseract, "image_to_string", return_value="synthetic OCR"),
        ):
            variants = ocr.recognize(Path("synthetic-empty-layer-multipage.pdf"))

        self.assertEqual(calls, [(1, 2)])
        self.assertEqual(len(variants), len(ocr.PSM_MODES))
        self.assertTrue(all("synthetic OCR" in variant for variant in variants))

    def test_page_limit_is_checked_before_text_extraction_even_for_searchable_pdfs(self) -> None:
        with (
            patch.object(ocr, "pdfinfo_from_path", return_value=pdfinfo_with_pages(81)),
            patch.object(ocr, "pdf_text_layer") as text_layer,
        ):
            with self.assertRaises(ocr.PdfPageLimitExceeded) as raised:
                ocr.recognize(Path("synthetic-searchable-oversize.pdf"))
        self.assertEqual((raised.exception.pages, raised.exception.maximum), (81, 80))
        self.assertIn("OCR_MAX_PDF_PAGES", raised.exception.employee_message)
        text_layer.assert_not_called()

    def test_pdf_page_limit_is_configurable(self) -> None:
        with (
            patch.object(ocr, "pdfinfo_from_path", return_value=pdfinfo_with_pages(3)),
            patch.object(ocr, "pdf_text_layer", return_value="x" * 300),
            patch.object(ocr, "_recognize_pdf_pages", return_value=["ok"]) as parse,
        ):
            result = ocr.recognize(Path("synthetic-searchable.pdf"), max_pdf_pages=3)
        self.assertEqual(result, ["ok"])
        parse.assert_called_once()

    def test_pdfinfo_call_uses_configured_timeout_and_rejects_invalid_count(self) -> None:
        budget = ocr.OcrBudget(pdfinfo_timeout_seconds=7)
        with patch.object(ocr, "pdfinfo_from_path", return_value={"Pages": "0"}) as info:
            with self.assertRaisesRegex(RuntimeError, "page count is invalid"):
                ocr._pdf_page_count(Path("synthetic-invalid.pdf"), budget)
        self.assertEqual(info.call_args.kwargs["timeout"], 7.0)

    def test_pdfinfo_reads_page_dimensions_for_the_raster_range(self) -> None:
        info = pdfinfo_with_pages(2)
        info["Page    1 size"] = "612 x 792 pts (letter)"
        info["Page    2 size"] = "21600 x 21600 pts"
        with patch.object(ocr, "pdfinfo_from_path", return_value=info) as query:
            page_count, sizes = ocr._pdf_page_metadata(
                Path("synthetic-mixed-size.pdf"), ocr.OcrBudget(), 80,
            )
        self.assertEqual(page_count, 2)
        self.assertEqual(sizes, [(612.0, 792.0), (21600.0, 21600.0)])
        self.assertEqual(query.call_args.kwargs["first_page"], 1)
        self.assertEqual(query.call_args.kwargs["last_page"], 80)

    def test_oversized_scanned_pdf_page_is_rejected_before_rasterization(self) -> None:
        info = {"Pages": "1", "Page    1 size": "21600 x 21600 pts"}
        with (
            patch.object(ocr, "pdfinfo_from_path", return_value=info),
            patch.object(ocr, "pdf_text_layer", return_value="short scan page"),
            patch.object(ocr, "_load_pdf_pages") as raster,
        ):
            with self.assertRaises(ocr.PdfPagePixelLimitExceeded) as raised:
                ocr.recognize(Path("synthetic-oversized-page.pdf"))
        self.assertEqual(raised.exception.page, 1)
        self.assertGreater(raised.exception.pixels, ocr.DEFAULT_MAX_IMAGE_PIXELS)
        self.assertIn("странице PDF 1", raised.exception.employee_message)
        self.assertNotIn("synthetic-oversized-page.pdf", raised.exception.employee_message)
        raster.assert_not_called()

    def test_scanned_pdf_with_unknown_page_size_fails_closed_before_rasterization(self) -> None:
        with (
            patch.object(ocr, "pdfinfo_from_path", return_value={"Pages": "1"}),
            patch.object(ocr, "pdf_text_layer", return_value="short scan page"),
            patch.object(ocr, "_load_pdf_pages") as raster,
        ):
            with self.assertRaises(ocr.PdfPageSizeUnavailable):
                ocr.recognize(Path("synthetic-unknown-page-size.pdf"))
        raster.assert_not_called()

    def test_pdfinfo_timeout_becomes_an_actionable_bounded_ocr_failure(self) -> None:
        with patch.object(ocr, "pdfinfo_from_path", side_effect=ocr.PDFPopplerTimeoutError("timed out")):
            with self.assertRaises(ocr.OcrProcessingTimeout) as raised:
                ocr._pdf_page_count(Path("synthetic-slow-metadata.pdf"), ocr.OcrBudget())
        self.assertIn("чтении свойств PDF", raised.exception.employee_message)

    def test_pdf_raster_timeout_is_bounded_and_reported_without_path(self) -> None:
        with patch.object(ocr, "convert_from_path", side_effect=ocr.PDFPopplerTimeoutError("timeout")) as convert:
            with self.assertRaises(ocr.OcrProcessingTimeout) as raised:
                ocr._load_pdf_pages(Path("private.pdf"), 1, 2, 300)
        self.assertIn("подготовке изображений страниц", raised.exception.employee_message)
        self.assertNotIn("private.pdf", raised.exception.employee_message)
        self.assertLessEqual(convert.call_args.kwargs["timeout"], ocr.DEFAULT_PDFINFO_TIMEOUT_SECONDS)

    def test_tesseract_timeout_is_reported_as_a_bounded_ocr_failure(self) -> None:
        page = Image.new("L", (10, 10), 255)
        with patch.object(ocr.pytesseract, "image_to_string", side_effect=RuntimeError("Tesseract process timeout")):
            with self.assertRaises(ocr.OcrProcessingTimeout) as raised:
                ocr._tesseract(page, "--psm 3", ocr.OcrBudget(tesseract_timeout_seconds=2))
        self.assertIn("распознавании страницы", raised.exception.employee_message)

    def test_expired_document_budget_prevents_starting_another_subprocess(self) -> None:
        budget = ocr.OcrBudget()
        budget._deadline = 0
        with patch.object(ocr.pytesseract, "image_to_string") as recognize:
            with self.assertRaises(ocr.OcrProcessingTimeout):
                ocr._tesseract(Image.new("L", (10, 10)), "--psm 3", budget)
        recognize.assert_not_called()

    def test_image_pixel_limit_is_checked_from_headers_before_copy(self) -> None:
        source = unittest.mock.MagicMock()
        source.__enter__.return_value = source
        source.width = 4001
        source.height = 3000
        source.n_frames = 1
        with (
            patch.object(ocr.Image, "open", return_value=source),
            patch.object(ocr.ImageOps, "exif_transpose") as transpose,
        ):
            with self.assertRaises(ocr.ImagePixelLimitExceeded) as raised:
                ocr.load_pages(Path("synthetic-large.png"))
        self.assertEqual((raised.exception.pixels, raised.exception.maximum), (12_003_000, 12_000_000))
        source.copy.assert_not_called()
        transpose.assert_not_called()

    def test_pillow_hard_image_limit_has_an_actionable_failure_state(self) -> None:
        def png_chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        header_only_png = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 20_000, 10_000, 8, 2, 0, 0, 0))
            + png_chunk(b"IEND", b"")
        )
        with tempfile.TemporaryDirectory(prefix="sro-ocr-image-limit-") as directory:
            path = Path(directory) / "synthetic-oversized.png"
            path.write_bytes(header_only_png)
            with self.assertRaises(ocr.ImageDecompressionLimitExceeded) as raised:
                ocr.load_pages(path)
        self.assertIn("предел безопасного чтения", raised.exception.employee_message)
        self.assertNotIn("synthetic-oversized.png", raised.exception.employee_message)

    def test_multiframe_tiff_fails_closed_instead_of_ignoring_later_pages(self) -> None:
        source = unittest.mock.MagicMock()
        source.__enter__.return_value = source
        source.width = 100
        source.height = 100
        source.n_frames = 2
        with patch.object(ocr.Image, "open", return_value=source):
            with self.assertRaises(ocr.UnsupportedMultiFrameImage):
                ocr.load_pages(Path("synthetic-multipage.tiff"))
        source.copy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
