"""Raw OCR fairness, shared raster, native lifecycle and reference safety tests."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts/dev'
sys.path.insert(0, str(SCRIPTS))
import ocr_engines_compare as raw
from ocr_engine_backends import file_hash, rgb_hash
from ocr_engine_metrics import normalized, validate_reference


class RasterTests(unittest.TestCase):
    def test_heif_orientation_uses_native_header_and_preserves_all_pixels(self):
        from PIL import Image
        for transforms, size in [((), (3024, 4032)), ((('irot', 90),), (4032, 3024))]:
            image = Image.new('RGB', (4032, 3024), 'white')
            image.format = 'HEIF'
            image.info['original_orientation'] = 6
            image._heif_file = [SimpleNamespace(_c_image=SimpleNamespace(transformations=transforms))]
            with patch.object(Image.Image, 'resize', side_effect=AssertionError('No raw input resize')):
                canonical, metadata = raw.canonical_rgb(image)
            self.assertEqual(canonical.size, size)
            self.assertEqual(canonical.width * canonical.height, 4032 * 3024)
            self.assertEqual(metadata['native_orientation_already_applied'], bool(transforms))
            canonical.close(); image.close()

    def test_unknown_heif_orientation_fails_instead_of_guessing(self):
        from PIL import Image
        image = Image.new('RGB', (12, 20))
        image.format = 'HEIF'; image.info['original_orientation'] = 6
        with self.assertRaisesRegex(ValueError, 'Cannot verify'):
            raw.canonical_rgb(image)

    def test_common_alpha_rule_is_explicit_and_rgb_hash_covers_dimensions(self):
        from PIL import Image
        image = Image.new('RGBA', (20, 10), (12, 34, 56, 255))
        canonical, metadata = raw.canonical_rgb(image)
        self.assertEqual(canonical.getpixel((0, 0)), (12, 34, 56))
        self.assertEqual(metadata['alpha_extrema'], (255, 255))
        different_shape = Image.frombytes('RGB', (10, 20), canonical.tobytes())
        self.assertNotEqual(rgb_hash(canonical), rgb_hash(different_shape))
        image.putalpha(0)
        transparent, _ = raw.canonical_rgb(image)
        self.assertEqual(transparent.getpixel((0, 0)), (255, 255, 255))

    def test_raw_workers_reject_sro_modules(self):
        blocker = raw.NoBusinessLogic()
        for name in ('app.settings', 'sro_robot.core.extract', 'ocr_compare'):
            with self.assertRaises(ImportError):
                blocker.find_spec(name)
        self.assertIsNone(blocker.find_spec('rapidocr'))


@unittest.skipUnless(os.environ.get('SRO_RAW_OCR_NATIVE') == '1', 'requires isolated raw OCR dependencies/models')
class NativeLifecycleTests(unittest.TestCase):
    def test_each_resident_request_runs_once_and_returns_fresh_text_and_pixels(self):
        from PIL import Image, ImageDraw
        config = raw.identity(SimpleNamespace(models=raw.ROOT / '.local-run/ocr-compare/models'))
        raw.PRIVATE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=raw.PRIVATE) as temp:
            folder = Path(temp).resolve(); scratch = folder / 'tmp'; scratch.mkdir()
            pages = []
            for index, digits in enumerate(('1111111111', '8888888888')):
                image = Image.new('RGB', (1000, 600), 'white')
                for row in range(6):
                    ImageDraw.Draw(image).text((50, 40 + row * 65), digits, fill='black', font_size=40)
                path = folder / f'synthetic-{index}.png'; image.save(path)
                pages.append({'id': str(index), 'path': path.name, 'size': list(image.size),
                              'sha256': file_hash(path), 'pixel_sha256': rgb_hash(image)})
            for engine in ('tesseract', 'rapidocr'):
                session = raw.Session(engine, config, scratch)
                try:
                    outputs = [session.process_document({'pages': [page]}, folder, i) for i, page in enumerate(pages)]
                    self.assertEqual([r['pages'][0]['pass_index'] for r in outputs], [1, 2])
                    self.assertEqual(len({r['pid'] for r in outputs}), 1)
                    self.assertNotEqual(outputs[0]['pages'][0]['text'], outputs[1]['pages'][0]['text'])
                    self.assertTrue(all(r['temp_removed'] for r in outputs))
                    for row, page in zip(outputs, pages, strict=True):
                        self.assertEqual(row['pages'][0]['pixel_sha256'], page['pixel_sha256'])
                        self.assertEqual(row['pages'][0]['calls']['recognize_completed'], 1)
                        self.assertGreater(row['memory_after']['request_rss_peak'], 0)
                finally:
                    session.close()
                self.assertFalse(Path(session.work.name).exists())


class TextPolicyTests(unittest.TestCase):
    def test_normalization_preserves_case_punctuation_digits_and_cyrillic(self):
        self.assertEqual(normalized('  АБВ\n 123-45  '), 'АБВ 123-45')
        self.assertNotEqual(normalized('Иванов'), normalized('иванов'))


@unittest.skipUnless(importlib.util.find_spec('rapidfuzz'), 'requires isolated text-metric dependency')
class AccuracyTests(unittest.TestCase):
    def test_cer_uses_reference_length_and_penalizes_insertions(self):
        from ocr_engine_metrics import errors, rates, span_errors
        self.assertEqual(rates(errors('abc', 'abX'))['CER'], 1 / 3)
        self.assertGreater(rates(errors('a', 'abcd'))['CER'], 1)
        span = span_errors('123', '12X3', 0, 3)
        self.assertEqual(span['character_errors'], 1)
        self.assertEqual(span['correct_characters'], 3)

    def test_manual_full_transcript_subset_requires_explicit_original_verification(self):
        corpus = {'documents': [{'pages': [{'id': 'a', 'pixel_sha256': 'pixels-a'}]},
                                {'pages': [{'id': 'b', 'pixel_sha256': 'pixels-b'}]}]}
        reference = {'schema': 1, 'corpus_sha256': 'corpus', 'method': 'manual_original_review',
                     'reviewer': 'synthetic reviewer', 'engine_outputs_hidden': False,
                     'pages': [{'id': 'a', 'pixel_sha256': 'pixels-a', 'selected': True,
                                'text': 'СИНТЕТИЧЕСКИЙ ТЕКСТ', 'verified': False, 'complete': False,
                                'seed': {'engine': 'tesseract'}},
                               {'id': 'b', 'pixel_sha256': 'pixels-b', 'selected': False}]}
        with self.assertRaisesRegex(ValueError, 'Every selected page'):
            validate_reference(reference, corpus, 'corpus')
        reference['pages'][0].update(verified=True, complete=True)
        self.assertEqual(set(validate_reference(reference, corpus, 'corpus')), {'a'})
        reference['pages'][0]['pixel_sha256'] = 'changed source'
        with self.assertRaisesRegex(ValueError, 'original raster'):
            validate_reference(reference, corpus, 'corpus')

    def test_complete_run_audit_reports_cold_lifetime_memory_and_subset_cer(self):
        from ocr_engine_metrics import audit_and_summarize
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prepared = root / 'prepared'; prepared.mkdir()
            docs = [{'index': i, 'group': group, 'pages': [{'id': str(i), 'pixel_sha256': str(i),
                    'sha256': str(i), 'size': [10, 20]}]} for i, group in enumerate(('img1', 'img2'))]
            corpus = {'documents': docs}
            raw.save(prepared / 'corpus.json', corpus)
            corpus_hash = file_hash(prepared / 'corpus.json')
            raw.save(root / 'manifest.json', {'corpus': str(prepared), 'corpus_sha256': corpus_hash, 'repeats': 3})
            index = 0
            for scenario in ('cold', 'resident'):
                for engine_index, engine in enumerate(('tesseract', 'rapidocr')):
                    for doc in docs:
                        for repeat in range(3):
                            metadata = {'native': 'tesseract 5.5.3', 'psm': 3, 'language': 'rus',
                                        'providers': [['CPUExecutionProvider']] * 3,
                                        'effective_config': {'Det': {'model_type': 'SERVER', 'limit_side_len': 736, 'limit_type': 'min'},
                                        'Global': {'max_side_len': 2000, 'return_word_box': False, 'return_single_char_box': False}}}
                            row = {'scenario': scenario, 'engine': engine, 'document_index': doc['index'],
                                   'group': doc['group'], 'repeat': repeat, 'temp_removed': True,
                                   'worker_temp_removed': True, 'pid': index + 100 if scenario == 'cold' else engine_index + 10,
                                   'ready': {'metadata': metadata, 'model_load_seconds': .2, 'memory': {'rss_peak': 180000}},
                                   'memory_after': {'request_rss_peak': 100000, 'rss_peak': 200000},
                                   'lifetime_memory': {'rss_peak': 200000}, 'rss_after_document': 90000,
                                   'request_wall_seconds': 1.0, 'cold_full_seconds': 1.5, 'resident_full_seconds': 1.0,
                                   'pages': [{**doc['pages'][0], 'input_size': [10, 20],
                                              'calls': {'recognize_attempted': 1, 'recognize_completed': 1},
                                              'seconds': {'recognition': .8}, 'text': 'abc123'}]}
                            raw.save(root / f'run-{index:03}.json', row); index += 1
            result = audit_and_summarize(root)
            self.assertEqual(result['verified_runs'], 24)
            self.assertFalse(result['quality_scored'])
            cold = result['performance']['all']['rapidocr']['cold']
            self.assertGreater(cold['max_sampled_lifetime_tree_rss_mib'], cold['max_sampled_request_tree_rss_mib'])
            reference = {'schema': 1, 'corpus_sha256': corpus_hash, 'method': 'manual_original_review',
                         'reviewer': 'synthetic', 'pages': [{'id': str(i), 'pixel_sha256': str(i), 'selected': i == 0,
                           'verified': True, 'complete': True, 'text': 'abc193', 'seed': {'engine': 'tesseract'}} for i in range(2)]}
            raw.save(root / 'reference.json', reference)
            scored = audit_and_summarize(root, root / 'reference.json')
            self.assertEqual(scored['reference']['selected_pages'], 1)
            self.assertEqual(scored['quality']['all']['rapidocr']['full_page']['CER'], 1 / 6)
            self.assertEqual(scored['quality']['all']['rapidocr']['full_page_scored_pages'], 1)


class ReferenceCustodyTests(unittest.TestCase):
    def test_atomic_save_retains_previous_reference_when_replace_fails(self):
        from ocr_engine_reference import atomic_save
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'reference.json'
            raw.save(path, {'text': 'previous synthetic draft'})
            with patch('ocr_engine_reference.os.replace', side_effect=OSError('synthetic disk error')):
                with self.assertRaises(OSError):
                    atomic_save(path, {'text': 'new synthetic draft'})
            self.assertEqual(json.loads(path.read_text())['text'], 'previous synthetic draft')
            self.assertEqual([p.name for p in Path(temporary).iterdir()], ['reference.json'])

    def test_local_hints_do_not_mark_source_or_validate_identity(self):
        from ocr_reference_drafts import hints
        text = 'ИНН 123456789012\nСНИЛС: 123-456-789 00\nАдрес: город Синтетический, дом 1\n'
        candidates = hints(text)
        self.assertTrue({'inn', 'snils', 'address'} <= {x['category'] for x in candidates})
        self.assertTrue(all('verified' not in candidate for candidate in candidates))
        self.assertTrue(all(0 <= x['start'] < x['end'] <= len(text) for x in candidates))


class PdfPolicyTests(unittest.TestCase):
    def test_pdf_uses_all_raster_pages_and_never_a_text_layer(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / 'samples/img1').mkdir(parents=True); (root / 'samples/img2').mkdir()
            for index in range(21):
                Image.new('RGB', (30, 40), 'white').save(root / f'samples/img1/synthetic-{index}.png')
            for index in range(3):
                Image.new('RGB', (30, 40), 'white').save(root / f'samples/img2/synthetic-{index}.png')
            (root / 'samples/img2/synthetic.pdf').write_bytes(b'synthetic PDF stub for raster adapter test')
            args = SimpleNamespace(output=root / 'private/prepared', pdf_dpi=300)
            def raster(*args, **kwargs):
                return [Image.new('RGB', (30, 40), 'white')]
            with (patch.object(raw, 'ROOT', root), patch.object(raw, 'PRIVATE', root / 'private'),
                  patch('pdf2image.pdfinfo_from_path', return_value={'Pages': 2}),
                  patch('pdf2image.convert_from_path', side_effect=raster) as convert):
                raw.prepare(args)
            self.assertEqual(convert.call_count, 2)
            self.assertEqual([c.kwargs['first_page'] for c in convert.call_args_list], [1, 2])
            self.assertEqual([c.kwargs['last_page'] for c in convert.call_args_list], [1, 2])
            self.assertTrue(all(c.kwargs['dpi'] == 300 for c in convert.call_args_list))
            corpus = json.loads((args.output / 'corpus.json').read_text())
            pdf = next(d for d in corpus['documents'] if d['source'].endswith('.pdf'))
            self.assertEqual(len(pdf['pages']), 2)
            self.assertTrue(all(p['pdf_raster_dpi'] == 300 for p in pdf['pages']))
