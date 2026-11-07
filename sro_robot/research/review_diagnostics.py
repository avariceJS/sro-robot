"""Cloud-safe count export and references to maintained synthetic reproductions.

No redaction of court text is attempted. Only fixed categories, field names,
code revisions and integer counts cross this export boundary.
"""
from __future__ import annotations

import re
from .review_schema import FIELDS

FIXTURE = 'tests/fixtures/extraction_quality_cases.json'
TEMPLATES = {
    'missing_address_unit': [3, 4, 5, 6, 10, 11, 12],
    'reported_participant_attribution': [7, 8, 13, 14],
    'field_omission': [3, 7, 8],
    'corrected_value': [0, 1, 2, 3, 7, 8, 9],
    'cleared_value': [9, 13],
    'document_review': [],
}


def diagnostic_packet(summary):
    counts = []
    categories = set()
    for item in summary.get('diagnostics', []):
        if not isinstance(item, dict) or item.get('field') not in {*FIELDS, '_document'}:
            continue
        category = item.get('category', 'document_review')
        if category not in TEMPLATES:
            category = 'document_review'
        status = item.get('status') if item.get('status') in {'open', 'investigating', 'fixed', 'resolved', 'dismissed'} else 'open'
        count = item.get('count')
        if type(count) is not int or count < 0:
            continue
        revision = item.get('parser_revision', '')
        if not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{40}', revision):
            revision = 'unidentified'
        counts.append({'field': item['field'], 'category': category, 'status': status,
                       'parser_revision': revision, 'count': count})
        categories.add(category)
    return {'diagnostics': counts, 'qualification': 'owner-feedback-not-independent-gold',
            'category_qualification': 'heuristic-not-adjudicated',
            'synthetic_reproductions': [
                {'category': category, 'fixture': FIXTURE, 'zero_based_indexes': TEMPLATES[category],
                 'reference': 'independently-authored-synthetic-text'} for category in sorted(categories)]}
