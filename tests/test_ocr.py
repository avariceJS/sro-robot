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

    def test_scanned_pdf_uses_four_binary_psm_passes(self) -> None:
        path = Path("synthetic-scan.pdf")
        page = Image.new("L", (4, 1))
        page.putdata([0, 210, 220, 255])
        seen_images = []

        def recognize_image(image, *, lang: str, config: str) -> str:
            seen_images.append(([image.getpixel((x, 0)) for x in range(image.width)], lang, config))
            return "synthetic OCR"

        with (
            patch.object(ocr, "pdf_text_layer", return_value=""),
            patch.object(ocr, "load_pages", return_value=[page]),
            patch.object(ocr.pytesseract, "image_to_string", side_effect=recognize_image),
            patch.object(ocr, "preprocess") as preprocess,
        ):
            variants = ocr.recognize(path)

        self.assertEqual(variants, ["synthetic OCR"] * 4)
        self.assertEqual([row[2] for row in seen_images], [f"--psm {psm}" for psm in ocr.PSM_MODES])
        self.assertTrue(all(row[1] == "rus" for row in seen_images))
        self.assertTrue(all(row[0] == [0, 0, 255, 255] for row in seen_images))
        preprocess.assert_not_called()

    def test_image_upload_keeps_legacy_preprocessed_and_original_passes(self) -> None:
        path = Path("synthetic-photo.png")
        page = Image.new("L", (2, 2))
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
        page = Image.new("L", (2, 2))
        responses = ["", "", "", "", "legacy text", "", "", "", ""]
        with (
            patch.object(ocr, "pdf_text_layer", return_value=""),
            patch.object(ocr, "load_pages", return_value=[page]),
            patch.object(ocr, "preprocess", return_value=page),
            patch.object(ocr.pytesseract, "image_to_string", side_effect=responses) as ocr_call,
        ):
            variants = ocr.recognize(path)

        self.assertEqual(variants, ["legacy text"])
        self.assertEqual(ocr_call.call_count, 9)


if __name__ == "__main__":
    unittest.main()
