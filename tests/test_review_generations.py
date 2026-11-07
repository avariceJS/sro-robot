"""Synthetic saved-reading generations, party roles and twenty-field owner review."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sro_robot.core.models import DebtorCard, Field
from sro_robot.research.review_projection import project, applicable_fields
from sro_robot.research.review_schema import FIELDS, NAME_PARTS, EVALUATION_FIELDS
from sro_robot.research.review_store import ReviewStore, ReviewConflict, ReviewError, field_map
from sro_robot.research.review_reprocess import reprocess_saved_variants
from tests.test_review_projection import person, entity, field


def party(role, name, ogrn):
    item = entity(name=name)
    item.update(kind='entity_participant', role=role, role_location={'source_variant': 0, 'source_start': 0, 'source_end': 8, 'source_page': 1})
    item['fields']['ogrn'] = field(ogrn)
    item['copyable_fields'].append('ogrn')
    return item


class PartyProjectionTests(unittest.TestCase):
    def test_corporate_plaintiff_does_not_erase_coherent_prior_person_and_address(self):
        individual = person(address='г. Тестоград, ул. Примерная, д. 1, кв. 4')
        company = party('plaintiff', 'ООО «Синтетический кредитор»', '0000000000000')
        for type_hint in ({}, {'entity_kind': field('legal_entity', source='debtor_entity_candidate:v1')}):
            observed = {**type_hint, 'review_suggestions': [individual, company]}
            result = project(observed, field_map(observed))
            self.assertEqual(result['entity_kind'], 'person')
            self.assertEqual(result['fields']['first_name']['value'], 'Тест')
            self.assertEqual(result['fields']['address']['value'], individual['fields']['address']['value'])
            self.assertIsNone(result['fields']['ogrn']['value'])
            self.assertEqual(len(result['candidates']), 2)
            self.assertEqual(result['candidates'][1]['role'], 'plaintiff')
            self.assertFalse(result['subject_is_debtor'])

    def test_corporate_defendant_remains_selected_with_person_option_retained(self):
        observed = {'review_suggestions': [person(), party('defendant', 'ООО «Синтетический ответчик»', '0000000000000')]}
        result = project(observed, field_map(observed))
        self.assertEqual(result['entity_kind'], 'legal_entity')
        self.assertEqual(result['subject_role'], 'defendant')
        self.assertEqual(result['fields']['ogrn']['value'], '0000000000000')
        self.assertIsNone(result['fields']['fio_raw']['value'])
        self.assertEqual(result['candidates'][0]['entity_kind'], 'person')

    def test_person_preference_cannot_resolve_conflicting_people_or_override_owner_type(self):
        items = [person(), person(name='Примеров Пример Примерович'), party('applicant', 'ООО «Синтетический кредитор»', '0000000000000')]
        observed = {'entity_kind': field('legal_entity', source='debtor_entity_candidate:v1'), 'review_suggestions': items}
        result = project(observed, field_map(observed))
        self.assertIsNone(result['selected_candidate_id'])
        self.assertIsNone(result['entity_kind'])
        self.assertIsNone(result['fields']['last_name']['value'])
        self.assertTrue(result['selection_required'])
        observed['entity_kind'] = field('legal_entity', source='owner-feedback')
        chosen = project(observed, field_map(observed))
        self.assertEqual(chosen['entity_kind'], 'legal_entity')
        self.assertEqual(chosen['subject_role'], 'applicant')
        self.assertIsNone(chosen['fields']['last_name']['value'])

    def test_unique_defendant_is_review_subject_not_declared_debtor(self):
        observed = {'review_suggestions': [party('plaintiff', 'ООО «Синтетический истец»', '0000000000000'), party('defendant', 'АО «Синтетический ответчик»', '1111111111111')]}
        result = project(observed, field_map(observed))
        self.assertEqual(result['subject_role'], 'defendant')
        self.assertFalse(result['subject_is_debtor'])
        self.assertEqual(result['fields']['legal_name']['value'], 'АО «Синтетический ответчик»')
        self.assertEqual(result['fields']['ogrn']['value'], '1111111111111')
        self.assertEqual(result['entity_kind'], 'legal_entity')
        self.assertIn('Роль должника не установлена', result['subject_warning'])
        self.assertNotIn('legal_name', observed)

    def test_unique_plaintiff_may_be_reviewed_without_inventing_debtor_role(self):
        observed = {'review_suggestions': [party('plaintiff', 'ООО «Синтетический истец»', '0000000000000')]}
        result = project(observed, field_map(observed))
        self.assertEqual(result['subject_role'], 'plaintiff')
        self.assertFalse(result['subject_is_debtor'])
        self.assertTrue(result['fields']['legal_name']['review_only'])

    def test_two_defendants_require_choice_but_known_legal_type_is_shown(self):
        observed = {'review_suggestions': [party('defendant', 'ООО «Первый синтетический»', '0000000000000'), party('defendant', 'ООО «Второй синтетический»', '1111111111111')]}
        result = project(observed, field_map(observed))
        self.assertIsNone(result['selected_candidate_id'])
        self.assertTrue(result['selection_required'])
        self.assertEqual(result['entity_kind'], 'legal_entity')
        self.assertIsNone(result['fields']['legal_name']['value'])
        self.assertIsNone(result['subject_role'])

    def test_review_names_expand_to_twenty_without_changing_evaluation_seventeen(self):
        self.assertEqual(len(EVALUATION_FIELDS), 17)
        self.assertEqual(len(FIELDS), 20)
        self.assertEqual(len(applicable_fields('person')), 12)
        self.assertEqual(len(applicable_fields('legal_entity')), 13)
        self.assertEqual(len(applicable_fields('sole_proprietor')), 10)
        observed = {'review_suggestions': [person()]}
        result = project(observed, field_map(observed))
        self.assertEqual([result['fields'][name]['value'] for name in NAME_PARTS], ['Тестов', 'Тест', 'Тестович'])


class CaptureGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.temp.name) / 'queue')
        self.asset = Path(self.temp.name) / 'synthetic.pdf'; self.asset.write_bytes(b'%PDF synthetic lineage\n%%EOF\n')
        self.key = hashlib.sha256(self.asset.read_bytes()).hexdigest()
        self.capture = {'input_sha256': self.key, 'variants': ['Synthetic saved reading'], 'observed_pipeline': {'review_suggestions': [person()]}, 'capture': {'accepted_parser_identity': True, 'revision': 'a' * 40, 'source_dirty': False, 'dirty_sha256': '0' * 64, 'runtime': {}, 'completed_ocr': 7, 'seconds': 1.2}, 'primary_pages': [{'page': 1, 'origin': 'ocr', 'text': 'Synthetic saved reading'}]}
        self.store.import_capture(self.asset, self.capture)

    def tearDown(self):
        self.temp.cleanup()

    def body(self, doc=None, **changes):
        doc = doc or self.store.get_document(self.key, expose=False)
        result = {'action_id': 'synthetic-generation-action', 'expected_revision': doc['revision'], 'expected_snapshot_sha256': doc['snapshot_sha256'], 'decision': 'approved', 'reviewed_fields': doc['review_projection']['applicable_fields'], 'corrected_fields': {}, 'incorrect_fields': [], 'comment': ''}
        result.update(changes)
        return result

    def updated(self):
        result = deepcopy(self.capture)
        result['capture']['revision'] = 'b' * 40
        result['observed_pipeline'] = {'review_suggestions': [party('defendant', 'ООО «Новая синтетическая сторона»', '0000000000000')]}
        return result

    def test_append_activates_pending_but_preserves_original_and_stales_old_tab(self):
        old = self.store.get_document(self.key, expose=False)
        with self.store.connection() as db:
            original_bytes = db.execute('SELECT capture_json FROM documents').fetchone()[0]
        result = self.store.append_capture_generation(self.key, self.updated(), expected_raw_snapshot_sha256=old['raw_capture_sha256'])
        self.assertTrue(result['activated'])
        current = self.store.get_document(self.key, expose=False)
        self.assertEqual(current['revision'], old['revision'] + 1)
        self.assertNotEqual(current['raw_capture_sha256'], old['raw_capture_sha256'])
        self.assertEqual(current['original_capture_sha256'], old['raw_capture_sha256'])
        self.assertEqual(current['review_projection']['subject_role'], 'defendant')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT capture_json FROM documents').fetchone()[0], original_bytes)
        self.assertEqual(len(self.store.capture_lineage(self.key)['generations']), 2)
        with self.assertRaises(ReviewConflict):
            self.store.apply_review(self.key, self.body(old))
        with self.assertRaises(ReviewConflict):
            self.store.append_capture_generation(self.key, self.updated(), expected_raw_snapshot_sha256=old['raw_capture_sha256'])

    def test_new_generation_cannot_erase_owner_review_or_corrections(self):
        initial = self.store.get_document(self.key, expose=False)
        self.store.apply_review(self.key, self.body(initial, decision='rejected', incorrect_fields=['address'], corrected_fields={'address': 'Synthetic owner-corrected address'}, comment='Synthetic feedback'))
        before = self.store.get_document(self.key, expose=False)
        result = self.store.append_capture_generation(self.key, self.updated(), expected_raw_snapshot_sha256=before['raw_capture_sha256'])
        self.assertTrue(result['appended']); self.assertFalse(result['activated'])
        restarted = ReviewStore(self.store.root)
        after = restarted.get_document(self.key, expose=False)
        self.assertEqual(after['status'], 'rejected')
        self.assertEqual(after['fields']['address']['value'], 'Synthetic owner-corrected address')
        self.assertEqual(after['raw_capture_sha256'], before['raw_capture_sha256'])
        self.assertEqual(after['revision'], before['revision'])
        self.assertEqual(restarted.capture_lineage(self.key)['active_capture_sha256'], before['raw_capture_sha256'])

    def test_active_generation_cannot_mask_original_snapshot_corruption(self):
        initial = self.store.get_document(self.key, expose=False)
        self.store.append_capture_generation(self.key, self.updated(), expected_raw_snapshot_sha256=initial['raw_capture_sha256'])
        with self.store.connection(write=True) as db:
            db.execute('UPDATE documents SET capture_json=? WHERE id=?', ('{}', self.key))
        with self.assertRaisesRegex(ReviewError, 'original_snapshot_integrity_failed'):
            self.store.get_document(self.key, expose=False)

    def test_saved_variants_reprocess_has_no_ocr_and_reuses_same_producer(self):
        producer = {'revision': 'c' * 40, 'source_dirty': False, 'dirty_sha256': '1' * 64}
        def parser(variants):
            self.assertEqual(variants, ['Synthetic saved reading'])
            card = DebtorCard(); card.case_number = Field('SYNTHETIC-NEW-CASE')
            return card
        with patch('sro_robot.core.extract.parse_file', side_effect=AssertionError('OCR/file parser must not run')):
            result = reprocess_saved_variants(self.store, parser=parser, identity=lambda: producer)
            repeated = reprocess_saved_variants(self.store, parser=parser, identity=lambda: producer)
        self.assertEqual(result['activated'], 1)
        self.assertEqual(result['ocr_calls'], 0)
        self.assertEqual(repeated['reused'], 1)
        with self.store.connection() as db:
            active = self.store._active_row(db, db.execute('SELECT * FROM documents').fetchone())
        current = json.loads(active['capture_json'])
        self.assertEqual(current['variants'], self.capture['variants'])
        self.assertEqual(current['capture']['completed_ocr'], 0)
        self.assertEqual(current['capture']['reprocess']['reading_producer']['completed_ocr'], 7)
        original_reading = current['capture']['reprocess']['reading_snapshot_sha256']
        producer['revision'] = 'd' * 40
        again = reprocess_saved_variants(self.store, parser=parser, identity=lambda: producer)
        self.assertEqual(again['activated'], 1)
        with self.store.connection() as db:
            newest = self.store._active_row(db, db.execute('SELECT * FROM documents').fetchone())
        evidence = json.loads(newest['capture_json'])['capture']['reprocess']
        self.assertEqual(evidence['reading_producer']['completed_ocr'], 7)
        self.assertEqual(evidence['reading_producer']['revision'], 'a' * 40)
        self.assertEqual(evidence['reading_snapshot_sha256'], original_reading)

    def test_parser_source_change_cannot_activate_unbound_generation(self):
        producer = {'revision': 'c' * 40, 'source_dirty': False, 'dirty_sha256': '1' * 64}
        def parser(variants):
            producer['revision'] = 'd' * 40
            return DebtorCard()
        result = reprocess_saved_variants(self.store, parser=parser, identity=lambda: producer)
        self.assertTrue(result['source_changed'])
        self.assertEqual(result['activated'], 0)
        self.assertEqual(self.store.capture_lineage(self.key)['generations'], [])

    def test_repeated_one_document_pulse_advances_past_already_refreshed_rows(self):
        second = Path(self.temp.name) / 'second.pdf'
        second.write_bytes(b'%PDF second synthetic lineage\n%%EOF\n')
        other_capture = deepcopy(self.capture)
        other_capture['input_sha256'] = hashlib.sha256(second.read_bytes()).hexdigest()
        self.store.import_capture(second, other_capture)
        producer = {'revision': 'c' * 40, 'source_dirty': False, 'dirty_sha256': '1' * 64}
        calls = []
        def parser(variants):
            calls.append(list(variants))
            return DebtorCard()
        first = reprocess_saved_variants(self.store, limit=1, parser=parser, identity=lambda: producer)
        following = reprocess_saved_variants(self.store, limit=1, parser=parser, identity=lambda: producer)
        exhausted = reprocess_saved_variants(self.store, limit=1, parser=parser, identity=lambda: producer)
        self.assertEqual((first['considered'], first['activated']), (1, 1))
        self.assertEqual((following['considered'], following['activated'], following['reused']), (1, 1, 1))
        self.assertEqual((exhausted['considered'], exhausted['activated'], exhausted['reused']), (0, 0, 2))
        self.assertEqual(len(calls), 2)

    def test_name_component_correction_updates_canonical_fio_without_stale_source(self):
        self.store.apply_review(self.key, self.body(decision='rejected', incorrect_fields=['first_name'], corrected_fields={'first_name': 'Пример'}, comment='Synthetic name component'))
        current = self.store.get_document(self.key, expose=False)
        self.assertEqual(current['fields']['first_name']['value'], 'Пример')
        self.assertEqual(current['fields']['fio_raw']['value'], 'Тестов Пример Тестович')
        self.assertEqual(current['fields']['fio_raw']['source'], 'owner-feedback')
        self.assertIsNone(current['fields']['fio_raw']['source_start'])

    def test_owner_cleared_full_fio_clears_all_normalized_parts(self):
        self.store.apply_review(self.key, self.body(decision='rejected', reviewed_fields=list(FIELDS), incorrect_fields=['fio_raw'], corrected_fields={'fio_raw': ''}, comment='Synthetic full-name clear'))
        current = self.store.get_document(self.key, expose=False)
        for name in NAME_PARTS:
            self.assertIsNone(current['fields'][name]['value'])
        self.assertEqual(current['fields']['fio_raw']['value'], '')

    def test_manual_civil_party_choice_keeps_role_and_original_projection_after_restart(self):
        old = self.store.get_document(self.key, expose=False)
        updated = self.updated()
        updated['observed_pipeline']['review_suggestions'].append(party('plaintiff', 'ООО «Синтетический истец»', '1111111111111'))
        self.store.append_capture_generation(self.key, updated, expected_raw_snapshot_sha256=old['raw_capture_sha256'])
        current = self.store.get_document(self.key, expose=False)
        plaintiff = next(item for item in current['review_projection']['candidates'] if item['role'] == 'plaintiff')
        self.store.apply_review(self.key, self.body(current, decision='rejected', selected_candidate_id=plaintiff['id'], incorrect_fields=['legal_name','ogrn'], corrected_fields={'legal_name': plaintiff['fields']['legal_name']['value'], 'ogrn': plaintiff['fields']['ogrn']['value']}, comment='Synthetic explicit party selection'))
        restarted = ReviewStore(self.store.root)
        result = restarted.get_document(self.key, expose=False)
        self.assertEqual(result['review_projection']['subject_role'], 'plaintiff')
        self.assertFalse(result['review_projection']['subject_is_debtor'])
        self.assertEqual(result['fields']['legal_name']['value'], 'ООО «Синтетический истец»')
        with restarted.connection() as db:
            row = db.execute('SELECT projection_json,selected_candidate_id FROM review_projections').fetchone()
        self.assertEqual(json.loads(row[0])['subject_role'], 'defendant')
        self.assertEqual(row[1], plaintiff['id'])

    def test_ambiguous_party_card_cannot_be_approved_without_subject_choice(self):
        old = self.store.get_document(self.key, expose=False)
        updated = self.updated()
        updated['observed_pipeline']['review_suggestions'].append(party('defendant', 'ООО «Второй синтетический ответчик»', '1111111111111'))
        self.store.append_capture_generation(self.key, updated, expected_raw_snapshot_sha256=old['raw_capture_sha256'])
        current = self.store.get_document(self.key, expose=False)
        self.assertTrue(current['review_projection']['selection_required'])
        with self.assertRaisesRegex(ReviewConflict, 'subject_selection_required'):
            self.store.apply_review(self.key, self.body(current))


if __name__ == '__main__':
    unittest.main()
