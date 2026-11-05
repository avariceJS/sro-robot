"""Общий экземпляр Jinja2 для HTML-страниц."""

from __future__ import annotations

from pathlib import Path
from hashlib import sha256

from starlette.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# Immutable release contents give each changed asset a new browser cache key.
templates.env.globals["review_asset_versions"] = {
    name: sha256((_TEMPLATES_DIR.parent / "static" / name).read_bytes()).hexdigest()[:16]
    for name in ("staff-controls.js", "field-source.js", "kad-helper-status.js", "app.css")
}
