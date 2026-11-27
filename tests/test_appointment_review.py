"""Synthetic exclusive appointment review, feedback and asset contracts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from sro_robot.research.review_api import create_app
from sro_robot.research.review_projection import appointment_projection
from sro_robot.research.review_schema import APPOINTMENT_FIELDS
from sro_robot.research.review_store import ReviewStore
from tests.test_local_review import ReviewFixture


ORIGIN = 'https://review.example.test'
TOKEN = 'synthetic-appointment-password-' + 'a' * 40
EXPERT = 'synthetic-expert'
TEXT = ('ОПРЕДЕЛЕНИЕ\nДело № А99-321/2099\n25.11.2099\n'
        'Рассмотрев заявление о признании гражданина банкротом.\nОПРЕДЕЛИЛ:\n'
        'Назначить судебное заседание по рассмотрению заявления на 18.12.2099 '
        'в 10 часов 30 минут.\n')


def source_fields():
    fields = {}
    for name, source, value in [('case_number', 'А99-321/2099', 'А99-321/2099'),
                                ('doc_date', '25.11.2099', '25.11.2099'),
                                ('hearing_date', '18.12.2099', '18.12.2099'),
                                ('hearing_time', '10 часов 30 минут', '10:30')]:
        start = TEXT.index(source)
        fields[name] = dict(value=value, source='synthetic-appointment:v1', source_variant=0,
                            source_start=start, source_end=start + len(source), source_page=1)
    return fields


class AppointmentReviewTests(ReviewFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.old_snapshot = self.store.get_document(self.key, expose=False)
        self.parser = patch('sro_robot.core.appointment_extract.extract_appointment_fields', return_value=source_fields())
        self.parser.start()
        self.addCleanup(self.parser.stop)
        path = Path(self.temp.name) / 'appointment.jpg'
        path.write_bytes(b'synthetic unique photo original')
        self.appointment_key = hashlib.sha256(path.read_bytes()).hexdigest()
        captured = dict(self.captured, input_sha256=self.appointment_key, variants=[TEXT])
        self.store.import_capture(path, captured, {'expert_review_kind': 'appointment'})
        self.client = TestClient(create_app(self.store, launch_token=TOKEN, public_origin=ORIGIN,
                                           expert_identity=EXPERT), base_url=ORIGIN)
        self.addCleanup(self.client.close)
        self.client.post('/login', data={'username': EXPERT, 'password': TOKEN}, headers={'Origin': ORIGIN})

    def document(self):
        return self.store.get_document(self.appointment_key, expose=False, expert=True)

    def verdict(self, **changes):
        doc = self.document()
        return self.payload(action_id='synthetic-appointment-action', expected_revision=doc['revision'],
                            expected_snapshot_sha256=doc['snapshot_sha256'],
                            reviewed_fields=doc['review_projection']['applicable_fields'], **changes)

    def post(self, payload):
        return self.client.post(f'/api/documents/{self.appointment_key}/review', json=payload,
                                headers={'Origin': ORIGIN})

    def test_explicit_queue_is_exclusive_and_legacy_snapshot_is_unchanged(self):
        self.assertIsNone(self.client.get('/api/next').json()['document'])
        response = self.client.get('/api/next?queue=appointments').json()
        self.assertEqual(response['document']['id'], self.appointment_key)
        self.assertEqual(response['stats']['total'], 1)
        self.assertEqual(self.client.get('/api/stats').json()['total'], 1)
        self.assertEqual(self.client.get('/api/stats?queue=appointments').json()['pending_review'], 1)
        self.assertEqual(self.client.get('/api/next?queue=other').status_code, 422)
        self.assertEqual(self.store.get_document(self.key, expose=False), self.old_snapshot)
        self.assertEqual(self.store.next_document(expert=False)['id'], self.key)

    def test_only_allowed_bound_fields_reach_the_api(self):
        fields = source_fields()
        fields['court'] = dict(fields['case_number'], source_end=len(TEXT) + 1)
        fields['practitioner'] = {'value': 'Unbound synthetic participant'}
        fields['raw_text'] = {'value': 'PRIVATE_SYNTHETIC_TEXT'}
        fields['case_number']['source_excerpt'] = 'PRIVATE_SYNTHETIC_EXCERPT'
        with patch('sro_robot.core.appointment_extract.extract_appointment_fields', return_value=fields):
            doc = self.client.get(f'/api/documents/{self.appointment_key}').json()['document']
        self.assertEqual(set(doc['fields']), set(source_fields()))
        self.assertTrue(doc['review_projection']['approval_allowed'])
        self.assertNotIn('original_fields', doc)
        self.assertNotIn('observed_pipeline', doc)
        self.assertNotIn('PRIVATE_SYNTHETIC', json.dumps(doc))

    def test_optional_fields_stay_unreviewed_and_approved_feedback_replays_after_restart(self):
        payload = self.verdict()
        projection = self.document()['review_projection']
        self.assertEqual(self.post(payload).status_code, 200)
        self.assertTrue(self.post(payload).json()['replayed'])
        restarted = ReviewStore(self.root)
        self.assertTrue(restarted.apply_review(self.appointment_key, payload, actor=EXPERT, expert=True)['replayed'])
        with restarted.connection() as db:
            review = db.execute('SELECT * FROM reviews').fetchone()
            states = json.loads(review['field_states_json'])
            self.assertEqual(states['applicant'], 'unreviewed')
            self.assertEqual(states['hearing_time'], 'owner-confirmed-present')
            self.assertEqual(set(states), set(APPOINTMENT_FIELDS))
            self.assertEqual(json.loads(db.execute('SELECT projection_json FROM review_projections').fetchone()[0]), projection)
            self.assertEqual(db.execute('SELECT actor FROM review_actors').fetchone()[0], EXPERT)
        self.assertEqual(self.client.get('/api/stats?queue=appointments').json()['approved'], 1)
        self.assertEqual(self.store.get_document(self.key, expose=False), self.old_snapshot)

    def test_empty_and_partial_results_remain_rejectable_but_cannot_be_approved(self):
        for fields in ({}, {'case_number': source_fields()['case_number']}):
            with self.subTest(fields=list(fields)), patch('sro_robot.core.appointment_extract.extract_appointment_fields', return_value=fields):
                doc = self.document()
                self.assertFalse(doc['review_projection']['approval_allowed'])
                self.assertEqual(doc['review_projection']['applicable_fields'], list(fields))
                self.assertEqual(self.post(self.verdict()).status_code, 409)
                rejected = self.verdict(decision='rejected', comment='Synthetic missing hearing evidence')
                # A mismatched scope cannot confirm fields absent from the exact snapshot.
                wrong_scope = dict(rejected, reviewed_fields=['hearing_date'])
                self.assertEqual(self.post(wrong_scope).status_code, 409)
        with patch('sro_robot.core.appointment_extract.extract_appointment_fields', return_value={}):
            payload = self.verdict(decision='rejected', comment='Synthetic no extracted fields')
            self.assertEqual(payload['reviewed_fields'], [])
            self.assertEqual(self.post(dict(payload, comment='')).status_code, 422)
            self.assertEqual(self.post(payload).status_code, 200)
            self.assertTrue(self.post(payload).json()['replayed'])
            with self.store.connection() as db:
                row = db.execute('SELECT * FROM diagnostics').fetchone()
                self.assertEqual(row['field_name'], '_document')
                self.assertEqual(row['reason'], payload['comment'])

    def test_assets_dispatch_by_metadata_and_remain_authenticated(self):
        endpoint = f'/api/documents/{self.appointment_key}'
        self.assertEqual(self.client.get(endpoint + '/original').content, b'synthetic unique photo original')
        with patch.object(self.store, 'preview', return_value=b'synthetic-png'):
            self.assertEqual(self.client.get(endpoint + '/preview/1').content, b'synthetic-png')
        self.client.cookies.clear()
        self.assertEqual(self.client.get(endpoint + '/original').status_code, 401)
        self.assertEqual(self.client.get(endpoint + '/preview/1').status_code, 401)

    def test_duplicate_import_cannot_retag_or_replace_old_feedback(self):
        self.store.enqueue_asset(self.original, {'expert_review_kind': 'appointment'})
        self.assertEqual(self.store.get_document(self.key, expose=False), self.old_snapshot)
        self.store.apply_review(self.key, self.payload(decision='rejected', comment='Synthetic old verdict'))
        with self.store.connection() as db:
            before = tuple(db.execute('SELECT * FROM reviews').fetchone())
        self.store.enqueue_asset(self.original, {'expert_review_kind': 'appointment'})
        with self.store.connection() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM reviews').fetchone()), before)
            metadata = json.loads(db.execute('SELECT metadata_json FROM documents WHERE id=?', (self.key,)).fetchone()[0])
            self.assertNotIn('expert_review_kind', metadata)
        self.assertEqual(self.store.stats(expert=True, queue='appointments')['total'], 1)

    def test_stale_snapshot_and_concurrent_verdict_cannot_replace_feedback(self):
        payload = self.verdict(decision='rejected', comment='Synthetic reason')
        changed = deepcopy(source_fields())
        changed.pop('doc_date')
        with patch('sro_robot.core.appointment_extract.extract_appointment_fields', return_value=changed):
            self.assertEqual(self.post(payload).status_code, 409)
        self.assertEqual(self.post(payload).status_code, 200)
        self.assertEqual(self.post(dict(payload, action_id='synthetic-other-tab')).status_code, 409)
        self.assertEqual(self.post(dict(payload, comment='Changed reason')).status_code, 409)

    def test_local_dispatch_also_uses_verdict_mode_and_matching_retry_counters(self):
        payload = self.verdict()
        first = self.store.apply_review(self.appointment_key, payload)
        replay = self.store.apply_review(self.appointment_key, payload)
        self.assertIsNone(first['document'])
        self.assertIsNone(replay['document'])
        self.assertEqual(first['stats']['approved'], 1)
        self.assertEqual(replay['stats'], first['stats'])
        self.assertEqual(self.store.stats()['approved'], 0)

    def test_actual_parser_practitioner_only_verdict_is_persisted(self):
        self.parser.stop()
        text = TEXT[:TEXT.index('Назначить судебное заседание')] + 'Утвердить финансовым управляющим Макетова Петра Сергеевича.\n'
        path = Path(self.temp.name) / 'synthetic-practitioner.jpg'
        path.write_bytes(b'synthetic unique practitioner photo')
        self.appointment_key = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.import_capture(path, dict(self.captured, input_sha256=self.appointment_key, variants=[text]),
                                  {'expert_review_kind': 'appointment'})
        self.assertEqual(self.document()['review_projection']['appointment_event'], 'practitioner')
        self.assertEqual(self.post(self.verdict()).status_code, 200)
        with self.store.connection() as db:
            states = json.loads(db.execute('SELECT field_states_json FROM reviews').fetchone()[0])
        self.assertEqual(states['practitioner'], 'owner-confirmed-present')
        self.assertEqual(states['doc_date'], 'owner-confirmed-present')
        self.assertEqual(states['hearing_date'], 'unreviewed')


class AppointmentProjectionTests(unittest.TestCase):
    def test_real_parser_contract_on_synthetic_order(self):
        projection = appointment_projection({'variants': [TEXT]})
        self.assertTrue(projection['approval_allowed'])
        self.assertEqual(projection['fields']['hearing_time']['value'], '10:30')
        self.assertEqual(projection['fields']['hearing_date']['value'], '18.12.2099')
        self.assertEqual(projection['version'], 'expert-appointment:v1')

    def test_explicit_practitioner_appointment_has_its_own_core_requirements(self):
        text = TEXT[:TEXT.index('Назначить судебное заседание')] + 'Утвердить финансовым управляющим Макетова Петра Сергеевича.\n'
        projection = appointment_projection({'variants': [text]})
        self.assertTrue(projection['approval_allowed'])
        self.assertEqual(projection['appointment_event'], 'practitioner')
        self.assertIn('practitioner', projection['fields'])
        self.assertNotIn('hearing_date', projection['fields'])
        self.assertNotIn('hearing_time', projection['fields'])
        fields = deepcopy(projection['fields'])
        fields.pop('doc_date')
        with patch('sro_robot.core.appointment_extract.extract_appointment_fields', return_value=fields):
            incomplete = appointment_projection({'variants': [text]})
        self.assertFalse(incomplete['approval_allowed'])
        self.assertEqual(incomplete['missing_required_fields'], ['doc_date'])

    def test_partial_hearing_with_practitioner_cannot_use_practitioner_approval(self):
        for text in (TEXT.replace('в 10 часов 30 минут', ''),
                     TEXT.replace('на 18.12.2099 в 10 часов 30 минут', 'в установленный срок')):
            with self.subTest(text=text):
                text += 'Утвердить финансовым управляющим Макетова Петра Сергеевича.\n'
                projection = appointment_projection({'variants': [text]})
                self.assertIn('practitioner', projection['fields'])
                self.assertEqual(projection['appointment_event'], 'hearing')
                self.assertFalse(projection['approval_allowed'])
                self.assertIn('hearing_time', projection['missing_required_fields'])
