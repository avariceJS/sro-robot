"""Synthetic pixel-contract checks against Pillow's default autocontrast."""
from contextlib import closing
import random
import unittest
from unittest.mock import patch

from PIL import Image, ImageFilter, ImageOps

from sro_robot.core.ocr import preprocess


def pillow_reference(image, scale):
    with closing(ImageOps.grayscale(image)) as gray:
        with closing(gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.LANCZOS)
                     if scale != 1 else gray.copy()) as resized:
            with closing(ImageOps.autocontrast(resized)) as contrasted:
                return contrasted.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))


class OCRPreprocessTests(unittest.TestCase):
    def assert_pillow_equivalent(self, image, scale):
        before = image.tobytes()
        with closing(pillow_reference(image, scale)) as expected, closing(preprocess(image, scale)) as actual:
            self.assertEqual((actual.mode, actual.size, actual.info), (expected.mode, expected.size, expected.info))
            self.assertEqual(actual.tobytes(), expected.tobytes())
            self.assertIsNot(actual, image)
        self.assertEqual(image.tobytes(), before)

    def test_every_gray_level_and_low_contrast_bands_match_pillow(self):
        for low, high in ((0, 255), (0, 254), (1, 255), (3, 107), (37, 201),
                          (0, 1), (127, 128), (254, 255)):
            with self.subTest(low=low, high=high):
                with closing(Image.frombytes('L', (high - low + 1, 1), bytes(range(low, high + 1)))) as image:
                    for scale in (1, 2):
                        self.assert_pillow_equivalent(image, scale)

    def test_uniform_endpoints_and_intermediate_gray_match_pillow(self):
        for value in (0, 1, 90, 127, 180, 254, 255):
            with self.subTest(value=value), closing(Image.new('L', (19, 13), value)) as image:
                for scale in (1, 2):
                    self.assert_pillow_equivalent(image, scale)

    def test_seeded_color_alpha_and_edge_shapes_match_pillow(self):
        random_source = random.Random(7351)
        for mode in ('L', 'RGB', 'RGBA'):
            for size in ((1, 1), (1, 31), (31, 1), (23, 47)):
                with self.subTest(mode=mode, size=size):
                    data = random_source.randbytes(size[0] * size[1] * len(mode))
                    with closing(Image.frombytes(mode, size, data)) as image:
                        for scale in (1, 2):
                            self.assert_pillow_equivalent(image, scale)

    def test_low_contrast_rounding_stays_truncating_before_sharpening(self):
        source = bytes((37, 38, 100, 180, 201))
        with closing(Image.frombytes('L', (len(source), 1), source)) as image:
            with patch.object(Image.Image, 'filter', autospec=True,
                              side_effect=lambda item, _: item.copy()):
                with closing(preprocess(image, 1)) as actual:
                    self.assertEqual(actual.tobytes(), bytes((0, 1, 97, 222, 255)))

    def test_input_metadata_encoder_options_and_lifetime_stay_owned_by_caller(self):
        with closing(Image.new('L', (9, 7), 127)) as image:
            image.info = {'dpi': (72, 72), 'synthetic': 'retained'}
            image.format = 'PNG'
            image.encoderinfo = {'compress_level': 6}
            original_info = image.info.copy()
            with closing(preprocess(image, 1)) as actual:
                self.assertEqual(actual.info, original_info)
                self.assertEqual(image.getpixel((0, 0)), 127)
            self.assertEqual(image.info, original_info)
            self.assertEqual(image.encoderinfo, {'compress_level': 6})
            self.assertEqual(image.format, 'PNG')
            self.assertEqual(image.getpixel((0, 0)), 127)
