from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from scripts.dev.quality import corpus_acquisition as acquisition
from scripts.dev.quality import corpus_acquisition_coordinator as coordinator


class CoordinatorTests(unittest.TestCase):
    def test_month_strata_have_distinct_state_and_propagate_exact_search_range(self):
        legacy = coordinator._state_path(self.root, 2026, 'bankruptcy')
        self.assertEqual(legacy.stem, hashlib.sha256(b'2026:bankruptcy').hexdigest()[:24])
        march = coordinator._state_path(self.root, 2026, 'bankruptcy', 3)
        june = coordinator._state_path(self.root, 2026, 'bankruptcy', 6)
        self.assertEqual(len({legacy, march, june}), 3)
        calls, runner = self.runner([self.result(0, 'complete')])
        result = coordinator.coordinate(self.root, year=2026, category='bankruptcy', month=3, runner=runner)
        self.assertEqual(result['status'], 'complete')
        command = calls[0][0]
        self.assertEqual(command[command.index('--month') + 1], '3')
        with self.assertRaises(acquisition.QueueError):
            coordinator._load_state(march, 2026, 'bankruptcy', 6)
        for value in (0, 13, True):
            with self.subTest(month=value), self.assertRaises(acquisition.QueueError):
                coordinator.coordinate(self.root, year=2026, category='bankruptcy', month=value, runner=runner)

    def test_owned_claim_precedes_unknown_child_marker_and_flag_is_propagated(self):
        from scripts.dev.quality import kad_discovery
        order = []
        def claim(root, year, category):
            state = coordinator._load_state(coordinator._state_path(root, year, category), year, category)
            self.assertEqual(state['status'], 'ready')
            order.append('claim')
        def run(command, **_kwargs):
            self.assertIn('--owned-browser', command)
            state = coordinator._load_state(coordinator._state_path(self.root, 2020, 'bankruptcy'), 2020, 'bankruptcy')
            self.assertEqual(state['status'], 'unknown')
            order.append('child')
            return self.result(0, 'complete')
        with patch.object(kad_discovery, 'ensure_owned_browser', side_effect=claim):
            outcome = coordinator.coordinate(self.root, year=2020, category='bankruptcy', owned_browser=True, runner=run)
        self.assertEqual(outcome['status'], 'complete')
        self.assertEqual(order, ['claim', 'child'])

    def test_response_capture_config_without_qualified_discovery_driver_fails_closed(self):
        from scripts.dev.quality import kad_discovery
        discovery = self.root / 'discovery'
        discovery.mkdir(mode=0o700, exist_ok=True)
        config = discovery / 'browser-response-capture.json'
        config.write_text('{"private":"configuration"}')
        config.chmod(0o600)
        calls, runner = self.runner([self.result(0, 'complete')])
        with patch.object(kad_discovery, 'ensure_owned_browser', side_effect=AssertionError('native UI must not be claimed')):
            outcome = coordinator.coordinate(self.root, year=2026, category='bankruptcy', month=6,
                                             owned_browser=True, runner=runner)
        self.assertEqual(outcome['status'], 'blocked')
        self.assertEqual(outcome['reason'], 'response_discovery_unavailable')
        self.assertEqual(calls, [])
        state = coordinator._load_state(coordinator._state_path(self.root, 2026, 'bankruptcy', 6),
                                        2026, 'bankruptcy', 6)
        self.assertEqual(state['status'], 'blocked')
        self.assertEqual(state['reason'], 'response_discovery_unavailable')
        with self.assertRaisesRegex(acquisition.QueueError, 'response_discovery_unavailable'):
            coordinator._command(self.root, 2026, 'bankruptcy', 25, 1, False, owned_browser=True, month=6)

    def test_response_config_preserves_existing_unknown_hold(self):
        from scripts.dev.quality import kad_discovery
        discovery = self.root / 'discovery'
        discovery.mkdir(mode=0o700, exist_ok=True)
        config = discovery / 'browser-response-capture.json'
        config.write_text('{"private":"configuration"}')
        config.chmod(0o600)
        path = coordinator._state_path(self.root, 2026, 'bankruptcy', 6)
        state = coordinator._load_state(path, 2026, 'bankruptcy', 6)
        state.update(status='unknown', reason='native_save_custody_unresolved')
        acquisition.write_json(path, state)
        calls, runner = self.runner([self.result(0, 'complete')])
        with patch.object(kad_discovery, 'ensure_owned_browser', side_effect=AssertionError('native UI must not be claimed')):
            outcome = coordinator.coordinate(self.root, year=2026, category='bankruptcy', month=6,
                                             owned_browser=True, runner=runner)
        self.assertEqual(outcome['status'], 'unknown')
        self.assertEqual(calls, [])
        saved = coordinator._load_state(path, 2026, 'bankruptcy', 6)
        self.assertEqual((saved['status'], saved['reason']), ('unknown', 'native_save_custody_unresolved'))

    def test_terminal_hold_never_claims_owned_browser(self):
        from scripts.dev.quality import kad_discovery
        path = coordinator._state_path(self.root, 2020, 'civil')
        state = coordinator._load_state(path, 2020, 'civil')
        state['status'] = 'unknown'
        acquisition.write_json(path, state)
        with patch.object(kad_discovery, 'ensure_owned_browser', side_effect=AssertionError('owned UI before reconciliation')):
            result = coordinator.coordinate(self.root, year=2020, category='civil', owned_browser=True)
        self.assertEqual(result['status'], 'unknown')

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
