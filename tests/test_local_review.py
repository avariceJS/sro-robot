"""Synthetic local owner-feedback persistence and HTTP boundaries."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sro_robot.research.review_store import ReviewStore, ReviewError, ReviewConflict
from sro_robot.research.review_api import create_app
from sro_robot.research.review_schema import FIELDS


class ReviewFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'private'
        self.store = ReviewStore(self.root)
        self.original = Path(self.temp.name) / 'synthetic.pdf'
        self.original.write_bytes(b'%PDF-1.4\nsynthetic test document\n%%EOF\n')
        self.key = hashlib.sha256(self.original.read_bytes()).hexdigest()
        self.captured = {'input_sha256': self.key, 'observed_pipeline': {'case_number': {'value': 'SYNTHETIC-CASE', 'warning': None}, 'address': {'value': None}},
                         'capture': {'accepted_parser_identity': True, 'revision': 'synthetic-source', 'source_dirty': False, 'dirty_sha256': 'synthetic-dirty', 'runtime': {}},
                         'primary_pages': [{'page': 1, 'origin': 'embedded-text-weak'}]}
        self.store.import_capture(self.original, self.captured, {'case_key': 'synthetic-private-case'})
        self.snapshot_sha = self.store.get_document(self.key, expose=False)['snapshot_sha256']

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, **changes):
        value = {'action_id': 'synthetic-action-1', 'expected_revision': 1, 'expected_snapshot_sha256': self.snapshot_sha, 'reviewed_fields': list(FIELDS), 'decision': 'approved', 'corrected_fields': {}, 'incorrect_fields': [], 'comment': ''}
        value.update(changes)
        return value


class ReviewStoreTests(ReviewFixture, unittest.TestCase):
    def test_approval_is_durable_idempotent_and_empty_fields_are_not_absence(self):
        result = self.store.apply_review(self.key, self.payload())
        self.assertFalse(result['replayed'])
        self.assertEqual(result['document']['status'], 'approved')
        restarted = ReviewStore(self.root)
        self.assertTrue(restarted.apply_review(self.key, self.payload())['replayed'])
        self.assertEqual(restarted.stats()['approved'], 1)
        self.assertIsNone(restarted.next_document())
        with restarted.connection() as db:
            review = db.execute('SELECT field_states_json FROM reviews').fetchone()
        states = json.loads(review[0])
        self.assertEqual(states['address'], 'unreviewed')
        self.assertEqual(states['case_number'], 'owner-confirmed-present')
        self.assertFalse(restarted.diagnostics_summary()['owner_feedback_is_gold'])

    def test_rejection_retains_original_correction_reason_identity_and_diagnostic(self):
        self.captured['observed_pipeline']['address'].update(source='ocr', source_start=5, source_end=10, source_page=2, source_boxes=[{'x': 1}])
        with self.store.connection(write=True) as db:
            snapshot = json.dumps(self.captured, sort_keys=True, separators=(',', ':'))
            db.execute('UPDATE documents SET capture_json=?,snapshot_sha256=? WHERE id=?', (snapshot, hashlib.sha256(snapshot.encode()).hexdigest(), self.key))
        self.snapshot_sha = self.store.get_document(self.key, expose=False)['snapshot_sha256']
        comment = 'Synthetic incomplete unit'
        result = self.store.apply_review(self.key, self.payload(decision='rejected', incorrect_fields=['address'], corrected_fields={'address': 'Synthetic address, unit 4'}, comment=comment))
        self.assertEqual(result['document']['fields']['address']['value'], 'Synthetic address, unit 4')
        self.assertIsNone(result['document']['original_fields']['address']['value'])
        self.assertEqual(result['document']['original_fields']['address']['source_start'], 5)
        self.assertEqual(result['document']['fields']['address']['source'], 'owner-feedback')
        self.assertIsNone(result['document']['fields']['address']['source_start'])
        self.assertIsNone(result['document']['fields']['address']['source_page'])
        self.assertEqual(result['document']['fields']['address']['source_boxes'], [])
        self.assertEqual(result['document']['parser_identity']['revision'], 'synthetic-source')
        with self.store.connection() as db:
            diagnostic = db.execute('SELECT * FROM diagnostics').fetchone()
            self.assertEqual(diagnostic['reason'], comment)
            self.assertEqual(json.loads(diagnostic['corrected_value_json']), 'Synthetic address, unit 4')
        summary = self.store.diagnostics_summary()
        self.assertNotIn(comment, json.dumps(summary))
        self.assertNotIn(self.key, json.dumps(summary))
        self.assertEqual(summary['diagnostics'][0]['field'], 'address')


    def test_concurrent_tabs_commit_one_action_only(self):
        def decide(index):
            try:
                self.store.apply_review(self.key, self.payload(action_id='synthetic-action-' + str(index)))
                return 'accepted'
            except ReviewConflict:
                return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(decide, [1, 2]))
        self.assertEqual(sorted(outcomes), ['accepted', 'conflict'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 1)

    def test_aggregate_categories_never_include_source_values_or_comment(self):
        value, reason = 'PRIVATE_SYNTHETIC_ADDRESS, кв. 728', 'PRIVATE_SYNTHETIC_REASON'
        self.store.apply_review(self.key, self.payload(decision='rejected', incorrect_fields=['address'], corrected_fields={'address': value}, comment=reason))
        result = self.store.diagnostics_summary()
        self.assertEqual(result['diagnostics'][0]['category'], 'missing_address_unit')
        self.assertEqual(result['category_qualification'], 'heuristic-owner-feedback-not-adjudicated')
        for private in (value, reason, self.key, '728'):
            self.assertNotIn(private, json.dumps(result))

    def test_action_id_reuse_with_changed_value_conflicts_and_import_cannot_erase_review(self):
        self.store.apply_review(self.key, self.payload())
        with self.assertRaises(ReviewConflict):
            self.store.apply_review(self.key, self.payload(comment='changed'))
        changed = json.loads(json.dumps(self.captured)); changed['observed_pipeline']['case_number']['value'] = 'NEW RESULT'
        self.assertFalse(self.store.import_capture(self.original, changed)['inserted'])
        result = self.store.get_document(self.key, expose=False)
        self.assertEqual(result['status'], 'approved')
        self.assertEqual(result['original_fields']['case_number']['value'], 'SYNTHETIC-CASE')

    def test_review_requires_exact_retained_snapshot_even_at_matching_revision(self):
        for value in (None, '0' * 64):
            with self.subTest(value=value), self.assertRaisesRegex(ReviewConflict, 'snapshot_identity_conflict'):
                self.store.apply_review(self.key, self.payload(expected_snapshot_sha256=value))
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 0)
        self.assertEqual(self.store.stats()['pending_review'], 1)

    def test_import_rejects_wrong_asset_or_invalid_field_values(self):
        changed = dict(self.captured, input_sha256='0' * 64)
        with self.assertRaises(ReviewError):
            self.store.import_capture(self.original, changed)

    def test_snapshot_drift_prevents_read_and_review_transaction(self):
        with self.store.connection(write=True) as db:
            db.execute('UPDATE documents SET capture_json=? WHERE id=?', ('{}', self.key))
        with self.assertRaisesRegex(ReviewError, 'snapshot_integrity_failed'):
            self.store.get_document(self.key)
        with self.assertRaisesRegex(ReviewError, 'snapshot_integrity_failed'):
            self.store.apply_review(self.key, self.payload())
        self.assertEqual(self.store.stats()['approved'], 0)
        changed = dict(self.captured, observed_pipeline={'address': {'value': ['invalid']}})
        with self.assertRaises(ReviewError):
            self.store.import_capture(self.original, changed)

    def test_original_is_owned_and_changed_source_or_symlink_cannot_change_it(self):
        self.original.write_bytes(b'changed source')
        owned, media = self.store.asset(self.key)
        self.assertEqual(hashlib.sha256(owned.read_bytes()).hexdigest(), self.key)
        self.assertEqual(owned.stat().st_mode & 0o777, 0o600)
        owned.unlink(); owned.symlink_to(self.original)
        with self.assertRaises(ReviewError):
            self.store.asset(self.key)

    def test_exposure_ledger_keeps_private_case_binding_and_worker_counts(self):
        self.assertEqual(self.store.exposure_records(), [])
        self.store.get_document(self.key)
        self.assertEqual(self.store.exposure_records()[0]['metadata']['case_key'], 'synthetic-private-case')
        self.store.set_download_status(10)
        self.assertEqual(self.store.stats()['downloading'], 10)
        self.assertEqual(self.store.stats()['pending_review'], 1)
        second = Path(self.temp.name) / 'second.png'; second.write_bytes(b'synthetic png')
        key = self.store.enqueue_asset(second)
        self.assertEqual(self.store.unprocessed()[0]['id'], key)
        self.store.set_status(key, 'error', 'capture_failed')
        self.assertEqual(self.store.unprocessed(), [])
        self.assertEqual(self.store.stats()['error'], 1)

    def test_skip_advances_without_approving_or_hiding_single_pending_item(self):
        self.assertIsNone(self.store.next_document(after=self.key))
        self.assertEqual(self.store.stats()['pending_review'], 1)
        self.assertEqual(self.store.next_document()['id'], self.key)

    def test_multipage_photo_preview_is_bounded_private_and_preserves_original(self):
        from PIL import Image
        source = Path(self.temp.name) / 'synthetic.tiff'
        first = Image.new('RGB', (40, 50), 'white')
        first.save(source, save_all=True, append_images=[Image.new('RGB', (40, 50), 'gray')])
        key = hashlib.sha256(source.read_bytes()).hexdigest()
        captured = dict(self.captured, input_sha256=key, primary_pages=[{'page': 1}, {'page': 2}])
        self.store.import_capture(source, captured)
        for page in (1, 2):
            preview = self.store.preview(key, page)
            self.assertTrue(preview.read_bytes().startswith(b'\x89PNG'))
            self.assertEqual(preview.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ReviewError):
            self.store.preview(key, 3)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), key)


class ReviewApiTests(ReviewFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.store, launch_token='synthetic-local-launch-token-0000000000000000', port=8767)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8767')

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def login(self):
        result = self.client.get('/session', params={'token': 'synthetic-local-launch-token-0000000000000000'}, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers['location'], '/')
        self.assertIn('HttpOnly', result.headers['set-cookie'])
        self.assertIn('SameSite=strict', result.headers['set-cookie'])

    def test_unauthenticated_host_origin_and_request_body_boundaries(self):
        self.assertEqual(self.client.get('/api/stats').status_code, 401)
        self.login()
        self.assertEqual(self.client.get('/api/stats', headers={'Host': 'foreign.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/documents/' + self.key + '/review', json=self.payload()).status_code, 403)
        self.assertEqual(self.client.post('/api/documents/' + self.key + '/review', json=self.payload(), headers={'Origin': 'https://foreign.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/documents/' + self.key + '/review', content=b'{', headers={'Origin': 'http://127.0.0.1:8767'}).status_code, 422)

    def test_original_endpoint_marks_exposure_and_feedback_counters_update(self):
        self.login()
        response = self.client.get('/api/documents/' + self.key + '/original')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.original.read_bytes())
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(len(self.store.exposure_records()), 1)
        result = self.client.post('/api/documents/' + self.key + '/review', json=self.payload(), headers={'Origin': 'http://127.0.0.1:8767'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['stats']['approved'], 1)
        self.assertIsNone(self.client.get('/api/next').json()['document'])

    def test_original_before_capture_still_records_material_exposure(self):
        other = Path(self.temp.name) / 'unprocessed.pdf'
        other.write_bytes(b'%PDF-1.4\nsynthetic unprocessed\n%%EOF\n')
        key = self.store.enqueue_asset(other)
        self.login()
        self.assertEqual(self.client.get('/api/documents/' + key + '/original').status_code, 200)
        self.assertIn(key, {row['document_id'] for row in self.store.exposure_records()})

    def test_session_cookie_survives_service_restart_and_errors_do_not_echo_values(self):
        self.login()
        cookies = self.client.cookies
        restarted = TestClient(create_app(ReviewStore(self.root), launch_token='synthetic-local-launch-token-0000000000000000', port=8767), base_url='http://127.0.0.1:8767')
        restarted.cookies.update(cookies)
        self.assertEqual(restarted.get('/api/stats').status_code, 200)
        result = restarted.post('/api/documents/' + self.key + '/review', json=self.payload(corrected_fields={'PRIVATE_SYNTHETIC': 'PRIVATE_VALUE'}), headers={'Origin': 'http://127.0.0.1:8767'})
        self.assertEqual(result.status_code, 422)
        self.assertNotIn('PRIVATE', result.text)
        restarted.close()


if __name__ == '__main__':
    unittest.main()
