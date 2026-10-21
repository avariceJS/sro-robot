"""Pure, deterministic KAD extension package builders; no app settings or secrets."""

from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


HELPER_ROOT = Path(__file__).resolve().parents[1] / "browser-extension" / "kad-helper"
FILES = (
    "manifest.json", "protocol.js", "native-pdf.js", "automation.js", "background.js",
    "content.js", "bridge.js", "service.js", "popup.html", "popup.js", "README.md",
    "icons/icon-16.png", "icons/icon-32.png", "icons/icon-48.png", "icons/icon-128.png",
)
STORE_FILES = tuple(filename for filename in FILES if filename != "README.md")
STORE_ORIGIN = "https://sro-office.ru"
SERVICE_ORIGINS_MARKER = "/* SRO_SERVICE_ORIGINS */['http://localhost:8000', 'http://127.0.0.1:8000']"
SERVICE_ORIGIN_MARKER = "/* SRO_SERVICE_ORIGIN */'http://localhost:8000'"


def _write_archive(files: tuple[str, ...], read_source) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for filename in files:
            info = ZipInfo(filename, (2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, read_source(filename))
    return output.getvalue()


def development_archive(office_origin: str | None = None) -> bytes:
    """Build the controlled source/debug archive; loopback stays developer-only."""
    def source(filename: str) -> bytes:
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
            text = raw.decode("utf-8")
            if text.count(SERVICE_ORIGINS_MARKER) != 1:
                raise ValueError("KAD helper origin marker changed; packaging refused")
            return text.replace(
                SERVICE_ORIGINS_MARKER,
                json.dumps(["http://localhost:8000", "http://127.0.0.1:8000", office_origin]),
            ).encode("utf-8")
        if filename == "popup.js":
            text = raw.decode("utf-8")
            if text.count(SERVICE_ORIGIN_MARKER) != 1:
                raise ValueError("KAD helper popup origin marker changed; packaging refused")
            return text.replace(SERVICE_ORIGIN_MARKER, json.dumps(office_origin)).encode("utf-8")
        return raw

    return _write_archive(FILES, source)


def store_archive(browser: str) -> bytes:
    """Build a static browser-store artifact pinned to the office HTTPS origin."""
    if browser not in {"chrome", "edge", "firefox"}:
        raise ValueError("browser must be chrome, edge, or firefox")

    def source(filename: str) -> bytes:
        raw = (HELPER_ROOT / filename).read_bytes()
        if filename == "manifest.json":
            manifest = json.loads(raw)
            manifest["host_permissions"] = ["https://kad.arbitr.ru/*", STORE_ORIGIN + "/*"]
            service_scripts = [entry for entry in manifest["content_scripts"] if "service.js" in entry["js"]]
            if len(service_scripts) != 1:
                raise ValueError("KAD helper service script layout changed; store packaging refused")
            service_scripts[0]["matches"] = [STORE_ORIGIN + "/*"]
            if browser in {"chrome", "edge"}:
                manifest.pop("browser_specific_settings", None)
                manifest.pop("optional_permissions", None)
                manifest["background"].pop("scripts", None)
            else:
                manifest.pop("minimum_chrome_version", None)
                manifest["permissions"] = [permission for permission in manifest["permissions"] if permission != "debugger"]
                manifest["background"].pop("service_worker", None)
            return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if filename in {"bridge.js", "service.js"}:
            text = raw.decode("utf-8")
            if text.count(SERVICE_ORIGINS_MARKER) != 1:
                raise ValueError("KAD helper origin marker changed; packaging refused")
            return text.replace(SERVICE_ORIGINS_MARKER, json.dumps([STORE_ORIGIN])).encode("utf-8")
        if filename == "popup.js":
            text = raw.decode("utf-8")
            if text.count(SERVICE_ORIGIN_MARKER) != 1:
                raise ValueError("KAD helper popup origin marker changed; packaging refused")
            return text.replace(SERVICE_ORIGIN_MARKER, json.dumps(STORE_ORIGIN)).encode("utf-8")
        return raw

    return _write_archive(STORE_FILES, source)
