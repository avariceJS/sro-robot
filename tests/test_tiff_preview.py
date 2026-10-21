from __future__ import annotations

from io import BytesIO
import subprocess
import tempfile
import unittest
import resource
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.services import tiff_preview, tiff_preview_worker


class TiffPreviewTests(unittest.TestCase):
    @staticmethod
    def write_tiff(path: Path, pages: list[tuple[int, int, int]]) -> bytes:
        images = [Image.new("L", (width, height), shade) for width, height, shade in pages]
        try:
            images[0].save(path, format="TIFF", save_all=True, append_images=images[1:])
        finally:
            for image in images:
                image.close()
        return path.read_bytes()

    def test_isolated_renderer_returns_only_requested_page_and_count(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sro-tiff-preview-") as directory:
            path = Path(directory) / "synthetic.tiff"
            content = self.write_tiff(path, [(32, 24, 10), (18, 28, 80), (40, 30, 180)])

            png, page_count = tiff_preview.render_tiff_preview(content, page_number=2)

        self.assertEqual(page_count, 3)
        with Image.open(BytesIO(png)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (18, 28))
            self.assertEqual(image.getpixel((0, 0)), 80)
            self.assertEqual(image.info, {})

    def test_renderer_strips_embedded_tiff_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sro-tiff-preview-metadata-") as directory:
            path = Path(directory) / "metadata.tiff"
            image = Image.new("RGB", (16, 12), (40, 80, 120))
            try:
                image.save(
                    path,
                    format="TIFF",
                    icc_profile=b"synthetic-icc-profile",
                    xmp=b"<x:xmpmeta>synthetic</x:xmpmeta>",
                )
            finally:
                image.close()

            png, page_count = tiff_preview.render_tiff_preview(path.read_bytes(), page_number=1)

        self.assertEqual(page_count, 1)
        with Image.open(BytesIO(png)) as preview:
            self.assertEqual(preview.info, {})

    def test_linux_infinite_address_space_limit_is_replaced_with_finite_cap(self) -> None:
        address_space = resource.RLIMIT_AS
        with (
            patch.object(tiff_preview_worker.sys, "platform", "linux"),
            patch.object(tiff_preview_worker.resource, "getrlimit", return_value=(
                resource.RLIM_INFINITY, resource.RLIM_INFINITY
            )),
            patch.object(tiff_preview_worker.resource, "setrlimit") as setrlimit,
        ):
            tiff_preview_worker._set_process_limits()

        self.assertIn(
            unittest.mock.call(
                address_space,
                (tiff_preview_worker.MAX_ADDRESS_SPACE_BYTES, tiff_preview_worker.MAX_ADDRESS_SPACE_BYTES),
            ),
            setrlimit.call_args_list,
        )

    def test_isolated_renderer_enforces_page_frame_and_pixel_bounds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sro-tiff-preview-limits-") as directory:
            path = Path(directory) / "synthetic.tiff"
            content = self.write_tiff(path, [(12, 12, 10), (14, 10, 80)])

            with self.assertRaises(tiff_preview.TiffPreviewPageError):
                tiff_preview.render_tiff_preview(content, page_number=3)
            with self.assertRaises(tiff_preview.TiffPreviewLimitError):
                tiff_preview.render_tiff_preview(content, page_number=1, max_frames=1)
            with self.assertRaises(tiff_preview.TiffPreviewLimitError):
                tiff_preview.render_tiff_preview(content, page_number=2, max_image_pixels=120)

    def test_renderer_timeout_and_unavailable_process_fail_closed(self) -> None:
        with (
            patch.object(
                tiff_preview.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("renderer", 8),
            ),
            self.assertRaises(tiff_preview.TiffPreviewTimeout),
        ):
            tiff_preview.render_tiff_preview(b"synthetic", page_number=1)

        with (
            patch.object(tiff_preview.subprocess, "run", side_effect=OSError("unavailable")),
            self.assertRaises(tiff_preview.TiffPreviewError),
        ):
            tiff_preview.render_tiff_preview(b"synthetic", page_number=1)

    def test_corrupt_tiff_fails_closed_without_returning_an_image(self) -> None:
        with self.assertRaises(tiff_preview.TiffPreviewError):
            tiff_preview.render_tiff_preview(b"synthetic corrupt TIFF", page_number=1)

    def test_invalid_renderer_response_is_rejected(self) -> None:
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b'{"page_count":0}\n\x89PNG\r\n\x1a\n'
        )
        with (
            patch.object(tiff_preview.subprocess, "run", return_value=result),
            self.assertRaises(tiff_preview.TiffPreviewError),
        ):
            tiff_preview.render_tiff_preview(b"synthetic", page_number=1)


if __name__ == "__main__":
    unittest.main()
