"""Read-only distribution of the employee's ordinary-browser KAD helper."""

from io import BytesIO
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from .deps import current_user
from ..db.models import User
from ..public_origin import public_app_origin
from ..settings import settings
from ..web.templates import templates

router = APIRouter(tags=["kad-helper"])
HELPER_ROOT = Path(__file__).resolve().parents[2] / "browser-extension" / "kad-helper"
HELPER_FILES = (
    "manifest.json", "protocol.js", "native-pdf.js", "automation.js", "background.js",
    "content.js", "bridge.js", "service.js", "popup.html", "popup.js", "README.md",
)
SERVICE_ORIGINS_MARKER = "/* SRO_SERVICE_ORIGINS */['http://localhost:8000', 'http://127.0.0.1:8000']"


def _archive_source(filename: str, office_origin: str | None) -> bytes:
    raw = (HELPER_ROOT / filename).read_bytes()
    if not office_origin:
        return raw
    if filename == "manifest.json":
        manifest = json.loads(raw)
        pattern = office_origin + "/*"
        manifest["host_permissions"].append(pattern)
        service_scripts = [entry for entry in manifest["content_scripts"] if "service.js" in entry["js"]]
        if len(service_scripts) != 1:
            raise ValueError("KAD helper service script layout changed; packaging refused")
        service_scripts[0]["matches"].append(pattern)
        return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if filename in {"bridge.js", "service.js"}:
        source = raw.decode("utf-8")
        if source.count(SERVICE_ORIGINS_MARKER) != 1:
            raise ValueError("KAD helper origin marker changed; packaging refused")
        origins = json.dumps(["http://localhost:8000", "http://127.0.0.1:8000", office_origin])
        return source.replace(SERVICE_ORIGINS_MARKER, origins).encode("utf-8")
    return raw


def helper_archive() -> bytes:
    """Package an explicit source allowlist; never include workspace data."""
    office_origin = public_app_origin(settings.public_app_origin)
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for filename in HELPER_FILES:
            info = ZipInfo(filename, (2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, _archive_source(filename, office_origin))
    return output.getvalue()


@router.get("/kad-helper", name="kad_helper_page")
def kad_helper_page(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(
        request, "kad_helper.html", {"user": user, "public_app_origin": settings.public_app_origin},
    )


@router.get("/kad-helper/download", name="kad_helper_download")
def kad_helper_download(user: User = Depends(current_user)):
    return Response(
        helper_archive(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="sro-kad-helper.zip"'},
    )
