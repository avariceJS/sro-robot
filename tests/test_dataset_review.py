"""Offline review rendering uses only generated synthetic assets."""
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from scripts.dev.quality import dataset_review as review
from scripts.dev.quality.court_dataset import identity, reference_template, write_json


def decoded_payload(page):
    text = re.search(r'<script type="application/json" id="payload">(.*?)</script>', page, re.S).group(1)
    return json.loads(text)


class DatasetReviewTests(unittest.TestCase):
    def corpus(self, root, count=1, pages=2):
        docs = []
        for number in range(count):
            stream = BytesIO(); Image.new('RGB', (12 + number, 16), (number, 40, 90)).save(stream, format='PNG')
            data = stream.getvalue(); sha = hashlib.sha256(data).hexdigest()
            (root / 'objects').mkdir(exist_ok=True)
            (root / 'objects' / (sha + '.png')).write_bytes(data)
            directory = root / 'previews' / sha; directory.mkdir(parents=True)
            for page in range(1, pages + 1):
                (directory / f'{page}.png').write_bytes(data)
            docs.append({'id': sha, 'sha256': sha, 'extension': '.png', 'pages': [
                {'sha256': sha, 'dhash': '0' * 16, 'aspect': 1} for _ in range(pages)],
                'case_key': '', 'duplicate_cluster': '', 'previously_seen': True, 'source': 'existing-local', 'medium': 'unknown',
                'strata': {'court': 'unknown', 'year': 'unknown', 'act_type': 'unknown'},
                'case_reviewed': False, 'duplicate_reviewed': False})
        manifest = {'schema': 1, 'documents': docs}
        split = {'schema': 1, 'manifest_sha256': identity(manifest),
                 'assignments': {d['id']: 'development' for d in docs}}
        write_json(root / 'manifest.json', manifest); write_json(root / 'split.json', split)
        return manifest, split

    def add_gold(self, root, doc, malicious=False):
        ref = reference_template(doc)
        ref.update(reviewer='Human A', second_reviewer='Human B', original_inspected=True)
        captured = {'input_sha256': doc['sha256'], 'observed_pipeline': {
            'fio_raw': {'value': '</script><img src=x onerror=alert(1)>' if malicious else 'SYNTHETIC_PREDICTION'}},
            'variants': ['PRIVATE_WEAK_TEXT']}
        write_json(root / 'references' / (doc['id'] + '.json'), ref)
        write_json(root / 'captures' / (doc['id'] + '.json'), captured)
        return ref, captured

    def test_blind_form_has_all_pages_but_zero_automated_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); manifest, split = self.corpus(root)
            doc = manifest['documents'][0]
            write_json(root / 'captures' / (doc['id'] + '.json'), {
                'variants': ['PRIVATE_WEAK_TEXT'], 'observed_pipeline': {'fio_raw': {'value': 'SYNTHETIC_PREDICTION'}}})
            with patch.object(review, '_read_json', side_effect=AssertionError('Blind mode opened observations')):
                summary = review.make_review(root)
            page = (root / 'review.html').read_text(); payload = decoded_payload(page)
            self.assertEqual(summary['documents'], 1)
            self.assertEqual(len(payload['documents'][0]['pages']), 2)
            self.assertNotIn('SYNTHETIC_PREDICTION', page)
            self.assertNotIn('PRIVATE_WEAK_TEXT', page)
            self.assertNotIn('observed_pipeline', payload['documents'][0])
            self.assertEqual(payload['documents'][0]['record'], reference_template(doc))
            self.assertEqual(payload['manifest_sha256'], identity(manifest))
            self.assertEqual(payload['split_sha256'], identity(split))
            self.assertEqual(payload['kind'], 'references')
            self.assertEqual(payload['documents'][0]['original'], 'objects/' + doc['id'] + '.png')
            self.assertEqual((root / 'review.html').stat().st_mode & 0o777, 0o600)

    def test_audit_exposes_only_exact_frozen_reference_and_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); manifest, _ = self.corpus(root)
            doc = manifest['documents'][0]; ref, captured = self.add_gold(root, doc)
            summary = review.make_review(root, audit=True)
            payload = decoded_payload((root / 'review.html').read_text()); item = payload['documents'][0]
            self.assertEqual(summary['mode'], 'audits')
            self.assertEqual(item['reference'], ref)
            self.assertEqual(item['observed_pipeline'], captured['observed_pipeline'])
            self.assertNotIn('PRIVATE_WEAK_TEXT', (root / 'review.html').read_text())
            record = item['record']
            self.assertEqual(record['capture_sha256'], identity(captured))
            self.assertEqual(record['reference_sha256'], identity(ref))
            self.assertEqual(record['document_id'], doc['id'])
            self.assertFalse(record['original_inspected'])
            self.assertEqual(record['reviewer'], '')
            self.assertTrue(all(v == {'owner': 'unreviewed', 'source': 'unreviewed', 'error_layer': None} for v in record['fields'].values()))

    def test_private_strings_cannot_close_data_script_or_insert_markup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); manifest, _ = self.corpus(root)
            self.add_gold(root, manifest['documents'][0], malicious=True)
            review.make_review(root, audit=True)
            page = (root / 'review.html').read_text()
            self.assertNotIn('</script><img', page)
            self.assertIn('\\u003c/script\\u003e', page)
            self.assertEqual(decoded_payload(page)['documents'][0]['observed_pipeline']['fio_raw']['value'], '</script><img src=x onerror=alert(1)>')
            self.assertEqual(page.count('</script>'), 2)
            self.assertNotIn('innerHTML', page)

    def test_csp_hashes_match_and_network_is_denied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.corpus(root)
            review.make_review(root); page = (root / 'review.html').read_text()
            self.assertIn('connect-src &#x27;none&#x27;', page)
            self.assertIn('default-src &#x27;none&#x27;', page)
            for source in (review.STYLE, review.SCRIPT):
                fingerprint = base64.b64encode(hashlib.sha256(source.encode()).digest()).decode()
                self.assertIn('sha256-' + fingerprint, page)
            self.assertNotIn('unsafe-inline', page)
            self.assertNotRegex(page, r'(?:https?://|<script[^>]+src=|fetch\(|XMLHttpRequest|WebSocket)')
            self.assertIn('new Blob(', page)
            self.assertIn('schema:1,kind:payload.kind', page)
            self.assertIn('v.replace(/\\\\f/g,\'\\f\')', page)

    def test_limit_and_existing_references_do_not_contaminate_blind_form(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); manifest, _ = self.corpus(root, count=3)
            self.add_gold(root, manifest['documents'][0])
            result = review.make_review(root, limit=1)
            payload = decoded_payload((root / 'review.html').read_text())
            self.assertEqual(result['documents'], 1)
            self.assertEqual(payload['documents'][0]['id'], manifest['documents'][1]['id'])
            for bad in (0, -1, 21, True):
                with self.assertRaises(ValueError):
                    review.make_review(root, limit=bad)

    def test_audit_requires_two_distinct_original_readers_and_bound_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); manifest, _ = self.corpus(root); doc = manifest['documents'][0]
            ref, captured = self.add_gold(root, doc)
            ref['second_reviewer'] = ' human a '
            write_json(root / 'references' / (doc['id'] + '.json'), ref)
            with self.assertRaisesRegex(ValueError, 'independent'):
                review.make_review(root, audit=True)
            ref['second_reviewer'] = 'Human B'
            write_json(root / 'references' / (doc['id'] + '.json'), ref)
            captured['input_sha256'] = '0' * 64
            write_json(root / 'captures' / (doc['id'] + '.json'), captured)
            with self.assertRaisesRegex(ValueError, 'exact capture'):
                review.make_review(root, audit=True)

    def test_preview_symlink_escape_rejected_without_overwriting_previous_form(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as other:
            root = Path(temporary); manifest, _ = self.corpus(root); doc = manifest['documents'][0]
            preview = root / 'previews' / doc['id'] / '1.png'
            external = Path(other) / 'synthetic.png'; external.write_bytes(preview.read_bytes())
            preview.unlink(); preview.symlink_to(external)
            (root / 'review.html').write_text('prior form')
            with self.assertRaisesRegex(ValueError, 'boundary'):
                review.make_review(root)
            self.assertEqual((root / 'review.html').read_text(), 'prior form')


if __name__ == '__main__':
    unittest.main()
