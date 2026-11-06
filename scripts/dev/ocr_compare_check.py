#!/usr/bin/env python3
"""Check private run-set integrity and compare field outputs without exposing values.

Exact agreement and output presence are diagnostics, never source-verified accuracy.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics

FIELDS = ('fio_raw', 'inn', 'snils', 'address', 'case_number', 'doc_date',
          'legal_name', 'short_name', 'legal_form', 'ogrn', 'ogrnip', 'kpp')


def check(directory):
    manifest = json.loads((directory / 'manifest.json').read_text())
    expected = {(engine, doc['index'], repeat): doc for engine in ('tesseract', 'rapidocr')
                for doc in manifest['inputs'] for repeat in range(manifest['repeats'])}
    runs = {}
    for path in sorted(directory.glob('run-*.json')):
        row = json.loads(path.read_text())
        key = (row['engine'], row['document_index'], row['repeat'])
        if key in runs or key not in expected:
            raise ValueError('Duplicate or unexpected engine/document/repeat')
        if row['sha256'] != expected[key]['sha256'] or not row['temp_removed']:
            raise ValueError('Changed bytes or temporary directory not removed')
        engine = row['engine']
        calls = row['calls']
        prefix = 'tesseract' if engine == 'tesseract' else 'onnx'
        if (calls.get(prefix + '_completed', 0) <= 0
                or calls.get(prefix + '_attempted') != calls[prefix + '_completed']
                or calls['pages_completed'] != expected[key]['frames']):
            raise ValueError('No completed backend calls or page/frame coverage mismatch')
        if row['config']['tesseract_runtime'] != manifest['baseline']:
            raise ValueError('Baseline runtime drift')
        if engine == 'rapidocr' and row['config']['providers'] != [['CPUExecutionProvider']] * 3:
            raise ValueError('Unexpected runtime provider')
        if abs(sum(row['accounted_seconds'].values()) - row['total_seconds']) > 1e-8:
            raise ValueError('Nested time accounting mismatch')
        if row['peak_tree_rss_bytes'] <= 0:
            raise ValueError('Missing memory observation')
        runs[key] = row
    if runs.keys() != expected.keys():
        raise ValueError(f'Incomplete run set: {len(runs)} of {len(expected)}')
    def value(row, field):
        return row['fields'].get(field, {}).get('value') or None
    coverage = {}
    for field in FIELDS:
        counts = Counter()
        for doc in manifest['inputs']:
            left = value(runs['tesseract', doc['index'], 0], field)
            right = value(runs['rapidocr', doc['index'], 0], field)
            counts['tesseract_filled'] += bool(left)
            counts['rapidocr_filled'] += bool(right)
            counts['both_empty'] += left is None and right is None
            counts['equal_nonempty'] += left is not None and left == right
            counts['different_outputs'] += left != right
            for engine in ('tesseract', 'rapidocr'):
                values = {value(runs[engine, doc['index'], rep], field) for rep in range(manifest['repeats'])}
                counts[engine + '_unstable_documents'] += len(values) > 1
        coverage[field] = dict(counts)
    summary = {'verified_runs': len(runs), 'documents': len(manifest['inputs']),
               'repeats': manifest['repeats'], 'quality_scored': False,
               'quality_limit': 'No independently original-verified reference; filled/empty/agreement are not correctness/errors/omissions.',
               'output_diagnostics_per_document_first_repeat': coverage, 'performance': {}}
    for engine in ('tesseract', 'rapidocr'):
        selected = [row for key, row in runs.items() if key[0] == engine]
        phases = set().union(*(r['accounted_seconds'] for r in selected))
        per_doc = [statistics.median(runs[engine, doc['index'], rep]['total_seconds']
                                    for rep in range(manifest['repeats'])) for doc in manifest['inputs']]
        summary['performance'][engine] = {
            'median_seconds': statistics.median(r['total_seconds'] for r in selected),
            'median_of_document_medians_seconds': statistics.median(per_doc),
            'mean_corpus_seconds': sum(r['total_seconds'] for r in selected) / manifest['repeats'],
            'p95_seconds': sorted(r['total_seconds'] for r in selected)[math.ceil(0.95 * len(selected)) - 1],
            'sampled_max_tree_rss_mib': max(r['peak_tree_rss_bytes'] for r in selected) / 1024**2,
            'completed_calls': sum(r['calls'].get('tesseract_completed' if engine == 'tesseract' else 'onnx_completed', 0) for r in selected),
            'phase_medians_seconds': {phase: statistics.median(r['accounted_seconds'].get(phase, 0) for r in selected) for phase in sorted(phases)},
        }
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.directory), ensure_ascii=False, indent=2))
