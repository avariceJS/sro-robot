"""Safe access to private browser artifacts referenced by workflow rows."""

from __future__ import annotations

import os
import hashlib
from io import BytesIO
from pathlib import Path
import stat
import warnings

from PIL import Image

from sro_robot.core.ocr import (
    DEFAULT_MAX_IMAGE_PIXELS,
    decode_heic_page,
)


DOCUMENT_SUFFIX_MIME_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}
MAX_INLINE_IMAGE_PIXELS = 12_000_000


def render_heic_preview(
    content: bytes, *, max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
) -> tuple[bytes, tuple[int, int]]:
    """Render a metadata-free, oriented JPEG preview from a private HEIC source."""
    if not isinstance(content, bytes) or not content:
        raise ValueError("HEIC preview input is empty")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(content)) as source:
            image = decode_heic_page(source, max_image_pixels=max_image_pixels)
    try:
        rgb = image.convert("RGB")
        try:
            rgb.info.clear()
            output = BytesIO()
            rgb.save(output, format="JPEG", quality=90, optimize=True, exif=b"")
            return output.getvalue(), rgb.size
        finally:
            rgb.close()
    finally:
        image.close()


def image_within_inline_preview_limit(content: bytes, media_type: str) -> bool:
    """Check raster headers without decoding pixels before browser delivery."""
    expected_format = {"image/jpeg": "JPEG", "image/png": "PNG"}.get(media_type)
    if expected_format is None:
        return False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                frame_count = getattr(image, "n_frames", 1)
                return (
                    image.format == expected_format
                    and width > 0
                    and height > 0
                    and frame_count > 0
                    and width * height * frame_count <= MAX_INLINE_IMAGE_PIXELS
                )
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError, TypeError):
        return False


def read_document(
    path_value: str,
    content_hash: str,
    upload_dir: Path,
    max_bytes: int,
) -> tuple[bytes, str, str]:
    """Read a verified document through one pinned, private-storage file descriptor."""
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not isinstance(content_hash, str)
        or len(content_hash) != 64
        or any(character not in "0123456789abcdef" for character in content_hash)
        or not isinstance(max_bytes, int)
        or max_bytes < 1
    ):
        raise FileNotFoundError

    root = Path(os.path.abspath(upload_dir))
    candidate = Path(os.path.abspath(path_value))
    suffix = candidate.suffix.lower()
    if (
        candidate.parent != root
        or suffix not in DOCUMENT_SUFFIX_MIME_TYPES
        or candidate.name != f"{content_hash}{suffix}"
    ):
        raise FileNotFoundError

    directory_fd = -1
    descriptor = -1
    try:
        root_before = root.lstat()
        if (
            not stat.S_ISDIR(root_before.st_mode)
            or (os.name == "posix" and root_before.st_uid != os.geteuid())
            or (os.name == "posix" and stat.S_IMODE(root_before.st_mode) != 0o700)
        ):
            raise FileNotFoundError
        directory_fd = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW,
        )
        root_opened = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(root_opened.st_mode)
            or (root_opened.st_dev, root_opened.st_ino)
            != (root_before.st_dev, root_before.st_ino)
            or (os.name == "posix" and root_opened.st_uid != os.geteuid())
            or (os.name == "posix" and stat.S_IMODE(root_opened.st_mode) != 0o700)
        ):
            raise FileNotFoundError

        before_open = os.stat(candidate.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before_open.st_mode)
            or before_open.st_size < 0
            or before_open.st_size > max_bytes
            or (os.name == "posix" and before_open.st_uid != os.geteuid())
            or (os.name == "posix" and stat.S_IMODE(before_open.st_mode) != 0o600)
        ):
            raise FileNotFoundError

        descriptor = os.open(candidate.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before_open.st_dev, before_open.st_ino)
            or opened.st_size != before_open.st_size
            or (os.name == "posix" and opened.st_uid != os.geteuid())
            or (os.name == "posix" and stat.S_IMODE(opened.st_mode) != 0o600)
        ):
            raise FileNotFoundError

        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after_read = os.fstat(descriptor)
        if (
            len(content) > max_bytes
            or len(content) != opened.st_size
            or (after_read.st_dev, after_read.st_ino) != (opened.st_dev, opened.st_ino)
            or after_read.st_size != opened.st_size
            or hashlib.sha256(content).hexdigest() != content_hash
        ):
            raise FileNotFoundError
        return content, DOCUMENT_SUFFIX_MIME_TYPES[suffix], suffix
    except (OSError, ValueError, TypeError) as exc:
        raise FileNotFoundError from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd >= 0:
            os.close(directory_fd)


def read_screenshot(path: str, screenshot_dir: Path | None = None) -> bytes:
    """Read a screenshot only when it is a regular direct child of its private dir.

    Database values are not filesystem authorization. Resolve the configured
    directory once, reject paths outside it, and open the final component
    without following symlinks.
    """
    if screenshot_dir is None:
        screenshot_dir = Path(os.environ.get(
            "SRO_OUT_DIR", str(Path(__file__).resolve().parents[2] / "out")
        ))
    root = screenshot_dir.resolve(strict=True)
    candidate = Path(path)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if (
        no_follow is None
        or not candidate.is_absolute()
        or candidate.suffix.lower() != ".png"
    ):
        raise FileNotFoundError
    try:
        if candidate.parent.resolve(strict=True) != root:
            raise FileNotFoundError
        descriptor = os.open(candidate, os.O_RDONLY | no_follow)
    except (OSError, RuntimeError) as exc:
        raise FileNotFoundError from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise FileNotFoundError
        with os.fdopen(descriptor, "rb") as artifact:
            descriptor = -1
            return artifact.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
