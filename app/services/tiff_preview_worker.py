"""Private subprocess entry point for decoding one bounded TIFF page."""

from __future__ import annotations

from io import BytesIO
import json
import resource
import sys
import warnings


MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024
MAX_CPU_SECONDS = 5


def _set_process_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))
    # Darwin rejects lowering its data/virtual-memory limits for the system
    # Python runtime. Linux workers can enforce a hard address-space ceiling.
    memory_limit = getattr(resource, "RLIMIT_AS", None) if sys.platform.startswith("linux") else None
    if memory_limit is not None:
        current_soft, current_hard = resource.getrlimit(memory_limit)
        address_space_hard_limit = (
            MAX_ADDRESS_SPACE_BYTES
            if current_hard == resource.RLIM_INFINITY
            else min(current_hard, MAX_ADDRESS_SPACE_BYTES)
        )
        address_space_soft_limit = (
            address_space_hard_limit
            if current_soft == resource.RLIM_INFINITY
            else min(current_soft, address_space_hard_limit)
        )
        resource.setrlimit(
            memory_limit,
            (address_space_soft_limit, address_space_hard_limit),
        )


def _render(content: bytes, page_number: int, max_frames: int, max_pixels: int) -> tuple[bytes, int]:
    from PIL import Image, ImageOps

    if not content or len(content) > MAX_INPUT_BYTES:
        raise ValueError("input bound")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(content)) as source:
            if source.format != "TIFF":
                raise ValueError("format")
            page_count = 0
            for index in range(max_frames + 1):
                try:
                    source.seek(index)
                except EOFError:
                    break
                if index >= max_frames:
                    raise OverflowError("frame count")
                page_count += 1
            if page_count == 0 or page_number > page_count:
                raise IndexError("page")
            source.seek(page_number - 1)
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise OverflowError("pixel count")
            oriented = ImageOps.exif_transpose(source)
            try:
                if oriented.width * oriented.height > max_pixels:
                    raise OverflowError("oriented pixel count")
                preview = oriented.convert("L")
                try:
                    # Pillow carries source image metadata across convert() and
                    # may write ICC data into the PNG unless it is removed.
                    preview.info.clear()
                    output = BytesIO()
                    preview.save(output, format="PNG", optimize=False)
                    image_bytes = output.getvalue()
                finally:
                    preview.close()
            finally:
                oriented.close()

    if len(image_bytes) > MAX_OUTPUT_BYTES:
        raise OverflowError("output size")
    return image_bytes, page_count


def main() -> int:
    _set_process_limits()
    try:
        page_number, max_frames, max_pixels = map(int, sys.argv[1:4])
        content = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        image_bytes, page_count = _render(content, page_number, max_frames, max_pixels)
    except IndexError:
        return 4
    except OverflowError:
        return 2
    except Exception:
        return 3
    sys.stdout.buffer.write(json.dumps({"page_count": page_count}, separators=(",", ":")).encode("ascii"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.write(image_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
