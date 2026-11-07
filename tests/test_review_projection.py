"""Synthetic coherent review cards, scoped feedback and immutable raw snapshots."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.dev.quality.court_dataset import FIELDS
from sro_robot.research import review_projection as projection
from sro_robot.research.review_store import ReviewStore, ReviewConflict, ReviewError, field_map, encoded, sha


def field(value, **extra):
    return {'value': value, 'confidence': .6, 'source_variant': 0, 'source_start': 10, 'source_end': 30, 'source_page': 1, **extra}


def person(name='Тестов Тест Тестович', inn='000000000000', address='г. Тестоград, д. 1', partial=False):
    last, first, middle = name.split()
    fields = {'fio': field(name), 'last_name': field(last), 'first_name': field(first), 'middle_name': field(middle), 'inn': field(inn), 'address': field(address), 'address_kind': field('registration')}
    return {'kind': 'application_person', 'fields': fields, 'copyable_fields': ['last_name','first_name','middle_name','inn'] + ([] if partial else ['address','address_kind'])}


def entity(kind='legal_entity', name='ООО «Синтетический образец»', inn='0000000000'):
    target = 'legal_name' if kind == 'legal_entity' else 'debtor_name'
    return {'kind': 'entity_candidate', 'fields': {'entity_kind': field(kind), target: field(name), 'inn': field(inn)}, 'copyable_fields': [target, 'inn']}


class ProjectionTests(unittest.TestCase):
    def project(self, observed, **kwargs):
        return projection.project(observed, field_map(observed), **kwargs)

    def test_maintained_person_occurrence_populates_primary_review_fields_not_raw(self):
        observed = {'review_suggestions': [person()]}
        result = self.project(observed)
        self.assertEqual(result['entity_kind'], 'person')
        self.assertEqual(result['fields']['fio_raw']['value'], 'Тестов Тест Тестович')
        self.assertEqual(result['fields']['inn']['value'], '000000000000')
        self.assertTrue(result['fields']['inn']['review_only'])
        self.assertEqual(result['fields']['inn']['source_start'], 10)
        self.assertNotIn('fio_raw', observed)
        self.assertIsNotNone(result['selected_candidate_id'])

    def test_partial_address_is_visible_but_explicitly_incomplete(self):
        result = self.project({'review_suggestions': [person(partial=True)]})
        address = result['fields']['address']
        self.assertEqual(address['value'], 'г. Тестоград, д. 1')
        self.assertTrue(address['address_partial'])
        self.assertIn('Неполный', address['warning'])

    def test_ambiguous_people_and_conflicting_same_name_identifiers_are_not_combined(self):
        for second in (person(name='Примеров Пример Примерович'), person(inn='111111111111')):
            with self.subTest(second=second['fields']['inn']['value']):
                result = self.project({'review_suggestions': [person(), second]})
                self.assertIsNone(result['selected_candidate_id'])
                self.assertTrue(result['selection_required'])
                self.assertIsNone(result['fields']['fio_raw']['value'])
                self.assertEqual(len(result['candidates']), 2)

    def test_existing_authoritative_identity_or_identifier_cannot_mix_with_candidate(self):
        for anchor in ({'inn': field('111111111111', source='debtor_role:v1')}, {'fio_raw': field('Примеров Пример Примерович', source='debtor_role:v1')}, {'snils': field('synthetic-id', source='owner-feedback')}):
            result = self.project({**anchor, 'review_suggestions': [person()]})
            self.assertIsNone(result['selected_candidate_id'])
            for name, value in anchor.items():
                self.assertEqual(result['fields'][name]['value'], value['value'])

    def test_legal_and_ip_have_canonical_types_without_person_or_inn_length_guessing(self):
        for kind, name in (('legal_entity', 'ООО «Синтетический образец»'), ('sole_proprietor', 'ИП Тестов Тест Тестович')):
            result = self.project({'review_suggestions': [entity(kind, name)]})
            self.assertEqual(result['entity_kind'], kind)
            target = 'legal_name' if kind == 'legal_entity' else 'debtor_name'
            self.assertEqual(result['fields'][target]['value'], name)
            self.assertNotIn('fio_raw', result['applicable_fields'])
            self.assertNotIn('snils', result['applicable_fields'])
        self.assertIsNone(self.project({'inn': field('0000000000')})['entity_kind'])

    def test_owner_clears_and_explicit_type_hide_incompatible_raw_anchor(self):
        observed = {'review_suggestions': [person()], 'entity_kind': field('person'), 'ogrn': field('hidden-company-id')}
        current = field_map(observed)
        current['fio_raw'] = field('', source='owner-feedback')
        current['entity_kind'] = field('', source='owner-feedback')
        result = projection.project(observed, current)
        self.assertEqual(result['fields']['fio_raw']['value'], '')
        self.assertIsNone(result['entity_kind'])
        result = self.project(observed)
        self.assertIsNotNone(result['selected_candidate_id'])
        self.assertEqual(result['fields']['ogrn']['value'], 'hidden-company-id')
        self.assertNotIn('ogrn', result['applicable_fields'])

    def test_unknown_and_malformed_confidence_remain_reviewable(self):
        self.assertEqual(self.project({})['applicable_fields'], list(projection.COMMON))
        item = person(); item['fields']['inn']['confidence'] = 'not-a-number'
        result = self.project({'review_suggestions': [item]})
        self.assertEqual(result['fields']['inn']['confidence'], 0.)

    def test_research_projection_import_never_loads_production_settings_database(self):
        code = 'import sys;import sro_robot.research.review_store;assert "app.settings" not in sys.modules;assert "app.db.base" not in sys.modules'
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, 'Local review import must not initialize production settings/database')


class ProjectionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = ReviewStore(root / 'review')
        self.asset = root / 'original.pdf'; self.asset.write_bytes(b'%PDF synthetic projection\n%%EOF\n')
        self.key = hashlib.sha256(self.asset.read_bytes()).hexdigest()
        self.capture = {'input_sha256': self.key, 'capture': {'revision': 'a' * 40, 'source_dirty': False, 'dirty_sha256': '0' * 64, 'runtime': {}, 'accepted_parser_identity': True}, 'observed_pipeline': {'review_suggestions': [person()]}, 'primary_pages': [{'page': 1}]}
        self.store.import_capture(self.asset, self.capture)

    def tearDown(self):
        self.temp.cleanup()

    def body(self, doc=None, **changes):
        doc = doc or self.store.get_document(self.key, expose=False)
        result = {'action_id': 'synthetic-review-action', 'expected_revision': doc['revision'], 'expected_snapshot_sha256': doc['snapshot_sha256'], 'reviewed_fields': doc['review_projection']['applicable_fields'], 'decision': 'approved', 'corrected_fields': {}, 'incorrect_fields': [], 'comment': ''}
        result.update(changes)
        return result

    def test_display_version_binds_old_tabs_without_mutating_raw_capture(self):
        old = self.store.get_document(self.key, expose=False)
        with patch.object(projection, 'PROJECTION_VERSION', 'owner-review-card:vNEXT'):
            changed = self.store.get_document(self.key, expose=False)
            self.assertNotEqual(changed['snapshot_sha256'], old['snapshot_sha256'])
            self.assertEqual(changed['raw_capture_sha256'], old['raw_capture_sha256'])
            with self.assertRaises(ReviewConflict):
                self.store.apply_review(self.key, self.body(old))
        self.assertEqual(self.store.stats()['pending_review'], 1)

    def test_identical_capture_in_different_store_cannot_consume_pending_action(self):
        original = self.store.get_document(self.key, expose=False)
        other = ReviewStore(Path(self.temp.name) / 'different-store')
        other.import_capture(self.asset, self.capture)
        different = other.get_document(self.key, expose=False)
        self.assertEqual(original['raw_capture_sha256'], different['raw_capture_sha256'])
        self.assertNotEqual(original['snapshot_sha256'], different['snapshot_sha256'])
        with self.assertRaises(ReviewConflict):
            other.apply_review(self.key, self.body(original))
        restarted = ReviewStore(self.store.root)
        self.assertEqual(restarted.get_document(self.key, expose=False)['snapshot_sha256'], original['snapshot_sha256'])

    def test_scope_and_immutable_projection_audit_match_values_owner_saw(self):
        old = self.store.get_document(self.key, expose=False)
        self.store.apply_review(self.key, self.body(old, decision='rejected', incorrect_fields=['address'], corrected_fields={'address': 'г. Тестоград, д. 1, кв. 4'}, comment='Synthetic apartment omission'))
        with self.store.connection() as db:
            audit = json.loads(db.execute('SELECT projection_json FROM review_projections').fetchone()[0])
            diagnostic = db.execute('SELECT original_value_json FROM diagnostics').fetchone()[0]
            raw = db.execute('SELECT capture_json FROM documents').fetchone()[0]
        self.assertEqual(audit['fields']['address']['value'], old['fields']['address']['value'])
        self.assertEqual(json.loads(diagnostic), old['fields']['address']['value'])
        self.assertNotIn('кв. 4', raw)
        corrected = self.store.get_document(self.key, expose=False)
        self.assertEqual(corrected['fields']['address']['value'], 'г. Тестоград, д. 1, кв. 4')
        self.assertEqual(corrected['fields']['address']['source'], 'owner-feedback')

    def test_empty_scope_or_hidden_correction_cannot_be_approved(self):
        with self.assertRaises(ReviewConflict):
            self.store.apply_review(self.key, self.body(reviewed_fields=[]))
        with self.assertRaises(ReviewError):
            self.store.apply_review(self.key, self.body(decision='rejected', incorrect_fields=['legal_name'], corrected_fields={'legal_name': 'hidden'}, comment='synthetic'))

    def test_approved_person_does_not_confirm_hidden_company_fields(self):
        with self.store.connection(write=True) as db:
            fields = json.loads(db.execute('SELECT effective_fields_json FROM documents').fetchone()[0])
            fields['ogrn'] = field('hidden-company-id')
            fields['entity_kind'] = field('person')
            db.execute('UPDATE documents SET effective_fields_json=?', (encoded(fields),))
        self.store.apply_review(self.key, self.body(reviewed_fields=list(FIELDS)))
        with self.store.connection() as db:
            states = json.loads(db.execute('SELECT field_states_json FROM reviews').fetchone()[0])
        self.assertEqual(states['ogrn'], 'unreviewed')
        self.assertEqual(states['fio_raw'], 'owner-confirmed-present')

    def test_old_accepted_action_replays_before_new_scope_and_hash_requirements(self):
        legacy = self.body(); legacy.pop('reviewed_fields')
        legacy['expected_snapshot_sha256'] = self.store.get_document(self.key, expose=False)['raw_capture_sha256']
        request_sha = sha(encoded({'document_id': self.key, **legacy}).encode())
        with self.store.connection(write=True) as db:
            row = db.execute('SELECT snapshot_sha256 FROM documents').fetchone()
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (legacy['action_id'], self.key, request_sha, 1, 'approved', row[0], '{}', '[]', '', 'synthetic-time', '{}', '{}', 2))
            db.execute("UPDATE documents SET status='approved',revision=2")
        self.assertTrue(self.store.apply_review(self.key, legacy)['replayed'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
