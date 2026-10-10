from __future__ import annotations

from pathlib import Path
import re
import unittest

import yaml


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "verify.yml"


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
        install_step = next(step for step in job["steps"] if step.get("name") == "Install pinned dependencies")
        self.assertIn("pip install -r requirements.txt", install_step["run"])
        verify_step = next(step for step in job["steps"] if step.get("name") == "Run local verification suite")
        self.assertEqual(verify_step["run"], "scripts/dev/verify.sh")


if __name__ == "__main__":
    unittest.main()
