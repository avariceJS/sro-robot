"""Versioned parser-only refresh of private saved readings. No OCR is invoked."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict
import json
import time

from .review_store import ReviewConflict, ReviewError, sha


def reprocess_saved_variants(store, *, limit=500, batch_seconds=240, parser=None, identity=None):
    from scripts.dev.quality.dataset_capture import source_identity
    from sro_robot.core.extract import parse_variants_for_review
    parser = parser or parse_variants_for_review
    identity = identity or source_identity
    if type(limit) is not int or not 1 <= limit <= 10000 or not 1 <= batch_seconds <= 240:
        raise ReviewError('invalid_reprocess_budget')
    producer = deepcopy(identity())
    deadline = time.monotonic() + batch_seconds
    result = dict(considered=0, activated=0, appended=0, reused=0, preserved_reviewed=0, errors=0, ocr_calls=0, source_changed=False)
    with store.connection() as db:
        # A pulse limits new parser work, not already refreshed rows encountered
        # before it. The separate scan bound covers the live backlog allowance.
        ids = [r[0] for r in db.execute("SELECT d.id FROM documents d WHERE status='pending_review' AND capture_json IS NOT NULL AND NOT EXISTS(SELECT 1 FROM reviews r WHERE r.document_id=d.id) ORDER BY id LIMIT 10000")]
    for key in ids:
        if result['considered'] >= limit or time.monotonic() >= deadline:
            break
        if identity() != producer:
            result['source_changed'] = True
            break
        with store.connection() as db:
            db.execute('BEGIN')
            row = store._active_row(db, db.execute('SELECT * FROM documents WHERE id=?', (key,)).fetchone())
            prior = row['snapshot_sha256']
            captured = json.loads(row['capture_json'])
        store._validate_capture(captured, key)
        if sha(row['capture_json'].encode()) != prior:
            raise ReviewError('snapshot_integrity_failed')
        variants = captured.get('variants')
        if not isinstance(variants, list) or not variants or not all(isinstance(v, str) for v in variants):
            result['errors'] += 1
            continue
        previous = captured['capture'].get('reprocess', {})
        if previous.get('method') == 'saved-variants-no-ocr' and previous.get('producer') == producer:
            result['reused'] += 1
            continue
        result['considered'] += 1
        started = time.monotonic()
        try:
            card = parser(list(variants))
            observed = asdict(card)
            observed.pop('raw_text', None)
            updated = deepcopy(captured)
            updated['observed_pipeline'] = observed
            updated['capture'].update(producer)
            updated['capture'].update(seconds=time.monotonic() - started, completed_ocr=0, parser_attempts=1, accepted_parser_identity=True)
            updated['capture']['reprocess'] = {
                'method': 'saved-variants-no-ocr', 'producer': producer, 'prior_snapshot_sha256': prior,
                'reading_snapshot_sha256': previous.get('reading_snapshot_sha256', previous.get('prior_snapshot_sha256', prior)),
                'reading_producer': deepcopy(previous.get('reading_producer')) or {k: captured['capture'].get(k) for k in ('revision','source_dirty','dirty_sha256','runtime','completed_ocr','seconds')},
            }
            if identity() != producer:
                result['source_changed'] = True
                break
            outcome = store.append_capture_generation(key, updated, expected_raw_snapshot_sha256=prior)
            result['appended'] += int(outcome['appended'])
            result['activated'] += int(outcome['activated'])
            result['preserved_reviewed'] += int(outcome['appended'] and not outcome['activated'])
        except (ReviewConflict, ReviewError, ValueError, TypeError):
            result['errors'] += 1
    return result
