"""Page-sized PNG previews using the already installed Poppler renderer."""
from io import BytesIO

from pdf2image import convert_from_bytes, pdfinfo_from_bytes


def render_pdf_preview(content: bytes, *, page_number: int, max_pages: int) -> tuple[bytes, int]:
    count = int(pdfinfo_from_bytes(content, timeout=10)["Pages"])
    if not 1 <= count <= max_pages:
        raise ValueError("PDF page count exceeds preview limit")
    if not 1 <= page_number <= count:
        raise IndexError("PDF page unavailable")
    images = convert_from_bytes(content, first_page=page_number, last_page=page_number,
                                size=1800, fmt="png", thread_count=1, timeout=15)
    try:
        if len(images) != 1:
            raise ValueError("PDF page unavailable")
        output = BytesIO()
        images[0].save(output, format="PNG")
        return output.getvalue(), count
    finally:
        for image in images:
            image.close()
