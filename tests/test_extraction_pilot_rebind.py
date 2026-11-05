"""Saved retry responses must be checked against their actual input fragments."""
import copy
import unittest

from scripts.dev.extraction_pilot.harness import text_lines
from scripts.dev.extraction_pilot.rebind import content_hash, rebind


def payload(value, source):
    atom = {'raw': value, 'value': value, 'src': source}
    return {
        'document': {'court': copy.deepcopy(atom)},
        'participants': [{
            'name': copy.deepcopy(atom), 'owner_quote': value, 'owner_src': source,
            'roles': [], 'fields': [],
        }],
    }


class SavedFragmentRebindTests(unittest.TestCase):
    def test_outside_leaf_line_stays_invalid_in_leaf_and_merged_parent(self):
        text = '\n'.join(f'line{i}' for i in range(8))
        page = {'page_id': 'p1', 'text': text, 'lines': text_lines(text, 'p1'), 'words': [], 'is_pdf': False}
        # The first child received L0000..L0005, not L0007.
        outside = payload('line7', 'p1:L0007')
        inside = payload('line7', 'p1:L0007')
        row = {'route': 'C', 'calls': [{
            'page_id': 'p1', 'result': copy.deepcopy(outside),
            'retry_calls': [
                {'page_id': 'p1', 'result': outside},
                {'page_id': 'p1', 'result': inside},
            ],
        }]}
        row['calls'][0]['result']['participants'].extend(copy.deepcopy(inside['participants']))
        before = content_hash(row)
        rebind(row, {'p1': page}, 'C')
        self.assertEqual(content_hash(row), before)
        first, second = row['calls'][0]['retry_calls']
        self.assertEqual(first['result']['document']['court']['evidence']['status'], 'invalid_source_id')
        self.assertEqual(first['result']['participants'][0]['ownership_evidence']['status'], 'invalid_source_id')
        self.assertEqual(second['result']['document']['court']['evidence']['status'], 'verified_quote')
        self.assertEqual(row['calls'][0]['result']['document']['court']['evidence']['status'], 'invalid_source_id')
        self.assertEqual(row['calls'][0]['result']['participants'][0]['ownership_evidence']['status'], 'invalid_source_id')

    def test_nested_child_uses_second_level_subset(self):
        text = '\n'.join(f'line{i}' for i in range(10))
        page = {'page_id': 'p1', 'text': text, 'lines': text_lines(text, 'p1'), 'words': [], 'is_pdf': False}
        # First split's second child sees L0003..L0009. Its first child
        # sees L0003..L0008, so L0009 is outside that nested request.
        outside = payload('line9', 'p1:L0009')
        valid = payload('line9', 'p1:L0009')
        nested = {'page_id': 'p1', 'result': copy.deepcopy(outside), 'retry_calls': [
            {'page_id': 'p1', 'result': outside}, {'page_id': 'p1', 'result': valid},
        ]}
        nested['result']['participants'].extend(copy.deepcopy(valid['participants']))
        other = payload('line0', 'p1:L0000')
        row = {'route': 'C', 'calls': [{'page_id': 'p1', 'result': copy.deepcopy(other),
                 'retry_calls': [{'page_id': 'p1', 'result': other}, nested]}]}
        # The root's merged assertions must mirror all direct child parties.
        row['calls'][0]['result']['participants'].extend(copy.deepcopy(nested['result']['participants']))
        before = content_hash(row)
        rebind(row, {'p1': page}, 'C')
        self.assertEqual(content_hash(row), before)
        self.assertEqual(nested['retry_calls'][0]['result']['document']['court']['evidence']['status'], 'invalid_source_id')
        self.assertEqual(nested['retry_calls'][1]['result']['document']['court']['evidence']['status'], 'verified_quote')
        self.assertEqual(nested['result']['document']['court']['evidence']['status'], 'invalid_source_id')

    def test_visual_retry_does_not_claim_full_page_ocr_proves_crop(self):
        page = {'page_id': 'p1', 'text': 'visible on original page', 'is_pdf': False}
        candidate = payload('visible on original page', 'p1')
        row = {'route': 'D', 'calls': [{'page_id': 'p1', 'result': copy.deepcopy(candidate),
               'retry_calls': [{'page_id': 'p1', 'result': candidate},
                               {'page_id': 'p1', 'result': copy.deepcopy(candidate)}]}]}
        row['calls'][0]['result']['participants'].extend(copy.deepcopy(candidate['participants']))
        before = content_hash(row)
        rebind(row, {'p1': page}, 'D')
        self.assertEqual(content_hash(row), before)
        for call in [row['calls'][0], *row['calls'][0]['retry_calls']]:
            evidence = call['result']['document']['court']['evidence']
            self.assertEqual(evidence['status'], 'page_only_unverified_quote')
            self.assertEqual(evidence['granularity'], 'page')
            self.assertEqual(evidence['boxes'], [])
            self.assertEqual(call['result']['participants'][0]['ownership_evidence']['status'],
                             'page_only_unverified_quote')


if __name__ == '__main__':
    unittest.main()
