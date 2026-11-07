import json
import unittest
from sro_robot.research.review_diagnostics import diagnostic_packet


class SafeDiagnosticsTests(unittest.TestCase):
    def test_real_values_comments_unknown_keys_and_identifiers_cannot_enter_export(self):
        private = 'PRIVATE_SYNTHETIC_SENTINEL'
        result = diagnostic_packet({'comment': private, 'diagnostics': [
            {'field': 'address', 'category': 'missing_address_unit', 'count': 2, 'status': 'open',
             'parser_revision': 'a'*40, 'comment': private, 'original_value': private, 'document_id': private},
            {'field': private, 'category': private, 'count': 1},
            {'field': 'inn', 'category': private, 'count': 1, 'status': private, 'parser_revision': private}]})
        self.assertNotIn(private, json.dumps(result))
        self.assertEqual(result['synthetic_reproductions'][0]['reference'], 'independently-authored-synthetic-text')
        self.assertEqual(result['diagnostics'][0]['count'], 2)
        self.assertEqual(result['diagnostics'][1]['parser_revision'], 'unidentified')

    def test_noninteger_counts_are_not_copied(self):
        self.assertEqual(diagnostic_packet({'diagnostics': [{'field':'address','count':'PRIVATE'}]})['diagnostics'], [])

    def test_name_components_are_fixed_review_fields_without_expanding_gold_schema(self):
        from scripts.dev.quality.court_dataset import FIELDS as EVALUATION_FIELDS
        names = ['last_name', 'first_name', 'middle_name']
        result = diagnostic_packet({'diagnostics': [
            {'field': name, 'category': 'corrected_value', 'status': 'open', 'count': 1,
             'original_value': 'PRIVATE_SYNTHETIC_NAME', 'comment': 'PRIVATE_SYNTHETIC_COMMENT'} for name in names]})
        self.assertEqual([item['field'] for item in result['diagnostics']], names)
        self.assertNotIn('PRIVATE_SYNTHETIC', json.dumps(result))
        self.assertEqual(len(EVALUATION_FIELDS), 17)
