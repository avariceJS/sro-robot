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
REQUIREMENT_LOCKS = (ROOT / "requirements.txt", ROOT / "requirements-test.txt")
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
        audits: list[dict[str, object]] = []
        dependency_count = 0
        vulnerability_count = 0
        audit_return_code = 0
        audit_stderr: list[str] = []
        for manifest in REQUIREMENT_LOCKS:
            audit = run([str(tool), "-r", str(manifest), "--format", "json"])
            (scratch / f"{manifest.stem}.stdout").write_text(audit.stdout, encoding="utf-8")
            (scratch / f"{manifest.stem}.stderr").write_text(audit.stderr, encoding="utf-8")
            audit_stderr.append(audit.stderr)
            audit_return_code = audit_return_code or audit.returncode
            try:
                report = json.loads(audit.stdout)
            except json.JSONDecodeError:
                preserve_scratch = True
                print(audit.stderr[-4000:], file=sys.stderr, end="")
                print(
                    f"Dependency audit output for {manifest.name} was invalid; scratch retained at {scratch}",
                    file=sys.stderr,
                )
                return audit.returncode or 1

            dependencies = report.get("dependencies", [])
            dependency_count += len(dependencies)
            vulnerability_count += sum(
                len(dependency.get("vulns", [])) for dependency in dependencies
            )
            audits.append({"requirements": manifest.name, **report})

        print(json.dumps({"audits": audits}, indent=2, sort_keys=True))
        print(
            f"{version.stdout.strip()} lock_entries={dependency_count} "
            f"vulnerabilities={vulnerability_count} "
            f"requirements_sha256={digest(REQUIREMENT_LOCKS[0])} "
            f"test_requirements_sha256={digest(REQUIREMENT_LOCKS[1])} "
            f"auditor_lock_sha256={digest(AUDITOR_REQUIREMENTS)}",
            file=sys.stderr,
        )
        for output in audit_stderr:
            if output:
                print(output, file=sys.stderr, end="")
        if audit_return_code or vulnerability_count:
            preserve_scratch = True
            print(f"Findings or incomplete audit; raw output retained at {scratch}", file=sys.stderr)
            return audit_return_code or 1
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
