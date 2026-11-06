#!/usr/bin/env python3
"""Local OCR-seeded review proposals. Never accepted as original-verified truth.

This helper is outside both OCR workers and every accuracy computation. Hints are
unvalidated surface-form candidates, not SRO fields, ownership or identity checks.
"""
import argparse
import json
from pathlib import Path

from ocr_engine_backends import file_hash
from ocr_engines_compare import require_private, save


def hints(text):
    result = []
    offset = 0
    for line in text.splitlines(keepends=True):
        words = line.split()
        upper = line.upper()
        for token in words:
            digits = ''.join(char for char in token if char.isdigit())
            if len(digits) in (10, 12) and (token.isdigit() or 'ИНН' in upper):
                start = line.find(token)
                result.append({'category': 'inn', 'start': offset + start, 'end': offset + start + len(token),
                               'reason': '10/12 digit token; no checksum or owner validation'})
        if 'СНИЛС' in upper:
            result.append({'category': 'snils', 'start': offset, 'end': offset + len(line.rstrip()),
                           'reason': 'explicit label; line candidate only'})
        if any(label in upper for label in ('АДРЕС', 'МЕСТО ЖИТЕЛЬСТВА', 'ПРОЖИВА', 'ЗАРЕГИСТРИРОВАН')):
            result.append({'category': 'address', 'start': offset, 'end': offset + len(line.rstrip()),
                           'reason': 'explicit address-related label; occurrence not an address-type assertion'})
        for index in range(len(words) - 2):
            triplet = words[index:index + 3]
            if all(word and word[0].isupper() and word.isalpha() for word in triplet):
                # Surface-form candidate can also be a court/company/other party.
                start = line.find(triplet[0]); end = line.find(triplet[2], start + len(triplet[0])) + len(triplet[2])
                result.append({'category': 'fio', 'start': offset + start, 'end': offset + end,
                               'reason': 'three capitalized alphabetic tokens; person/party not established'})
        offset += len(line)
    return result


def prepare(corpus_path, runs):
    corpus_path = Path(corpus_path).resolve(); runs = Path(runs).resolve()
    require_private(corpus_path); require_private(runs)
    reference = json.loads((corpus_path / 'reference.json').read_text())
    selected = {page['id'] for page in reference['pages'] if page.get('selected')}
    proposals = {}
    for path in sorted(runs.glob('run-*.json')):
        row = json.loads(path.read_text())
        if row['scenario'] != 'cold' or row['engine'] != 'tesseract' or row['repeat'] != 0:
            continue
        for page in row['pages']:
            if page['id'] in selected:
                proposals[page['id']] = {'text': page['text'], 'pixel_sha256': page['pixel_sha256'],
                    'seed': {'engine': 'tesseract', 'scenario': 'cold', 'repeat': 0, 'run_sha256': file_hash(path)},
                    'hints': hints(page['text']), 'verified': False}
    save(corpus_path / 'draft-proposals.json', {'schema': 1, 'method': 'unverified_ocr_seed',
         'corpus_sha256': reference['corpus_sha256'], 'pages': proposals,
         'warning': 'Only draft text and uncertain local hints. Not an independent reference.'})
    return {'draft_pages': len(proposals), 'selected_pages': len(selected),
            'hint_counts': {category: sum(h['category'] == category for p in proposals.values() for h in p['hints'])
                            for category in ('fio', 'inn', 'snils', 'address')},
            'original_verified_pages_added': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--runs', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.corpus, args.runs)))
