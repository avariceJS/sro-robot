"""Read-only distribution of the employee's ordinary-browser KAD helper."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from .deps import current_user
from ..db.models import User
from ..public_origin import public_app_origin
from ..kad_helper_package import development_archive, store_archive
from ..settings import settings
from ..web.templates import templates

router = APIRouter(tags=["kad-helper"])


def helper_archive() -> bytes:
    """Package an explicit source allowlist; never include workspace data."""
    return development_archive(public_app_origin(settings.public_app_origin))


@router.get("/kad-helper", name="kad_helper_page")
def kad_helper_page(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(
        request, "kad_helper.html", {
            "user": user,
            "public_app_origin": settings.public_app_origin,
            "chrome_store_url": settings.kad_chrome_store_url,
            "edge_store_url": settings.kad_edge_store_url,
            "firefox_store_url": settings.kad_firefox_store_url,
            "store_distribution_ready": bool(settings.kad_chrome_store_url or settings.kad_edge_store_url or settings.kad_firefox_store_url),
        },
    )


@router.get("/kad-helper/privacy", name="kad_helper_privacy")
def kad_helper_privacy(request: Request):
    return templates.TemplateResponse(request, "kad_helper_privacy.html", {})


@router.get("/kad-helper/download", name="kad_helper_download")
def kad_helper_download(user: User = Depends(current_user)):
    # Unpacked archives are a controlled development path, never the staff install path.
    if not settings.debug:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="KAD helper is distributed through its official browser store")
    return Response(
        development_archive(public_app_origin(settings.public_app_origin)), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="sro-kad-helper.zip"'},
    )
