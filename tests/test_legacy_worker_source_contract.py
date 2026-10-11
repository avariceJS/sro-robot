from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "dev" / "legacy_worker_schema_smoke.py"
_SPEC = importlib.util.spec_from_file_location("legacy_worker_schema_smoke", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Could not load the legacy worker schema smoke helper")
_SMOKE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SMOKE)


class LegacyWorkerSourceContractTests(unittest.TestCase):
    def test_accepts_worker_module_from_expected_revision_archive(self) -> None:
        source_root = Path("/tmp/synthetic-legacy-source")
        expected_worker = source_root / "app" / "workers" / "jobs.py"
        with (
            patch.dict(os.environ, {"SRO_EXPECT_WORKER_SOURCE_ROOT": str(source_root)}),
            patch.object(_SMOKE.jobs, "__file__", str(expected_worker)),
        ):
            _SMOKE._assert_expected_worker_source()

    def test_rejects_current_or_unexpected_worker_module(self) -> None:
        source_root = Path("/tmp/synthetic-legacy-source")
        with (
            patch.dict(os.environ, {"SRO_EXPECT_WORKER_SOURCE_ROOT": str(source_root)}),
            patch.object(_SMOKE.jobs, "__file__", "/tmp/current/app/workers/jobs.py"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Worker source did not match the pinned compatibility revision"
            ):
                _SMOKE._assert_expected_worker_source()


if __name__ == "__main__":
    unittest.main()
