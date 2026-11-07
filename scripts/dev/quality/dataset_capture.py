#!/usr/bin/env python3
"""One isolated local dataset capture; private payloads never go to stdout.

The caller owns process/resource supervision. This helper accesses no app/DB or
external service and does not adjudicate recognized values or PDF text layers.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout, redirect_stderr
import dataclasses
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SOURCE_SCOPE = ("sro_robot", "scripts/dev/quality", "requirements.in", "requirements.txt", "pyproject.toml")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_identity() -> dict:
    """Hash relevant tracked changes and untracked source, without exposing diff."""
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, timeout=5).decode().strip()
    patch = subprocess.check_output(["git", "diff", "--binary", "HEAD", "--", *SOURCE_SCOPE], cwd=ROOT, timeout=5)
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *SOURCE_SCOPE], cwd=ROOT, timeout=5,
    )
    state = hashlib.sha256(patch)
    for name in sorted(name for name in untracked.split(b"\0") if name):
        path = ROOT / os.fsdecode(name)
        if path.is_file():
            state.update(name + b"\0" + bytes.fromhex(digest(path)))
    return {"revision": revision, "source_dirty": bool(patch or untracked), "dirty_sha256": state.hexdigest()}


def runtime_identity(commands: list[str]) -> dict:
    """Identify actual invoked CLIs and only the two supported model files."""
    executables = []
    for command in dict.fromkeys(commands):
        resolved = shutil.which(command)
        if resolved is None:
            raise ValueError("capture_runtime_unavailable")
        executable = Path(resolved).resolve()
        entry = {"sha256": digest(executable), "models": {}, "models_identified": False}
        try:
            response = subprocess.run([str(executable), "--list-langs"], capture_output=True, text=True,
                                      timeout=2, check=True)
            match = re.search(r'List of available languages in "([^"]+)"', response.stdout + response.stderr)
            if match:
                directory = Path(match.group(1))
                entry["models"] = {name: digest(directory / (name + ".traineddata"))
                                   for name in ("rus", "osd") if (directory / (name + ".traineddata")).is_file()}
                entry["models_identified"] = set(entry["models"]) == {"rus", "osd"}
        except (OSError, subprocess.SubprocessError):
            pass
        executables.append(entry)
    versions = {}
    for package in ("Pillow", "pillow-heif", "pytesseract", "pdf2image"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "unavailable"
    tools = {}
    for name in ("pdftotext", "pdfinfo", "pdftoppm"):
        executable = shutil.which(name)
        tools[name] = digest(Path(executable)) if executable else None
    return {"reader_executables": tools, "platform": platform.system(), "machine": platform.machine(),
            "python": platform.python_version(), "packages": versions,
            "ocr_executables": executables,
            "native_optout": os.environ.get("SRO_TESSERACT_NATIVE") == "0"}


def capture_document(path: Path) -> dict:
    """Capture one file in a disposable process, with a 50-second OCR budget.

    Accepting useful embedded PDF text with zero OCR calls is deliberate. That
    source remains a weak label, never independently verified ground truth.
    Primary pages come from the first returned variant, without best-of scoring.
    """
    from sro_robot.core import extract, entity_extract, ocr
    import pytesseract

    path = Path(path)
    original_hash = digest(path)
    identity = source_identity()
    attempts, readings, commands = [], [], []
    completed = 0
    api = pytesseract.pytesseract
    original_run = api.run_tesseract
    originals = [(extract, "parse", extract.parse), (entity_extract, "parse_entity", entity_extract.parse_entity),
                 (ocr, "_tesseract", ocr._tesseract), (ocr, "_tesseract_with_source", ocr._tesseract_with_source)]

    def counted(*args, **kwargs):
        nonlocal completed
        command = str(api.tesseract_cmd)
        result = original_run(*args, **kwargs)
        completed += 1
        commands.append(command)
        return result

    def parser_wrapper(function):
        def wrapped(texts):
            card = function(texts)
            attempts.append((card, list(texts)))
            return card
        return wrapped

    def reader_wrapper(function, with_source):
        def wrapped(image, config, budget):
            result = function(image, config, budget)
            text, tsv = result if with_source else (result, None)
            readings.append({"config": config, "size": list(image.size), "text": text, "tsv": tsv})
            return result
        return wrapped

    budget = ocr.OcrBudget(deadline_seconds=50, tesseract_timeout_seconds=20, allow_native_tesseract=True)
    started = time.perf_counter()
    api.run_tesseract = counted
    for module, name, original in originals:
        setattr(module, name, parser_wrapper(original) if name in {"parse", "parse_entity"}
                else reader_wrapper(original, name == "_tesseract_with_source"))
    try:
        card = extract.parse_file(path, budget=budget)
    finally:
        api.run_tesseract = original_run
        for module, name, original in originals:
            setattr(module, name, original)
    seconds = time.perf_counter() - started
    accepted = [texts for result, texts in attempts if result is card]
    if len(accepted) != 1 or not accepted[0]:
        raise ValueError("capture_parser_identity_failed")
    primary = accepted[0][0]
    pages = primary.split("\f")
    if primary.endswith("\f"):
        pages.pop()
    text_pages = set(budget.text_layer_pages)
    # The reader's all-text fast path returns before populating its budget
    # page set. Reproduce only its explicit usability test, never correctness.
    if (path.suffix.lower() == ".pdf" and not completed and not budget.ocr_pages
            and len(primary.strip()) > 400 and all(len(page.strip()) >= 120 for page in pages)):
        text_pages.update(range(1, len(pages) + 1))
    text_pdf = path.suffix.lower() == ".pdf" and bool(text_pages) and not budget.ocr_pages
    if not completed and not text_pdf:
        raise ValueError("capture_ocr_liveness_failed")
    if original_hash != digest(path) or identity != source_identity():
        raise ValueError("capture_input_or_source_changed")
    snapshot = dataclasses.asdict(card)
    snapshot.pop("raw_text", None)
    return {"input_sha256": original_hash, "variants": accepted[0], "observed_pipeline": snapshot,
            "primary_pages": [{"page": i + 1, "text": text,
                               "origin": "ocr" if i + 1 in budget.ocr_pages else
                                         "embedded-text-weak" if i + 1 in text_pages else "unknown"}
                              for i, text in enumerate(pages)],
            "readings": readings,
            "capture": {"seconds": seconds, "completed_ocr": completed,
                        "parser_attempts": len(attempts), "accepted_parser_identity": True,
                        "ocr_pages": sorted(budget.ocr_pages), "text_layer_pages": sorted(text_pages),
                        "recovery_reasons": list(budget.recovery_reasons), "reference": "unverified",
                        **identity, "runtime": runtime_identity(commands)}}


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("capture_arguments_invalid")


def main(argv=None) -> int:
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    temporary = None
    previous_logging_disable = logging.root.manager.disable
    try:
        args = parser.parse_args(argv)
        root = args.private_root.resolve(strict=True)
        source, output = args.input.resolve(strict=True), args.output.resolve()
        if (root.is_relative_to(ROOT.resolve()) or ROOT.resolve().is_relative_to(root) or root == Path(root.anchor)
                or not root.is_dir() or source == output
                or not source.is_relative_to(root) or not output.is_relative_to(root)):
            raise ValueError("capture_private_boundary")
        # Library warnings may contain private paths; the isolated helper emits
        # only its fixed failure token. The payload is an atomic mode-0600 file.
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            result = capture_document(source)
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile("w", dir=output.parent, encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False)
        os.replace(temporary, output)
        return 0
    except Exception:
        print("dataset_capture_failed", file=sys.stderr)
        return 1
    finally:
        logging.disable(previous_logging_disable)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
