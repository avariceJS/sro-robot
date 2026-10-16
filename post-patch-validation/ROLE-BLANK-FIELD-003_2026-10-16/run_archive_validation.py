"""Run pinned baseline/patch checks in git-archive snapshots, without Git writes.

The bundled PPV runner creates registered worktrees. This repository explicitly
forbids extra worktrees, so this adapter preserves the same seven evidence lenses
with immutable archive inputs and side-blind exploit/variant commands.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tarfile
import tempfile


PLAN_DIR = Path(__file__).resolve().parent
REPO = PLAN_DIR.parents[1]
PLAN = json.loads((PLAN_DIR / "plan.json").read_text())
CHECKS = PLAN_DIR / "checks.py"
PATCH = PLAN_DIR / "patch.diff"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
        timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, check=False)


def snapshot(target: Path, archive: bytes) -> None:
    target.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        # The historical repository contains unrelated absolute symlink test
        # artifacts. This finding only needs the Python source and tests.
        selected = [member for member in tar.getmembers()
                    if member.name in ("sro_robot", "tests")
                    or member.name.startswith(("sro_robot/", "tests/"))]
        if any(member.issym() or member.islnk() for member in selected):
            raise RuntimeError("Selected archive contains a link")
        tar.extractall(target, members=selected, filter="data")


def check_one(kind: str, case: str, checkout: Path, raw_dir: Path,
              check_id: str, side: str) -> dict:
    scratch = checkout / ("scratch-" + secrets.token_hex(8))
    scratch.mkdir()
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(scratch),
        "LANG": "C", "LC_ALL": "C", "TZ": "UTC", "PYTHONHASHSEED": "0",
        "NO_COLOR": "1", "TERM": "dumb", "PYTHONPATH": str(checkout),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PPV_REACHED_MARKER": "PPV_REACHED",
    }
    if kind == "suite":
        argv = [sys.executable, "-m", "unittest", "tests.test_core", "tests.test_ocr"]
    else:
        argv = [sys.executable, str(CHECKS), case]
    result = run(argv, cwd=checkout, env=env)
    stdout_path = raw_dir / f"{check_id}-{side}.stdout"
    stderr_path = raw_dir / f"{check_id}-{side}.stderr"
    stdout_path.write_bytes(result.stdout)
    stderr_path.write_bytes(result.stderr)
    return {
        "exit_code": result.returncode,
        "stdout_sha256": sha256(result.stdout),
        "stderr_sha256": sha256(result.stderr),
        "marker_reached": b"PPV_REACHED\n" in result.stdout,
        "stdout": f"streams/{stdout_path.name}",
        "stderr": f"streams/{stderr_path.name}",
        "stdout_bytes": result.stdout,
        "stderr_bytes": result.stderr,
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: run_archive_validation.py RESULT_DIRECTORY")
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    if sha256(PATCH.read_bytes()) != PLAN["patch_sha256"]:
        raise RuntimeError("Pinned patch hash changed")
    if sha256(CHECKS.read_bytes()) != PLAN["checks_sha256"]:
        raise RuntimeError("Pinned checks hash changed")
    if run(["git", "rev-parse", "--verify", PLAN["base_ref"]], cwd=REPO).returncode:
        raise RuntimeError("Base commit missing")
    archive_result = run(["git", "archive", "--format=tar", PLAN["base_ref"]],
                         cwd=REPO, timeout=120)
    if archive_result.returncode:
        raise RuntimeError("git archive failed")
    raw_dir = output / "streams"
    raw_dir.mkdir()
    with tempfile.TemporaryDirectory(prefix="sro-role-ppv-archives-") as temp:
        temp_path = Path(temp)
        roots = {side: temp_path / secrets.token_hex(12) for side in ("base", "patched")}
        for root in roots.values():
            snapshot(root, archive_result.stdout)
        patched = run(["patch", "--batch", "--forward", "-p1", "-i", str(PATCH)],
                      cwd=roots["patched"])
        if patched.returncode:
            raise RuntimeError("Pinned patch did not apply to archive: " + patched.stderr.decode(errors="replace"))
        observations: list[dict] = []
        for check in PLAN["checks"]:
            kind = check["kind"]
            sides = ["patched"] if kind == "suite" else ["base", "patched"]
            if kind in ("exploit", "variant") and secrets.randbelow(2):
                sides.reverse()
            runs = {}
            for side in sides:
                observed = check_one(kind, check["case"], roots[side], raw_dir,
                                     check["id"], side)
                runs[side] = {key: value for key, value in observed.items()
                              if key not in ("stdout_bytes", "stderr_bytes")}
                runs[side]["stdout_text"] = observed["stdout_bytes"].decode(errors="replace")
                runs[side]["stderr_tail"] = observed["stderr_bytes"].decode(errors="replace")[-500:]
            if kind in ("exploit", "variant"):
                good = (runs["base"]["exit_code"] != 0 and runs["patched"]["exit_code"] == 0
                        and runs["base"]["marker_reached"] and runs["patched"]["marker_reached"]
                        and "AssertionError" in runs["base"]["stderr_tail"])
            elif kind == "behavior":
                good = (runs["base"]["exit_code"] == runs["patched"]["exit_code"] == 0
                        and runs["base"]["stdout_sha256"] == runs["patched"]["stdout_sha256"])
            elif kind == "suite":
                good = runs["patched"]["exit_code"] == 0
            else:
                good = all(item["exit_code"] == 0 for item in runs.values())
            observations.append({"id": check["id"], "kind": kind, "passed": good, "runs": runs})

    report = {
        "finding_id": PLAN["finding_id"], "evidence_level": PLAN["evidence_level"],
        "base_ref": PLAN["base_ref"], "patch_sha256": PLAN["patch_sha256"],
        "checks_sha256": PLAN["checks_sha256"],
        "archive_sha256": sha256(archive_result.stdout),
        "checks": observations,
        "assessment": {
            "status": "complete" if all(item["passed"] for item in observations) else "incomplete",
            "findings": [item["id"] for item in observations if not item["passed"]],
            "gaps": [], "human_review_required": True,
        },
    }
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["# Role-bound extraction: archive validation", "",
             f"Evidence level: {PLAN['evidence_level']}. Base: `{PLAN['base_ref']}`.",
             f"Patch SHA-256: `{PLAN['patch_sha256']}`.", "",
             "The repository's no-extra-worktree rule required archive snapshots instead of the bundled PPV runner.",
             "Exploit and variant checks used identical side-blind assertions and printed `PPV_REACHED` before them.",
             "", "| Check | Kind | Result |", "| --- | --- | --- |"]
    lines += [f"| {item['id']} | {item['kind']} | {'pass' if item['passed'] else 'FAIL'} |"
              for item in observations]
    lines += ["", f"Assessment: {report['assessment']['status']}; human review required.", ""]
    (output / "report.md").write_text("\n".join(lines))
    manifest = {"schema_version": "1.0", "files": []}
    for artifact in sorted(path for path in output.rglob("*") if path.is_file()):
        manifest["files"].append({"path": artifact.relative_to(output).as_posix(),
                                  "bytes": artifact.stat().st_size,
                                  "sha256": sha256(artifact.read_bytes())})
    (output / "artifact-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"assessment": report["assessment"], "output": str(output)}, ensure_ascii=False))
    return 0 if report["assessment"]["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
