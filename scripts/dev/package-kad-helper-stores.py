#!/usr/bin/env python3
"""Build deterministic Chrome and Edge store ZIP candidates."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.kad_helper_package import store_archive  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path, help="directory for store ZIP candidates")
    parser.add_argument(
        "--include-firefox-experimental", action="store_true",
        help="also build a controlled-use Firefox ZIP; it is not ready for AMO submission",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    browsers = ["chrome", "edge"]
    if args.include_firefox_experimental:
        browsers.append("firefox")
    for browser in browsers:
        suffix = "-experimental" if browser == "firefox" else ""
        destination = args.output_dir / f"sro-kad-helper-{browser}{suffix}.zip"
        archive = store_archive(browser)
        destination.write_bytes(archive)
        print(f"{browser}: {destination} size={len(archive)} sha256={hashlib.sha256(archive).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
