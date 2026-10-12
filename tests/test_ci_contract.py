from __future__ import annotations

from pathlib import Path
import re
import unittest

import yaml


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "verify.yml"
DEPENDENCY_AUDIT_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "dependency-audit.yml"
)


class ContinuousIntegrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_workflow_has_read_only_repository_permissions(self) -> None:
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})

    def test_workflow_runs_on_supported_events_with_bounded_runner_time(self) -> None:
        self.assertEqual(
            set(self.workflow["on"]),
            {"push", "pull_request", "workflow_dispatch"},
        )
        job = self.workflow["jobs"]["verify"]
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertEqual(int(job["timeout-minutes"]), 20)
        self.assertEqual(job["env"]["SRO_DRILL_TASKS"], "100000")
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], "true")

    def test_actions_are_commit_pinned_and_same_local_verifier_runs(self) -> None:
        job = self.workflow["jobs"]["verify"]
        action_steps = [step["uses"] for step in job["steps"] if "uses" in step]
        self.assertGreaterEqual(len(action_steps), 2)
        for action in action_steps:
            with self.subTest(action=action):
                self.assertRegex(action, re.compile(r"^.+@[0-9a-f]{40}$"))

        setup_python = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python@"))
        self.assertEqual(setup_python["with"]["python-version"], "3.13")
        install_step = next(step for step in job["steps"] if step.get("name") == "Install hash-locked dependencies")
        self.assertLess(
            install_step["run"].index("-r requirements-bootstrap.txt"),
            install_step["run"].index("-r requirements.txt"),
        )
        self.assertIn("pip install --require-hashes -r requirements.txt", install_step["run"])
        self.assertIn("pip install --require-hashes -r requirements-test.txt", install_step["run"])
        audit_step = next(step for step in job["steps"] if step.get("name") == "Audit installed Python environment")
        self.assertIn("pip install --require-hashes -r requirements-audit.txt", audit_step["run"])
        self.assertIn("python -m pip_audit --local --vulnerability-service pypi", audit_step["run"])
        verify_step = next(step for step in job["steps"] if step.get("name") == "Run local verification suite")
        self.assertLess(
            job["steps"].index(audit_step),
            job["steps"].index(verify_step),
        )
        self.assertEqual(verify_step["run"], "scripts/dev/verify.sh")

    def test_every_exact_dependency_pin_has_release_hashes(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        package_pattern = re.compile(r"^[A-Za-z0-9_.-]+==[^\s]+$")
        hash_pattern = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")
        for manifest in (
            "requirements-bootstrap.txt",
            "requirements.txt",
            "requirements-test.txt",
            "requirements-audit.txt",
        ):
            with self.subTest(manifest=manifest):
                requirements = (repo_root / manifest).read_text(encoding="utf-8").splitlines()
                index = 0
                pin_count = 0
                while index < len(requirements):
                    line = requirements[index].strip()
                    if not line or line.startswith("#"):
                        index += 1
                        continue
                    self.assertTrue(line.endswith("\\"), f"requirement lacks a continuation: {line}")
                    self.assertRegex(line[:-1].strip(), package_pattern)
                    pin_count += 1
                    index += 1
                    hashes_for_pin = 0
                    while index < len(requirements):
                        hash_line = requirements[index].strip()
                        continues = hash_line.endswith("\\")
                        hash_value = hash_line[:-1].strip() if continues else hash_line
                        self.assertRegex(hash_value, hash_pattern)
                        hashes_for_pin += 1
                        index += 1
                        if not continues:
                            break
                    self.assertGreater(hashes_for_pin, 0)
                self.assertGreater(pin_count, 0)

    def test_bootstrap_installer_version_is_explicitly_pinned(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        manifest = (repo_root / "requirements-bootstrap.txt").read_text(encoding="utf-8")
        self.assertIn("pip==26.2.1 \\", manifest)

    def test_advisory_scanner_version_is_explicitly_pinned(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        manifest = (repo_root / "requirements-audit.txt").read_text(encoding="utf-8")
        self.assertIn("pip_audit==2.10.1 \\", manifest)

    def test_property_testing_library_is_explicitly_pinned(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        manifest = (repo_root / "requirements-test.txt").read_text(encoding="utf-8")
        self.assertIn("hypothesis==6.169.0 \\", manifest)

    def test_dependency_advisories_are_rechecked_weekly_and_on_manifest_changes(self) -> None:
        workflow = yaml.load(
            DEPENDENCY_AUDIT_WORKFLOW_PATH.read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            workflow["on"]["schedule"],
            [{"cron": "17 6 * * 1"}],
        )
        watched_paths = {
            "requirements.txt",
            "requirements-test.txt",
            "requirements-audit.txt",
            "scripts/dev/audit_python_dependencies.py",
            ".github/workflows/dependency-audit.yml",
        }
        for event in ("push", "pull_request"):
            with self.subTest(event=event):
                self.assertEqual(set(workflow["on"][event]["paths"]), watched_paths)

        job = workflow["jobs"]["audit"]
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertEqual(int(job["timeout-minutes"]), 10)
        self.assertEqual(
            [step["run"] for step in job["steps"] if "run" in step],
            ["python scripts/dev/audit_python_dependencies.py"],
        )
        for step in job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], re.compile(r"^.+@[0-9a-f]{40}$"))


if __name__ == "__main__":
    unittest.main()
