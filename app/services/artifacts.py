"""Safe access to private browser artifacts referenced by workflow rows."""

from __future__ import annotations

import os
from pathlib import Path
import stat


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
