"""Page-sized PNG previews using the already installed Poppler renderer."""
from pathlib import Path
import subprocess
import tempfile

from pdf2image import pdfinfo_from_path


def render_pdf_preview(content: bytes, *, page_number: int, max_pages: int) -> tuple[bytes, int]:
    with tempfile.TemporaryDirectory(prefix="sro-pdf-preview-") as folder:
        source = Path(folder) / "original.pdf"
        source.write_bytes(content)
        count = int(pdfinfo_from_path(source, timeout=10)["Pages"])
        if not 1 <= count <= max_pages:
            raise ValueError("PDF page count exceeds preview limit")
        if not 1 <= page_number <= count:
            raise IndexError("PDF page unavailable")
        page = str(page_number)
        try:
            result = subprocess.run(
                ["pdftoppm", "-f", page, "-l", page, "-scale-to", "1800", "-singlefile", "-png", str(source)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=15,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise ValueError("PDF page preview unavailable") from None
        if not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("PDF page preview unavailable")
        return result.stdout, count
