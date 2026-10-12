#!/usr/bin/env python3
"""Run the pinned Python dependency audit in an isolated, hash-locked venv."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
AUDITOR_REQUIREMENTS = ROOT / "requirements-audit.txt"
APP_REQUIREMENTS = ROOT / "requirements.txt"
SCRATCH_PREFIX = "sro-pip-audit-"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
    preserve_scratch = False
    try:
        venv = scratch / "venv"
        created = run([sys.executable, "-m", "venv", str(venv)], timeout=120)
        if created.returncode:
            preserve_scratch = True
            print(created.stderr, file=sys.stderr, end="")
            print(f"Dependency audit setup failed; scratch retained at {scratch}", file=sys.stderr)
            return created.returncode

        python = venv / "bin" / "python"
        install = run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "-r",
                str(AUDITOR_REQUIREMENTS),
            ]
        )
        (scratch / "install.stdout").write_text(install.stdout, encoding="utf-8")
        (scratch / "install.stderr").write_text(install.stderr, encoding="utf-8")
        if install.returncode:
            preserve_scratch = True
            print(install.stderr[-4000:], file=sys.stderr, end="")
            print(f"Hash-locked auditor installation failed; scratch retained at {scratch}", file=sys.stderr)
            return install.returncode

        tool = venv / "bin" / "pip-audit"
        version = run([str(tool), "--version"], timeout=30)
        audit = run([str(tool), "-r", str(APP_REQUIREMENTS), "--format", "json"])
        (scratch / "audit.stdout").write_text(audit.stdout, encoding="utf-8")
        (scratch / "audit.stderr").write_text(audit.stderr, encoding="utf-8")
        if audit.stdout:
            print(audit.stdout, end="")

        try:
            report = json.loads(audit.stdout)
        except json.JSONDecodeError:
            preserve_scratch = True
            print(audit.stderr[-4000:], file=sys.stderr, end="")
            print(f"Dependency audit output was invalid; scratch retained at {scratch}", file=sys.stderr)
            return audit.returncode or 1

        dependency_count = len(report.get("dependencies", []))
        vulnerability_count = sum(
            len(dependency.get("vulns", []))
            for dependency in report.get("dependencies", [])
        )
        print(
            f"{version.stdout.strip()} dependencies={dependency_count} "
            f"vulnerabilities={vulnerability_count} "
            f"requirements_sha256={digest(APP_REQUIREMENTS)} "
            f"auditor_lock_sha256={digest(AUDITOR_REQUIREMENTS)}",
            file=sys.stderr,
        )
        if audit.stderr:
            print(audit.stderr, file=sys.stderr, end="")
        if audit.returncode or vulnerability_count:
            preserve_scratch = True
            print(f"Findings or incomplete audit; raw output retained at {scratch}", file=sys.stderr)
            return audit.returncode or 1
        return 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        preserve_scratch = True
        print(f"Dependency audit failed: {type(exc).__name__}; raw output retained at {scratch}", file=sys.stderr)
        return 2
    finally:
        if not preserve_scratch:
            shutil.rmtree(scratch)


if __name__ == "__main__":
    raise SystemExit(main())
