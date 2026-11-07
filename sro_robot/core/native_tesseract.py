"""Optional, locally qualified macOS CLI; never a recognition retry/backend pool.

Configure only at the single-job RQ process boundary, before OCR starts. Keeping
the CLI preserves pytesseract's process cancellation and document deadlines.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".local-run/tesseract-native"
SOURCE_COMMIT = "db0ec62f81b0737fbbe184d8fea40af5738f8eef"
POLICY = "tesseract-5.5.3-arm64-dotprod-v1"
_owned_command: str | None = None


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def supported_host() -> bool:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    return subprocess.check_output(
        ["/usr/sbin/sysctl", "-n", "hw.optional.arm.FEAT_DotProd"],
        timeout=1, text=True,
    ).strip() == "1"


def host_identity(tessdata: Path, leptonica: Path) -> dict:
    """Bind qualification to the effective default CLI, model set and decoder."""
    baseline = shutil.which("tesseract")
    if baseline is None:
        raise ValueError("System Tesseract unavailable")
    if any(name.startswith("DYLD_") for name in os.environ):
        raise ValueError("Custom dynamic loading must be qualified separately")
    system_library = Path(baseline).resolve().parents[1] / "lib/libtesseract.5.dylib"
    effective_data = Path(os.environ.get("TESSDATA_PREFIX", str(tessdata))).resolve()
    if effective_data != tessdata.resolve():
        raise ValueError("Custom tessdata must be qualified separately")
    # Only these models are requested by the production reader. Rehashing all
    # unrelated installed languages on each job wastes hundreds of MB of I/O.
    models = [tessdata / (name + ".traineddata") for name in ("rus", "osd")]
    if not all(p.is_file() for p in models):
        raise ValueError("Russian/OSD models unavailable")
    return {
        "system_cli": str(Path(baseline).resolve()),
        "system_sha256": sha256(Path(baseline)),
        "system_library_sha256": sha256(system_library),
        "tessdata": str(tessdata.absolute()),
        "tessdata_target": str(tessdata.resolve()),
        "models": {p.name: sha256(p) for p in models},
        "configs": {name: sha256(tessdata / "configs" / name)
                    for name in ("txt", "tsv") if (tessdata / "configs" / name).is_file()},
        "leptonica": str(leptonica.absolute()),
        "leptonica_target": str(leptonica.resolve()),
        "leptonica_sha256": sha256(leptonica),
        "patch_sha256": sha256(ROOT / "scripts/native/tesseract-5.5.3-dotprod.patch"),
    }


def qualified_command(runtime: Path = RUNTIME, *, manifest: dict | None = None) -> str | None:
    """Fail back before recognition if a host/artifact/dependency changed."""
    try:
        if os.environ.get("SRO_TESSERACT_NATIVE", "1") == "0" or not supported_host():
            return None
        if manifest is None:
            manifest_path = runtime / "qualified.json"
            if manifest_path.stat().st_size > 64_000:
                return None
            manifest = json.loads(manifest_path.read_text())
        if (manifest["policy"] != POLICY or manifest["source_commit"] != SOURCE_COMMIT
                or manifest["qualified"] is not True):
            return None
        identity = manifest["identity"]
        if identity != host_identity(Path(identity["tessdata"]), Path(identity["leptonica"])):
            return None
        digest = manifest["binary_sha256"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return None
        command = runtime / digest / "tesseract"
        if sha256(command) != digest:
            return None
        # Also proves dyld can load dependency closure. No input document used.
        version = subprocess.check_output(
            [str(command), "--version"], stderr=subprocess.STDOUT,
            timeout=1, text=True,
        )
        if not version.startswith("tesseract 5.5.3\n") or "leptonica-1.87.0" not in version:
            return None
        return str(command)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def configure_native_tesseract() -> bool:
    """Select once before a job's parse, preserving genuine caller overrides.

    Recheck our own selection on later jobs in non-forking test/dev workers; a
    stale artifact resets only our command. Never call while another OCR runs.
    """
    import pytesseract

    global _owned_command
    api = pytesseract.pytesseract
    if api.tesseract_cmd not in ("tesseract", _owned_command):
        return False
    selected = qualified_command()
    api.tesseract_cmd = selected or "tesseract"
    _owned_command = selected
    return selected is not None
