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
