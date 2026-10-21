from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_PATH = ROOT / "scripts/ops/release_preflight.py"
SPEC = importlib.util.spec_from_file_location("release_preflight", PREFLIGHT_PATH)
assert SPEC is not None and SPEC.loader is not None
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)


class ReleasePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.commit = "a" * 40
        self.release = self.base / "releases" / self.commit
        self.release.mkdir(parents=True)
        (self.release / ".release-commit").write_text(self.commit + "\n", encoding="ascii")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_clean_release_with_no_existing_venv_passes(self) -> None:
        PREFLIGHT.validate(self.base, self.commit)

    def test_existing_venv_and_private_env_file_are_refused(self) -> None:
        venv = self.base / "venvs" / self.commit
        venv.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "virtual environment already exists"):
            PREFLIGHT.validate(self.base, self.commit)
        venv.rmdir()
        (self.release / ".env").write_text("synthetic-only\n", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "private environment file"):
            PREFLIGHT.validate(self.base, self.commit)

    def test_symlinked_release_marker_is_refused(self) -> None:
        marker = self.release / ".release-commit"
        marker.unlink()
        target = self.base / "marker"
        target.write_text(self.commit, encoding="ascii")
        marker.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "marker is unavailable"):
            PREFLIGHT.validate(self.base, self.commit)


class ReleaseArchiveSafetyTests(unittest.TestCase):
    def test_export_omits_exact_symlink_fixtures_without_deleting_git_evidence(self) -> None:
        excluded = {
            "post-patch-validation/DOC-FILE-CREATE-SECURITY-001_2026-10-11/results/scratch/014-variant-symlink-destination-base/document.pdf",
            "post-patch-validation/DOC-FILE-CREATE-SECURITY-001_2026-10-11/results/scratch/015-variant-symlink-destination-patched/document.pdf",
        }
        tracked = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", *sorted(excluded)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertEqual(set(tracked), excluded)

        archive = subprocess.run(
            ["git", "archive", "--format=tar", "--worktree-attributes", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        with tarfile.open(fileobj=__import__("io").BytesIO(archive), mode="r:") as tar:
            names = {member.name for member in tar.getmembers()}
            self.assertTrue(excluded.isdisjoint(names))
            self.assertFalse(any(member.issym() for member in tar.getmembers()))


if __name__ == "__main__":
    unittest.main()
