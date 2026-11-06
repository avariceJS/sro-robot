"""Synthetic benchmark accounting and direct-execution checks; no private corpus."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('ocr_compare', Path(__file__).resolve().parents[1] / 'scripts/dev/ocr_compare.py')
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


class OcrCompareTests(unittest.TestCase):
    def test_nested_phase_time_is_not_added_twice(self):
        with patch.object(bench.time, 'perf_counter', side_effect=[0, 1, 3, 6, 10, 12]):
            clock = bench.Clock()
            with clock.measure('preparation'):
                with clock.measure('recognition'):
                    pass
            clock.switch('done')
        self.assertEqual(dict(clock.seconds), {'startup': 3, 'preparation': 6, 'recognition': 3})
        self.assertEqual(sum(clock.seconds.values()), 12)

    def test_worker_socket_guard_rejects_connect_and_datagrams(self):
        for event in ('socket.__new__', 'socket.connect', 'socket.getaddrinfo', 'socket.sendto', 'socket.sendmsg'):
            with self.assertRaises(RuntimeError):
                bench.deny_network(event, ())

    @unittest.skipUnless(os.environ.get('SRO_OCR_COMPARE_INTEGRATION') == '1', 'requires isolated OCR experiment venv')
    def test_same_bytes_twice_run_real_tesseract_in_distinct_processes_and_clean_temp(self):
        from PIL import Image, ImageDraw
        bench.PRIVATE.mkdir(parents=True, exist_ok=True)
        (bench.PRIVATE / 'tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=bench.PRIVATE) as directory:
            source = Path(directory) / 'synthetic.png'
            image = Image.new('RGB', (1000, 600), 'white')
            draw = ImageDraw.Draw(image)
            for row in range(6):
                draw.text((60, 40 + row * 65), '1234567890 2099 14 01', fill='black', font_size=40)
            image.save(source)
            args = type('Args', (), {'baseline_python': bench.ROOT / '.venv/bin/python',
                                    'models': bench.PRIVATE / 'models', 'output': Path(directory),
                                    'baseline': bench.baseline_identity()})
            sha = bench.digest(source)
            results = [bench.run_one('tesseract', source, sha, args, i) for i in range(2)]
            self.assertNotEqual(results[0]['pid'], results[1]['pid'])
            for result in results:
                self.assertEqual(result['sha256'], sha)
                self.assertGreaterEqual(result['calls']['tesseract_completed'], 5)
                self.assertTrue(result['temp_removed'])
                self.assertAlmostEqual(sum(result['accounted_seconds'].values()), result['total_seconds'])
                self.assertGreater(result['peak_tree_rss_bytes'], 0)


class OcrCompareRunSetTests(unittest.TestCase):
    def test_run_set_requires_every_pair_and_rejects_changed_bytes(self):
        import json
        spec = importlib.util.spec_from_file_location('ocr_compare_check', bench.ROOT / 'scripts/dev/ocr_compare_check.py')
        audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = {'inputs': [{'index': 0, 'sha256': 'synthetic-hash', 'frames': 1}],
                        'repeats': 3, 'baseline': {'version': 'synthetic-runtime'}}
            (directory / 'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                audit.check(directory)
            index = 0
            for engine in ('tesseract', 'rapidocr'):
                for repeat in range(3):
                    prefix = 'tesseract' if engine == 'tesseract' else 'onnx'
                    row = {'engine': engine, 'document_index': 0, 'repeat': repeat,
                           'sha256': 'synthetic-hash', 'temp_removed': True,
                           'calls': {prefix + '_attempted': 1, prefix + '_completed': 1, 'pages_completed': 1},
                           'config': {'tesseract_runtime': manifest['baseline'], 'providers': [['CPUExecutionProvider']] * 3},
                           'accounted_seconds': {'backend_calls': 1.0}, 'total_seconds': 1.0,
                           'peak_tree_rss_bytes': 1024, 'fields': {}}
                    (directory / f'run-{index:03}.json').write_text(json.dumps(row))
                    index += 1
            result = audit.check(directory)
            self.assertEqual(result['verified_runs'], 6)
            self.assertFalse(result['quality_scored'])
            self.assertEqual(result['output_diagnostics_per_document_first_repeat']['inn']['both_empty'], 1)
            rapid = directory / 'run-003.json'
            original = rapid.read_text()
            row = json.loads(original)
            row['config']['providers'] = []
            rapid.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError, 'Unexpected runtime provider'):
                audit.check(directory)
            rapid.write_text(original)
            path = directory / 'run-000.json'
            row = json.loads(path.read_text())
            row['calls']['tesseract_attempted'] = 2
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError, 'No completed backend calls'):
                audit.check(directory)
            row['calls']['tesseract_attempted'] = 1
            row['sha256'] = 'changed'
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError, 'Changed bytes'):
                audit.check(directory)
