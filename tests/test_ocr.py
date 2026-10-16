from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from sro_robot.core import ocr


class OCRPathTests(unittest.TestCase):
    def test_text_layer_pdf_skips_rasterization_and_ocr(self) -> None:
        path = Path("synthetic-text.pdf")
        embedded = "synthetic text layer " * 30
        with (
            patch.object(ocr, "pdf_text_layer", return_value=embedded),
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
            patch.object(ocr, "_load_pdf_pages", return_value=[Image.new("L", (100, 100))]) as raster,
            patch.object(ocr.pytesseract, "image_to_string", return_value="Должник: Иванова Анна Сергеевна") as recognize,
        ):
            variants = ocr.recognize(Path("synthetic-mixed.pdf"))

        self.assertEqual(len(variants), len(ocr.PSM_MODES))
        self.assertTrue(all(cover.strip() in variant for variant in variants))
        self.assertTrue(all("Должник: Иванова Анна Сергеевна" in variant for variant in variants))
        raster.assert_called_once_with(Path("synthetic-mixed.pdf"), 2, 2, 300)
        self.assertEqual(recognize.call_count, len(ocr.PSM_MODES))

    def test_searchable_multipage_pdf_keeps_fast_path(self) -> None:
        embedded = ("synthetic searchable page " * 12) + "\f" + ("second searchable page " * 12)
        with (
            patch.object(ocr, "pdf_text_layer", return_value=embedded),
            patch.object(ocr, "_load_pdf_pages") as raster,
            patch.object(ocr.pytesseract, "image_to_string") as image_to_string,
        ):
            self.assertEqual(ocr.recognize(Path("synthetic-text.pdf")), [embedded])
        raster.assert_not_called()
        image_to_string.assert_not_called()

    def test_blank_page_in_mixed_pdf_does_not_run_tesseract(self) -> None:
        cover = "synthetic searchable cover " * 28
        with (
            patch.object(ocr, "pdf_text_layer", return_value=cover + "\f\f"),
            patch.object(ocr, "_load_pdf_pages", return_value=[Image.new("L", (20, 20), 255)]),
            patch.object(ocr.pytesseract, "image_to_string") as image_to_string,
        ):
            variants = ocr.recognize(Path("synthetic-mixed.pdf"))

        self.assertTrue(variants)
        image_to_string.assert_not_called()

    def test_multipage_pdf_rasterization_is_bounded_to_four_page_batches(self) -> None:
        text_pages = ["searchable text " * 10] + [""] * 9
        calls = []

        def raster(_path, first_page, last_page, _dpi):
            calls.append((first_page, last_page))
            return [Image.new("L", (20, 20), 255) for _ in range(last_page - first_page + 1)]

        with patch.object(ocr, "_load_pdf_pages", side_effect=raster):
            variants = ocr._recognize_pdf_pages(Path("synthetic-long.pdf"), text_pages, 300)

        self.assertEqual(calls, [(2, 5), (6, 9), (10, 10)])
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

        def recognize_image(image, *, lang: str, config: str) -> str:
            seen_images.append(([image.getpixel((x, 0)) for x in range(image.width)], lang, config))
            return "synthetic OCR"

        with (
            patch.object(ocr, "pdf_text_layer", return_value=""),
            patch.object(ocr, "pdfinfo_from_path", return_value={"Pages": "1"}),
            patch.object(ocr, "_load_pdf_pages", return_value=[page]),
            patch.object(ocr.pytesseract, "image_to_string", side_effect=recognize_image),
            patch.object(ocr, "preprocess") as preprocess,
        ):
            variants = ocr.recognize(path)

        self.assertEqual(variants, ["synthetic OCR"] * 4)
        self.assertEqual([row[2] for row in seen_images], [f"--psm {psm}" for psm in ocr.PSM_MODES])
        self.assertTrue(all(row[1] == "rus" for row in seen_images))
        self.assertEqual([row[0][:4] for row in seen_images], [[0, 0, 255, 255]] * 4)
        preprocess.assert_not_called()

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

    def test_failed_binary_pass_falls_back_to_legacy_scan_pipeline(self) -> None:
        path = Path("synthetic-scan.pdf")
        page = Image.new("L", (100, 100), 255)
        for x in range(30):
            page.putpixel((x, 0), 0)
        responses = ["", "", "", "", "legacy text", "", "", "", ""]
        with (
            patch.object(ocr, "pdf_text_layer", return_value=""),
            patch.object(ocr, "pdfinfo_from_path", return_value={"Pages": "1"}),
            patch.object(ocr, "_load_pdf_pages", return_value=[page]),
            patch.object(ocr, "preprocess", return_value=page),
            patch.object(ocr.pytesseract, "image_to_string", side_effect=responses) as ocr_call,
        ):
            variants = ocr.recognize(path)

        self.assertEqual(variants, ["legacy text"] * len(ocr.PSM_MODES))
        self.assertEqual(ocr_call.call_count, 9)

    def test_empty_text_layer_uses_pdf_page_count_for_all_scan_pages(self) -> None:
        calls = []

        def raster(_path, first_page, last_page, _dpi):
            calls.append((first_page, last_page))
            return [Image.new("L", (100, 100), 0) for _ in range(last_page - first_page + 1)]

        with (
            patch.object(ocr, "pdf_text_layer", return_value=""),
            patch.object(ocr, "pdfinfo_from_path", return_value={"Pages": "2"}),
            patch.object(ocr, "_load_pdf_pages", side_effect=raster),
            patch.object(ocr.pytesseract, "image_to_string", return_value="synthetic OCR"),
        ):
            variants = ocr.recognize(Path("synthetic-empty-layer-multipage.pdf"))

        self.assertEqual(calls, [(1, 2)])
        self.assertEqual(len(variants), len(ocr.PSM_MODES))
        self.assertTrue(all("synthetic OCR" in variant for variant in variants))


if __name__ == "__main__":
    unittest.main()
