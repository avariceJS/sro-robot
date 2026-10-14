#!/usr/bin/env python3
"""Clone the synthetic 100-PDF corpus and replace one item with corrupt bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


CORRUPT_PDF = b"synthetic damaged PDF fixture; intentionally not a PDF header\n"


def prepare(source_root: Path, output_root: Path, index: int) -> dict:
    manifest_path = source_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("generator") != "scripts/dev/nightly/generate_corpus.py":
        raise SystemExit("Refusing: expected a generated synthetic corpus")
    documents = manifest.get("documents", [])
    if len(documents) != 100 or len({item["sha256"] for item in documents}) != 100:
        raise SystemExit("Refusing: expected 100 unique synthetic PDFs")
    if not 0 <= index < len(documents):
        raise SystemExit("Corruption index must be between 0 and 99")
    if output_root.exists() and (not output_root.is_dir() or any(output_root.iterdir())):
        raise SystemExit("Refusing: output path is not an empty directory")
    output_root.mkdir(parents=True, exist_ok=True)
    for item in documents:
        shutil.copyfile(source_root / item["filename"], output_root / item["filename"])

    target = documents[index]
    path = output_root / target["filename"]
    original = target["sha256"]
    path.write_bytes(CORRUPT_PDF)
    target["bytes"] = len(CORRUPT_PDF)
    target["sha256"] = hashlib.sha256(CORRUPT_PDF).hexdigest()
    target["kind"] = "corrupt_synthetic"
    manifest["failure_isolation_fixture"] = {
        "filename": target["filename"],
        "original_sha256": original,
        "corrupt_sha256": target["sha256"],
        "corruption": "replaced file bytes with a short non-PDF synthetic string",
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest["failure_isolation_fixture"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-corpus", type=Path, required=True)
    parser.add_argument("--output-corpus", type=Path, required=True)
    parser.add_argument("--index", type=int, default=50)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source_corpus, args.output_corpus, args.index), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
