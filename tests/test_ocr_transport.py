"""Real, small synthetic OCR equivalence on the installed system libraries.

CI installs Tesseract/rus and DejaVu. No original samples or application services
are used; unsupported local tool/font installations skip this integration check.
"""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont, ImageCms
import pytesseract

from sro_robot.core import ocr


class ScratchTransportIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which(pytesseract.pytesseract.tesseract_cmd):
            raise unittest.SkipTest("Tesseract is not installed")
        if "rus" not in pytesseract.get_languages():
            raise unittest.SkipTest("Russian Tesseract model is not installed")
        fonts = (
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        )
        font = next((path for path in fonts if path.is_file()), None)
        if font is None:
            raise unittest.SkipTest("A local Cyrillic test font is unavailable")
        cls.font = ImageFont.truetype(str(font), 40)

    def test_text_tsv_and_coordinates_match_png_for_gray_color_and_transparency(self):
        for mode, transparency in (("L", None), ("RGB", None), ("L", 0), ("RGB", (255, 0, 0))):
            with self.subTest(mode=mode, transparency=transparency):
                image = Image.new(mode, (1100, 280), "white")
                ImageDraw.Draw(image).multiline_text(
                    (30, 20), "СИНТЕТИЧЕСКИЙ АКТ\nДело А99-12345/2099\nИНН 772881234508",
                    font=self.font, fill="black", spacing=12,
                )
                if mode == "RGB":
                    image.info["icc_profile"] = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
                if transparency is not None:
                    image.info["transparency"] = transparency
                try:
                    with tempfile.TemporaryDirectory(prefix="sro-synthetic-ocr-") as directory:
                        root = Path(directory)
                        reference = root / "reference.png"
                        image.save(reference, format="PNG", compress_level=0)
                        output = str(root / "expected")
                        invoke = pytesseract.pytesseract.run_tesseract
                        invoke(str(reference), output, "txt tsv", "rus",
                               config="--psm 6 -c tessedit_create_tsv=1", timeout=10)
                        expected = tuple(pytesseract.pytesseract._read_output(output + suffix, False)
                                         for suffix in (".txt", ".tsv"))
                        formats = []
                        def checked_input(input_filename, *args, **kwargs):
                            with Image.open(input_filename) as encoded:
                                formats.append(encoded.format)
                            return invoke(input_filename, *args, **kwargs)
                        with patch.object(pytesseract.pytesseract, "run_tesseract", side_effect=checked_input):
                            actual = ocr._tesseract_with_source(image, "--psm 6", None)
                        self.assertEqual(actual, expected)
                        self.assertEqual(formats, ["BMP" if transparency is None else "PNG"])
                        if transparency is None:
                            self.assertIn("772881234508", actual[0])
                finally:
                    image.close()


if __name__ == "__main__":
    unittest.main()
