"""Read-only distribution of the employee's ordinary-browser KAD helper."""

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from .deps import current_user
from ..db.models import User
from ..web.templates import templates

router = APIRouter(tags=["kad-helper"])
HELPER_ROOT = Path(__file__).resolve().parents[2] / "browser-extension" / "kad-helper"
HELPER_FILES = (
    "manifest.json", "protocol.js", "automation.js", "background.js",
    "content.js", "popup.html", "popup.js", "README.md",
)


def helper_archive() -> bytes:
    """Package an explicit source allowlist; never include workspace data."""
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for filename in HELPER_FILES:
            info = ZipInfo(filename, (2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (HELPER_ROOT / filename).read_bytes())
    return output.getvalue()


@router.get("/kad-helper", name="kad_helper_page")
def kad_helper_page(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(request, "kad_helper.html", {"user": user})


@router.get("/kad-helper/download", name="kad_helper_download")
def kad_helper_download(user: User = Depends(current_user)):
    return Response(
        helper_archive(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="sro-kad-helper.zip"'},
    )
