from __future__ import annotations

from io import BytesIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pillow_heif import HeifImagePlugin  # noqa: F401 - register the synthetic HEIF encoder

from app.services.artifacts import render_heic_preview
from sro_robot.core import extract, ocr
from sro_robot.core.source_locations import words_from_tsv


def synthetic_heic(
    *, orientation: int = 1, size: tuple[int, int] = (40, 20),
    header_transform: bool = False,
) -> bytes:
    image = Image.new("RGB", size, "white")
    image.paste((255, 0, 0), (0, 0, max(1, size[0] // 5), max(1, size[1] // 5)))
    exif = Image.Exif()
    exif[274] = orientation
    output = BytesIO()
    if header_transform:
        image.info["exif"] = exif.tobytes()
        image.save(output, format="HEIF", quality=100)
    else:
        image.save(output, format="HEIF", quality=100, exif=exif)
    image.close()
    return output.getvalue()


class HeicIngestionTests(unittest.TestCase):
    def test_heic_exif_orientation_is_applied_before_ocr_and_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-phone-photo.heic"
            source = synthetic_heic(orientation=6)
            path.write_bytes(source)

            pages = ocr.load_pages(path)

        self.assertEqual(pages[0].size, (20, 40))
        red_pixels = [
            (x, y)
            for y in range(pages[0].height)
            for x in range(pages[0].width)
            if (lambda pixel: pixel[0] > 170 and pixel[1] < 130 and pixel[2] < 130)(
                pages[0].getpixel((x, y))
            )
        ]
        self.assertTrue(red_pixels)
        self.assertGreaterEqual(min(x for x, _y in red_pixels), 12)
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t10\t8\t5\t4\t90\tSynthetic\n"
        )
        words = words_from_tsv(tsv, "Synthetic", page_size=pages[0].size)
        self.assertEqual(len(words), 1)
        self.assertAlmostEqual(words[0].x, 0.5)
        self.assertAlmostEqual(words[0].y, 0.2)
        pages[0].close()

    def test_heic_recognition_enables_source_location_capture(self) -> None:
        collector = object()
        page = Image.new("RGB", (40, 20), "white")
        with (
            patch.object(ocr, "load_pages", return_value=[page]),
            patch.object(ocr, "_recognize_pages", return_value=["synthetic text"]) as recognize,
        ):
            self.assertEqual(
                ocr.recognize(Path("synthetic-source-location.heic"), source_location_collector=collector),
                ["synthetic text"],
            )

        self.assertIs(recognize.call_args.kwargs["source_location_collector"], collector)
        page.close()

    def test_parse_file_binds_heic_source_boxes_from_recognition(self) -> None:
        text = "Дело № А99-123/2099"
        tsv = (
            "level\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t20\t10\t50\t10\t96\tА99-123/2099\n"
        )

        def recognize(_path, **options):
            collector = options["source_location_collector"]
            collector.record_variant(0, [(text, tsv, (100, 50))])
            return [text]

        with patch("sro_robot.core.ocr.recognize", side_effect=recognize):
            card = extract.parse_file("synthetic.HEIC")

        self.assertEqual(card.case_number.value, "А99-123/2099")
        self.assertEqual(card.case_number.source_boxes, [{
            "page": 1, "x": 0.2, "y": 0.2, "width": 0.5, "height": 0.2,
        }])

    def test_orientation_mapping_covers_mirrored_and_rotated_exif_values(self) -> None:
        expected_sizes = {
            2: (40, 20), 3: (40, 20), 4: (40, 20), 5: (20, 40),
            6: (20, 40), 7: (20, 40), 8: (20, 40),
        }
        for orientation, expected_size in expected_sizes.items():
            with self.subTest(orientation=orientation), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "synthetic.heic"
                path.write_bytes(synthetic_heic(orientation=orientation))
                page = ocr.load_pages(path)[0]
                self.assertEqual(page.size, expected_size)
                page.close()

    def test_iso_bmff_rotation_is_not_applied_a_second_time(self) -> None:
        expected_marker_quadrant = {
            3: ("right", "bottom"),
            6: ("right", "top"),
            8: ("left", "bottom"),
        }
        for orientation, expected_size in ((3, (40, 20)), (6, (20, 40)), (8, (20, 40))):
            with self.subTest(orientation=orientation), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "synthetic-header-rotation.heic"
                path.write_bytes(synthetic_heic(orientation=orientation, header_transform=True))
                with Image.open(path) as source:
                    transforms = source._heif_file[source.tell()]._c_image.transformations
                    self.assertTrue(any(item[0] == "irot" and item[1] != 0 for item in transforms))
                page = ocr.load_pages(path)[0]
                self.assertEqual(page.size, expected_size)
                red_pixels = [
                    (x, y)
                    for y in range(page.height)
                    for x in range(page.width)
                    if (lambda pixel: pixel[0] > 170 and pixel[1] < 130 and pixel[2] < 130)(
                        page.getpixel((x, y))
                    )
                ]
                self.assertTrue(red_pixels)
                marker_x = sum(x for x, _y in red_pixels) / len(red_pixels)
                marker_y = sum(y for _x, y in red_pixels) / len(red_pixels)
                horizontal, vertical = expected_marker_quadrant[orientation]
                self.assertGreater(marker_x, page.width / 2 if horizontal == "right" else -1)
                self.assertLess(marker_x, page.width / 2 if horizontal == "left" else page.width)
                self.assertGreater(marker_y, page.height / 2 if vertical == "bottom" else -1)
                self.assertLess(marker_y, page.height / 2 if vertical == "top" else page.height)
                page.close()

    def test_4032_by_3024_heic_is_proportionally_resized_under_ocr_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-12mp.heic"
            original = synthetic_heic(size=(4032, 3024))
            path.write_bytes(original)

            page = ocr.load_pages(path)[0]

            self.assertLessEqual(page.width * page.height, ocr.DEFAULT_MAX_IMAGE_PIXELS)
            self.assertAlmostEqual(page.width / page.height, 4 / 3, places=3)
            self.assertEqual(path.read_bytes(), original)
            page.close()

    def test_inline_preview_is_oriented_bounded_jpeg_without_source_metadata(self) -> None:
        for header_transform in (False, True):
            with self.subTest(header_transform=header_transform):
                preview, dimensions = render_heic_preview(
                    synthetic_heic(orientation=6, header_transform=header_transform),
                )
                self.assertEqual(dimensions, (20, 40))
                with Image.open(BytesIO(preview)) as decoded:
                    self.assertEqual(decoded.format, "JPEG")
                    self.assertEqual(decoded.size, dimensions)
                    self.assertIsNone(decoded.getexif().get(274))

    def test_heic_above_resize_allowance_and_corrupt_input_fail_closed(self) -> None:
        image = Image.new("RGB", (4032, 3024), "white")
        output = BytesIO()
        image.save(output, format="HEIF")
        image.close()
        with Image.open(BytesIO(output.getvalue())) as source:
            with self.assertRaises(ocr.ImagePixelLimitExceeded):
                ocr.decode_heic_page(source, max_image_pixels=1_000_000)

        with self.assertRaises((OSError, SyntaxError, ValueError)):
            render_heic_preview(b"synthetic corrupt HEIC")


if __name__ == "__main__":
    unittest.main()
