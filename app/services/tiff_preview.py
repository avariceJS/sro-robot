"""Bounded, isolated rendering of one verified TIFF page for staff review."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

from sro_robot.config import ROOT
from sro_robot.core.ocr import DEFAULT_MAX_TIFF_FRAMES, MAX_CONFIGURED_TIFF_FRAMES


TIFF_PREVIEW_TIMEOUT_SECONDS = 8
TIFF_PREVIEW_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
TIFF_PREVIEW_MAX_INPUT_BYTES = 25 * 1024 * 1024
TIFF_PREVIEW_SLOTS = threading.BoundedSemaphore(1)


class TiffPreviewError(RuntimeError):
    """The requested TIFF page could not be rendered safely."""


class TiffPreviewLimitError(TiffPreviewError):
    """The source exceeds TIFF preview bounds."""


class TiffPreviewPageError(TiffPreviewError):
    """The requested one-based page does not exist."""


class TiffPreviewTimeout(TiffPreviewError):
    """The isolated TIFF decoder exceeded its hard wall-clock timeout."""


def render_tiff_preview(
    content: bytes,
    *,
    page_number: int,
    max_frames: int = DEFAULT_MAX_TIFF_FRAMES,
    max_image_pixels: int = 12_000_000,
) -> tuple[bytes, int]:
    """Render a one-based TIFF page in a bounded subprocess.

    Callers should acquire ``TIFF_PREVIEW_SLOTS`` non-blockingly before
    invoking this function. The child receives only document bytes and fixed
    limits, never application secrets or a filesystem path.
    """
    if not isinstance(content, bytes) or len(content) > TIFF_PREVIEW_MAX_INPUT_BYTES:
        raise TiffPreviewLimitError("TIFF preview input exceeds its byte bound")
    if (
        not isinstance(page_number, int)
        or isinstance(page_number, bool)
        or page_number < 1
        or not 1 <= max_frames <= MAX_CONFIGURED_TIFF_FRAMES
        or not 1 <= max_image_pixels <= 25_000_000
    ):
        raise TiffPreviewLimitError("TIFF preview limits or page are invalid")

    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.services.tiff_preview_worker",
                str(page_number),
                str(max_frames),
                str(max_image_pixels),
            ],
            input=content,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=TIFF_PREVIEW_TIMEOUT_SECONDS,
            check=False,
            cwd=ROOT,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise TiffPreviewTimeout("TIFF preview renderer timed out") from exc
    except OSError as exc:
        raise TiffPreviewError("TIFF preview renderer is unavailable") from exc

    if result.returncode == 4:
        raise TiffPreviewPageError("TIFF preview page is outside the document")
    if result.returncode == 2:
        raise TiffPreviewLimitError("TIFF preview source exceeds safe rendering bounds")
    if result.returncode != 0 or len(result.stdout) > TIFF_PREVIEW_MAX_OUTPUT_BYTES:
        raise TiffPreviewError("TIFF preview page could not be rendered")

    header, separator, image_bytes = result.stdout.partition(b"\n")
    if not separator or len(header) > 128 or not image_bytes:
        raise TiffPreviewError("TIFF preview renderer returned an invalid result")
    try:
        metadata = json.loads(header)
        page_count = metadata["page_count"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise TiffPreviewError("TIFF preview renderer returned invalid metadata") from exc
    if (
        not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or not 1 <= page_count <= max_frames
        or page_number > page_count
        or not image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        raise TiffPreviewError("TIFF preview renderer returned an invalid page")
    return image_bytes, page_count
