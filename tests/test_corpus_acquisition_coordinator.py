from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from scripts.dev.quality import corpus_acquisition as acquisition
from scripts.dev.quality import corpus_acquisition_coordinator as coordinator


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "private-corpus"
        acquisition.private_root(self.root)
        self.clock = [1000.0]
        self.sleeps = []

    def queue_control(self, *, reason="browser_save_not_started", disabled=False, cooldown=30):
        state = acquisition.load_queue(self.root)
        state["last_request_at"] = self.clock[0] - 10
        state["source_controls"]["kad-browser-pdf"] = {
            "disabled": disabled, "reason": reason,
            "cooldown_until": self.clock[0] + cooldown if not disabled else 0,
        }
        acquisition.write_json(self.root / acquisition.STATE, state)

    def runner(self, results, *, case_cache=None):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            if case_cache is not None and not case_cache.exists():
                case_cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                case_cache.write_text('[{"private":"candidate"}]')
            return results.pop(0)
        return calls, run

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock[0] += seconds

    @staticmethod
    def result(returncode, status, reason=None):
        return SimpleNamespace(returncode=returncode,
                               stdout=json.dumps({"status": status, "reason": reason}), stderr="")

    def test_explicit_top_navigation_timeout_retries_fresh_search_not_stale_cache(self):
        cache = self.root / "discovery/cases.json"
        cache.parent.mkdir(mode=0o700, parents=True)
        cache.write_text('[{"stale":"previous stratum"}]')
        calls, run = self.runner([
            self.result(1, "discovery-failed", "timeout"),
            self.result(0, "complete"),
        ], case_cache=cache)
        outcome = coordinator.coordinate(self.root, year=2024, category="bankruptcy", runner=run,
                                         now=lambda: self.clock[0], sleep=self.sleep)
        self.assertEqual(outcome["status"], "complete")
        self.assertEqual(self.sleeps, [60.0])
        self.assertTrue(all("--resume-cases" not in call[0] for call in calls))

    def test_persisted_queue_cooldown_resumes_only_same_case_cache(self):
        cache = self.root / "discovery/cases.json"
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            if len(calls) == 1:
                cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                cache.write_text('[{"private":"candidate"}]')
                self.queue_control()
                return self.result(2, "source-paused")
            return self.result(0, "complete")
        outcome = coordinator.coordinate(self.root, year=2024, category="civil", runner=run,
                                         now=lambda: self.clock[0], sleep=self.sleep)
        self.assertEqual(outcome["status"], "complete")
        self.assertEqual(self.sleeps, [30.0])
        self.assertNotIn("--resume-cases", calls[0][0])
        self.assertIn("--resume-cases", calls[1][0])

    def test_changed_case_cache_blocks_resume(self):
        cache = self.root / "discovery/cases.json"
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            cache.write_text('[{"private":"candidate"}]')
            self.queue_control()
            return self.result(2, "source-paused")
        def mutate_during_cooldown(seconds):
            self.sleep(seconds)
            cache.write_text('[{"changed":"candidate set"}]')
        outcome = coordinator.coordinate(self.root, year=2024, category="civil", runner=run,
                                         now=lambda: self.clock[0], sleep=mutate_during_cooldown)
        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(len(calls), 1)

    def test_access_challenge_never_retries_or_resets_disabled_adapter(self):
        self.queue_control(reason="browser_access_challenge", disabled=True, cooldown=0)
        calls, run = self.runner([self.result(2, "source-paused", "challenge")])
        outcome = coordinator.coordinate(self.root, year=2024, category="bankruptcy", runner=run,
                                         now=lambda: self.clock[0], sleep=self.sleep)
        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(len(calls), 0)
        self.assertEqual(self.sleeps, [])
        self.assertTrue(acquisition.load_queue(self.root)["source_controls"]["kad-browser-pdf"]["disabled"])

    def test_untracked_allowed_cooldown_waits_then_starts_fresh_search(self):
        self.queue_control()
        calls, run = self.runner([self.result(0, "complete")])
        outcome = coordinator.coordinate(self.root, year=2024, category="bankruptcy", runner=run,
                                         now=lambda: self.clock[0], sleep=self.sleep)
        self.assertEqual(outcome["status"], "complete")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [30.0])
        self.assertNotIn("--resume-cases", calls[0][0])

    def test_persisted_counter_out_of_range_is_rejected(self):
        path = coordinator._state_path(self.root, 2024, "bankruptcy")
        acquisition.write_json(path, {"schema": 1, "year": 2024, "category": "bankruptcy",
                                      "retries": coordinator.MAX_RETRIES + 1, "idle_seconds": 0,
                                      "status": "new", "resume_mode": "fresh"})
        with self.assertRaises(acquisition.QueueError):
            coordinator.coordinate(self.root, year=2024, category="bankruptcy",
                                   runner=lambda *_a, **_k: self.fail("runner should not run"))

    def test_native_unknown_custody_stops_even_with_retryable_queue_cooldown(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            cache = self.root / "discovery/cases.json"
            cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            cache.write_text('[{"private":"candidate"}]')
            self.queue_control()
            (self.root / "inbox" / ".native-save-pending.json").write_text("private pending")
            return self.result(2, "source-paused")
        outcome = coordinator.coordinate(self.root, year=2024, category="bankruptcy", runner=run,
                                         now=lambda: self.clock[0], sleep=self.sleep)
        self.assertEqual(outcome["status"], "unknown")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_regular_reconciliation_note_is_not_unresolved_native_stage(self):
        inbox = self.root / "inbox"
        note = inbox / ".native-kad-save-reconciliation.json"
        note.write_text("private reconciliation note")
        self.assertFalse(coordinator._has_unresolved_native_save(self.root))
        stage = inbox / ".native-kad-save-stage"
        stage.mkdir(mode=0o700)
        self.assertTrue(coordinator._has_unresolved_native_save(self.root))

    def test_explicit_challenge_summary_overrides_stale_retryable_queue_cooldown(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            self.queue_control()
            return self.result(2, "source-paused", "challenge")
        result = coordinator.coordinate(self.root, year=2024, category="civil", runner=run,
                                        now=lambda: self.clock[0], sleep=self.sleep)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])
        second = coordinator.coordinate(self.root, year=2024, category="civil",
                                        runner=lambda *_a, **_k: self.fail("blocked challenge reran"),
                                        now=lambda: self.clock[0], sleep=self.sleep)
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(self.sleeps, [])

    def test_unknown_terminal_state_cannot_be_adopted_as_retryable_cooldown(self):
        self.queue_control()
        path = coordinator._state_path(self.root, 2024, "bankruptcy")
        acquisition.write_json(path, {"schema": 1, "year": 2024, "category": "bankruptcy",
                                      "retries": 0, "idle_seconds": 0, "status": "unknown",
                                      "resume_mode": "fresh", "reason": "process_outcome_unknown"})
        result = coordinator.coordinate(self.root, year=2024, category="bankruptcy",
                                        runner=lambda *_a, **_k: self.fail("unknown terminal state reran"),
                                        now=lambda: self.clock[0], sleep=self.sleep)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.sleeps, [])

    def test_process_restart_honors_persisted_wake_time_without_double_charging(self):
        def transient(_command, **_kwargs):
            raise RuntimeError("simulated coordinator interruption")
        # The real subprocess timeout path is terminal. Here the first navigation
        # result persists its cooldown, then the test interrupts during the wait.
        calls = []
        def first(command, **kwargs):
            calls.append(command)
            return self.result(1, "discovery-failed", "timeout")
        def interrupted_sleep(seconds):
            raise RuntimeError("simulated host interruption")
        with self.assertRaises(RuntimeError):
            coordinator.coordinate(self.root, year=2024, category="bankruptcy", runner=first,
                                   now=lambda: self.clock[0], sleep=interrupted_sleep)
        stored = json.loads(next((self.root / "coordinator").glob("*.json")).read_text())
        self.assertEqual(stored["status"], "cooldown")
        self.assertEqual(stored["retry_at"], 1060.0)
        self.clock[0] = 1030.0
        resumed_calls, resumed = self.runner([self.result(0, "complete")])
        result = coordinator.coordinate(self.root, year=2024, category="bankruptcy", runner=resumed,
                                        now=lambda: self.clock[0], sleep=self.sleep)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.sleeps, [30.0])

    def test_retry_count_and_cumulative_idle_are_bounded_per_stratum(self):
        calls, run = self.runner([
            self.result(1, "discovery-failed", "timeout"),
            self.result(1, "discovery-failed", "navigation"),
            self.result(1, "discovery-failed", "unavailable"),
        ])
        outcome = coordinator.coordinate(self.root, year=2024, category="civil", runner=run,
                                         now=lambda: self.clock[0], sleep=self.sleep)
        self.assertEqual(outcome["status"], "exhausted")
        self.assertEqual(len(calls), 3)  # initial process plus at most two fresh-process retries
        self.assertEqual(self.sleeps, [60.0, 60.0])
        stored = json.loads(next((self.root / "coordinator").glob("*.json")).read_text())
        self.assertEqual(stored["retries"], 2)
        self.assertEqual(stored["idle_seconds"], 120)


if __name__ == "__main__":
    unittest.main()
