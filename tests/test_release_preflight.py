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
    def test_export_ignore_omits_synthetic_symlink_without_deleting_git_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            fixture_dir = repo / "synthetic-validation"
            fixture_dir.mkdir()
            target = fixture_dir / "source.pdf"
            target.write_bytes(b"synthetic document")
            link = fixture_dir / "linked.pdf"
            link.symlink_to("source.pdf")
            (repo / ".gitattributes").write_text(
                "synthetic-validation/linked.pdf export-ignore\n", encoding="utf-8",
            )

            def git(*args: str, text: bool = False) -> subprocess.CompletedProcess:
                return subprocess.run(
                    ["git", *args], cwd=repo, check=True, capture_output=True, text=text,
                )

            git("init", "--quiet")
            git("add", ".gitattributes", "synthetic-validation/source.pdf",
                "synthetic-validation/linked.pdf")
            git("-c", "user.name=Synthetic Test", "-c", "user.email=synthetic@example.invalid",
                "commit", "--quiet", "-m", "synthetic archive fixture")

            tracked = git("ls-tree", "-r", "--name-only", "HEAD", text=True).stdout.splitlines()
            self.assertEqual(
                tracked,
                [".gitattributes", "synthetic-validation/linked.pdf", "synthetic-validation/source.pdf"],
            )
            archive = git("archive", "--format=tar", "--worktree-attributes", "HEAD").stdout
            with tarfile.open(fileobj=__import__("io").BytesIO(archive), mode="r:") as tar:
                members = tar.getmembers()
                names = {member.name for member in members}
                self.assertIn("synthetic-validation/source.pdf", names)
                self.assertNotIn("synthetic-validation/linked.pdf", names)
                self.assertFalse(any(member.issym() for member in members))

            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_bytes(), b"synthetic document")


if __name__ == "__main__":
    unittest.main()
