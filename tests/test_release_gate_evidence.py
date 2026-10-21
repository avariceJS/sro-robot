from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "ops" / "release_gate_evidence.py"
SPEC = importlib.util.spec_from_file_location("release_gate_evidence", SOURCE)
assert SPEC is not None and SPEC.loader is not None
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)


class ReleaseGateEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.commit = "a" * 40
        self.tree = "b" * 40
        self.record = {
            "schema_version": 1,
            "source_commit": self.commit,
            "source_tree": self.tree,
            "verification_command": EVIDENCE.VERIFY_COMMAND,
            "verification_exit_code": 0,
            "verification_log_sha256": "c" * 64,
            "verified_at_utc": "2026-10-21T12:00:00+00:00",
        }

    def test_matching_success_record_is_accepted(self) -> None:
        EVIDENCE.validate_record(self.record, self.commit, self.tree)

    def test_source_and_nonzero_gate_results_are_rejected(self) -> None:
        with self.assertRaisesRegex(EVIDENCE.EvidenceError, "EvidenceSourceMismatch"):
            EVIDENCE.validate_record(self.record, "d" * 40, self.tree)
        failed = dict(self.record, verification_exit_code=1)
        with self.assertRaisesRegex(EVIDENCE.EvidenceError, "ExactLocalGateNotPassed"):
            EVIDENCE.validate_record(failed, self.commit, self.tree)

    def test_invalid_log_hash_or_command_is_rejected(self) -> None:
        invalid_hash = dict(self.record, verification_log_sha256="not-a-hash")
        with self.assertRaisesRegex(EVIDENCE.EvidenceError, "InvalidVerificationLogHash"):
            EVIDENCE.validate_record(invalid_hash, self.commit, self.tree)
        wrong_command = dict(self.record, verification_command="true")
        with self.assertRaisesRegex(EVIDENCE.EvidenceError, "ExactLocalGateNotPassed"):
            EVIDENCE.validate_record(wrong_command, self.commit, self.tree)


if __name__ == "__main__":
    unittest.main()
