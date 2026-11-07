import importlib.util
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/dev/review-local.py'
spec = importlib.util.spec_from_file_location('review_launcher_test', SCRIPT)
launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)


class LocalLauncherTests(unittest.TestCase):
    def test_stop_does_not_open_a_database_that_may_be_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch('sro_robot.research.review_store.ReviewStore', side_effect=AssertionError('database unavailable')):
                with patch.object(sys, 'argv', [str(SCRIPT), 'stop', '--root', temporary]):
                    with patch('builtins.print'):
                        self.assertEqual(launcher.main(), 0)

    def test_replenishment_includes_current_month_and_excludes_future_months(self):
        strata = launcher.default_source_strata(date(2026, 11, 8))
        windows = {(item['year'], item['month']) for item in strata}
        self.assertEqual(len(windows), 23)
        self.assertIn((2026, 11), windows)
        self.assertIn((2025, 12), windows)
        self.assertNotIn((2026, 12), windows)
        self.assertTrue(all(year in {2025, 2026} for year, month in windows))

    def test_parser_update_does_not_block_access_to_existing_feedback(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary)
            (dataset / 'baseline.json').write_text(json.dumps({'source': {'revision': 'a'*40}}))
            with patch.object(launcher.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
                result = launcher.import_existing(SimpleNamespace(dataset=dataset), None)
            self.assertTrue(result['existing_queue_preserved'])
            self.assertEqual(result['imported'], 0)

    def test_operator_pause_survives_finishing_an_existing_pulse(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source-plan.json'
            launcher.update_source_plan(path, lambda p: p.update(enabled=True, next_index=0))
            launcher.update_source_plan(path, lambda p: p.update(enabled=False))
            launcher.update_source_plan(path, lambda p: p.update(next_index=1, last_added=5))
            self.assertIs(json.loads(path.read_text())['enabled'], False)

    def test_stop_targets_owned_process_group_and_preserves_unrelated_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = 'synthetic-owned-instance'
            child = subprocess.Popen([sys.executable, str(SCRIPT), 'collect', '--root', str(root), '--instance', instance],
                                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
            try:
                (root / 'collect-process.json').write_text(json.dumps({'pid': child.pid, 'instance': instance, 'port': 8767}))
                for _ in range(40):
                    if launcher.owned(root, 'collect'):break
                    time.sleep(.05)
                self.assertIsNotNone(launcher.owned(root, 'collect'))
                result = subprocess.run([sys.executable, str(SCRIPT), 'stop', '--root', str(root)], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0)
                child.wait(timeout=5)
                self.assertIsNone(unrelated.poll())
            finally:
                if child.poll() is None:child.terminate()
                unrelated.terminate(); unrelated.wait(timeout=5)
                child.wait(timeout=5)

    def test_invalid_private_arguments_are_not_echoed(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'PRIVATE_SYNTHETIC_ARGUMENT'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('PRIVATE_SYNTHETIC_ARGUMENT', result.stdout + result.stderr)
