"""Durable private owner feedback; approval is deliberately not independent gold."""
from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone

from scripts.dev.quality.court_dataset import ROOT, cohort, check_cohort, load_manifest, validate_split, asset_path
from .review_schema import FIELDS, NAME_PARTS
from .review_projection import project, applicable_fields, KINDS, ROLES

STATUSES = ('downloading', 'processing', 'pending_review', 'approved', 'rejected', 'error')
MAX_ASSET_BYTES = 64 * 1024 * 1024
MEDIA = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.heic': 'image/heic', '.tif': 'image/tiff', '.tiff': 'image/tiff'}
PREVIEW_LOCK = threading.BoundedSemaphore(1)


class ReviewError(ValueError):
    pass


class ReviewConflict(ReviewError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def field_map(observed):
    return {name: (dict(observed[name]) if isinstance(observed.get(name), dict) else {'value': observed.get(name)}) for name in FIELDS}


class ReviewStore:
    def __init__(self, root):
        raw = Path(root).expanduser()
        if raw.is_symlink():
            raise ReviewError('private_storage_boundary')
        self.root = raw.resolve()
        if self.root.is_relative_to(ROOT) or ROOT.is_relative_to(self.root):
            raise ReviewError('private_storage_boundary')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise ReviewError('private_storage_permissions')
        self.objects = self.root / 'objects'
        self.objects.mkdir(mode=0o700, exist_ok=True)
        if self.objects.is_symlink():
            raise ReviewError('private_storage_boundary')
        self.database = self.root / 'review.sqlite3'
        if self.database.is_symlink():
            raise ReviewError('private_storage_boundary')
        fd = os.open(self.database, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.database, 0o600)
        with self.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, asset_sha256 TEXT NOT NULL UNIQUE, extension TEXT NOT NULL,
                status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                capture_json TEXT, snapshot_sha256 TEXT, effective_fields_json TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}', error_code TEXT);
            CREATE INDEX IF NOT EXISTS document_pending ON documents(status, created_at, id);
            CREATE INDEX IF NOT EXISTS document_review_order ON documents(status, id);
            CREATE TABLE IF NOT EXISTS reviews (
                action_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                payload_sha256 TEXT NOT NULL, expected_revision INTEGER NOT NULL,
                decision TEXT NOT NULL, original_snapshot_sha256 TEXT NOT NULL,
                corrected_fields_json TEXT NOT NULL, incorrect_fields_json TEXT NOT NULL,
                comment TEXT NOT NULL, reviewed_at TEXT NOT NULL, parser_identity_json TEXT NOT NULL,
                field_states_json TEXT NOT NULL, result_revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS diagnostics (
                id INTEGER PRIMARY KEY, action_id TEXT NOT NULL REFERENCES reviews(action_id),
                document_id TEXT NOT NULL REFERENCES documents(id), field_name TEXT NOT NULL,
                original_value_json TEXT, corrected_value_json TEXT, reason TEXT NOT NULL,
                parser_identity_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS exposure (
                document_id TEXT PRIMARY KEY REFERENCES documents(id), first_seen_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT 'owner-review-interface');
            CREATE TABLE IF NOT EXISTS imports (
                dataset_identity TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES documents(id),
                capture_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL,
                PRIMARY KEY(dataset_identity, document_id));
            CREATE TABLE IF NOT EXISTS external_counts (name TEXT PRIMARY KEY, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS review_projections (
                action_id TEXT PRIMARY KEY REFERENCES reviews(action_id), projection_sha256 TEXT NOT NULL,
                projection_json TEXT NOT NULL, reviewed_fields_json TEXT NOT NULL, selected_candidate_id TEXT);
            CREATE TABLE IF NOT EXISTS review_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS capture_generations (
                document_id TEXT NOT NULL REFERENCES documents(id), snapshot_sha256 TEXT NOT NULL,
                prior_snapshot_sha256 TEXT, capture_json TEXT NOT NULL, producer_json TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(document_id,snapshot_sha256));
            CREATE TABLE IF NOT EXISTS active_capture (
                document_id TEXT PRIMARY KEY REFERENCES documents(id), snapshot_sha256 TEXT NOT NULL,
                FOREIGN KEY(document_id,snapshot_sha256) REFERENCES capture_generations(document_id,snapshot_sha256));
            ''')
            db.execute("INSERT OR IGNORE INTO review_metadata VALUES('store_identity',?)", (secrets.token_hex(32),))
            self.store_identity = db.execute("SELECT value FROM review_metadata WHERE key='store_identity'").fetchone()[0]

    @contextmanager
    def connection(self, write=False):
        if self.database.is_symlink():
            raise ReviewError('private_storage_boundary')
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _copy_asset(self, path):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in MEDIA or path.stat().st_size > MAX_ASSET_BYTES:
            raise ReviewError('invalid_original')
        data = path.read_bytes()
        key, extension = sha(data), path.suffix.lower()
        if not data:
            raise ReviewError('invalid_original')
        target = self.objects / (key + extension)
        if target.exists():
            if target.is_symlink() or sha(target.read_bytes()) != key:
                raise ReviewError('original_integrity_failed')
        else:
            with tempfile.NamedTemporaryFile(dir=self.objects, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            try:
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return key, extension

    def enqueue_asset(self, path, metadata=None, *, status='processing'):
        if status not in {'downloading', 'processing'}:
            raise ReviewError('invalid_initial_status')
        key, extension = self._copy_asset(path)
        timestamp = now()
        with self.connection(write=True) as db:
            db.execute('INSERT OR IGNORE INTO documents(id,asset_sha256,extension,status,created_at,updated_at,metadata_json) VALUES(?,?,?,?,?,?,?)',
                       (key, key, extension, status, timestamp, timestamp, encoded(metadata or {})))
        return key

    @staticmethod
    def _validate_capture(captured, key):
        if not isinstance(captured, dict):
            raise ReviewError('capture_identity_invalid')
        evidence = captured.get('capture', {})
        if (captured.get('input_sha256') != key or evidence.get('accepted_parser_identity') is not True
                or not isinstance(captured.get('observed_pipeline'), dict)
                or not isinstance(evidence.get('runtime'), dict)
                or not all(k in evidence for k in ('revision', 'source_dirty', 'dirty_sha256'))):
            raise ReviewError('capture_identity_invalid')
        for name, field in field_map(captured['observed_pipeline']).items():
            if field.get('value') is not None and not isinstance(field['value'], str):
                raise ReviewError('capture_field_invalid')
        if len(encoded(captured)) > 128 * 1024 * 1024:
            raise ReviewError('capture_size_limit')

    def put_capture(self, document_id, captured):
        self._validate_capture(captured, document_id)
        snapshot = encoded(captured)
        with self.connection(write=True) as db:
            row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if row is None:
                raise ReviewError('document_not_found')
            if row['capture_json'] is not None:
                # Never replace an old result or its review when code is updated.
                return False
            db.execute('UPDATE documents SET capture_json=?,snapshot_sha256=?,effective_fields_json=?,status=?,revision=revision+1,updated_at=?,error_code=NULL WHERE id=?',
                       (snapshot, sha(snapshot.encode()), encoded(field_map(captured['observed_pipeline'])), 'pending_review', now(), document_id))
        return True

    def import_capture(self, path, captured, metadata=None):
        key = self.enqueue_asset(path, metadata)
        inserted = self.put_capture(key, captured)
        return {'id': key, 'inserted': inserted, 'reused': not inserted}

    def import_dataset(self, dataset_root, limit=100):
        root = Path(dataset_root).resolve()
        manifest = load_manifest(root)
        # Feedback intentionally exposes historical controls; only evaluation
        # capture/score must reject their later use as independent holdout.
        validate_split(root, manifest, check_review_exposure=False)
        baseline = json.loads((root / 'baseline.json').read_text())
        identity = sha(encoded(manifest).encode())
        imported = existing = rejected = 0
        for doc in manifest['documents']:
            if imported + existing >= limit:
                break
            capture_path = root / 'captures' / (doc['id'] + '.json')
            if not capture_path.is_file():
                continue
            if capture_path.is_symlink() or not capture_path.resolve().is_relative_to(root):
                raise ReviewError('capture_storage_boundary')
            captured = json.loads(capture_path.read_text())
            check_cohort(baseline, cohort(captured))
            path = asset_path(root, doc)
            self._validate_capture(captured, doc['sha256'])
            result = self.import_capture(path, captured, {'source': 'frozen-dataset', 'medium': doc['medium'], 'case_key': doc['case_key'], 'previously_seen': doc['previously_seen'], 'dataset_identity': identity})
            imported += result['inserted']; existing += result['reused']
            with self.connection(write=True) as db:
                db.execute('INSERT OR IGNORE INTO imports VALUES(?,?,?,?)', (identity, result['id'], sha(capture_path.read_bytes()), now()))
        return {'imported': imported, 'existing': existing, 'rejected': rejected}

    def set_status(self, document_id, status, error_code=None):
        if status not in STATUSES or (error_code is not None and (not isinstance(error_code, str) or len(error_code) > 100)):
            raise ReviewError('invalid_status')
        with self.connection(write=True) as db:
            row = db.execute('SELECT status FROM documents WHERE id=?', (document_id,)).fetchone()
            if not row:
                raise ReviewError('document_not_found')
            if row['status'] in {'approved', 'rejected', 'pending_review'}:
                return False
            db.execute('UPDATE documents SET status=?,error_code=?,updated_at=? WHERE id=?', (status, error_code, now(), document_id))
        return True

    def stats(self):
        with self.connection() as db:
            counts = {s: 0 for s in STATUSES}
            counts.update({r['status']: r['n'] for r in db.execute('SELECT status,COUNT(*) AS n FROM documents GROUP BY status')})
            counts['downloading'] += sum(r['count'] for r in db.execute('SELECT count FROM external_counts'))
        return {**counts, 'total': sum(counts.values())}

    def set_download_status(self, count):
        if type(count) is not int or not 0 <= count <= 100000:
            raise ReviewError('invalid_external_count')
        with self.connection(write=True) as db:
            db.execute("INSERT INTO external_counts VALUES('source-downloading',?) ON CONFLICT(name) DO UPDATE SET count=excluded.count", (count,))

    def unprocessed(self, limit=1):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ReviewError('invalid_processing_limit')
        with self.connection() as db:
            rows = db.execute("SELECT id,asset_sha256,extension,status FROM documents WHERE capture_json IS NULL AND status='processing' ORDER BY created_at,id LIMIT ?", (limit,)).fetchall()
        return [{'id': r['id'], 'status': r['status'], 'path': self.objects / (r['asset_sha256'] + r['extension']), 'capture': None} for r in rows]

    def exposure_records(self):
        """Private local case/exposure bridge; never emit this through aggregate APIs."""
        with self.connection() as db:
            rows = db.execute('SELECT e.document_id,e.first_seen_at,d.asset_sha256,d.metadata_json FROM exposure e JOIN documents d ON d.id=e.document_id').fetchall()
        return [{'document_id': r['document_id'], 'asset_sha256': r['asset_sha256'], 'first_seen_at': r['first_seen_at'], 'metadata': json.loads(r['metadata_json'])} for r in rows]

    def _active_row(self, db, row):
        if row is None:
            return None
        if row['capture_json']:
            if sha(row['capture_json'].encode()) != row['snapshot_sha256']:
                raise ReviewError('original_snapshot_integrity_failed')
            self._validate_capture(json.loads(row['capture_json']), row['asset_sha256'])
        resolved = dict(row)
        resolved['original_capture_sha256'] = row['snapshot_sha256']
        active = db.execute('SELECT g.* FROM active_capture a JOIN capture_generations g ON g.document_id=a.document_id AND g.snapshot_sha256=a.snapshot_sha256 WHERE a.document_id=?', (row['id'],)).fetchone()
        if active:
            if not row['capture_json']:
                raise ReviewError('original_snapshot_missing')
            resolved.update(capture_json=active['capture_json'], snapshot_sha256=active['snapshot_sha256'])
        return resolved

    def capture_lineage(self, document_id):
        """Private producer/source history. Captured content is never an aggregate export."""
        with self.connection() as db:
            db.execute('BEGIN')
            original = db.execute('SELECT snapshot_sha256,capture_json FROM documents WHERE id=?', (document_id,)).fetchone()
            if original is None:
                raise ReviewError('document_not_found')
            active = db.execute('SELECT snapshot_sha256 FROM active_capture WHERE document_id=?', (document_id,)).fetchone()
            rows = db.execute('SELECT snapshot_sha256,prior_snapshot_sha256,producer_json,reason,created_at FROM capture_generations WHERE document_id=? ORDER BY created_at,snapshot_sha256', (document_id,)).fetchall()
        return {'original_capture_sha256': original['snapshot_sha256'], 'active_capture_sha256': active[0] if active else original['snapshot_sha256'],
                'generations': [{**dict(r), 'producer': json.loads(r['producer_json'])} for r in rows]}

    def append_capture_generation(self, document_id, captured, *, expected_raw_snapshot_sha256, reason='parser-saved-variants'):
        self._validate_capture(captured, document_id)
        if reason not in {'parser-saved-variants', 'explicit-reprocess'}:
            raise ReviewError('invalid_reprocess_reason')
        serialized = encoded(captured); snapshot = sha(serialized.encode())
        producer = {key: captured['capture'].get(key) for key in ('revision','source_dirty','dirty_sha256','runtime')}
        with self.connection(write=True) as db:
            row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if row is None or not row['capture_json']:
                raise ReviewError('capture_not_found')
            active = self._active_row(db, row)
            if active['snapshot_sha256'] != expected_raw_snapshot_sha256:
                raise ReviewConflict('capture_generation_conflict')
            self._document(active)
            if snapshot == active['snapshot_sha256']:
                return {'appended': False, 'activated': False, 'snapshot_sha256': snapshot}
            original = json.loads(row['capture_json'])
            old_producer = {key: original['capture'].get(key) for key in ('revision','source_dirty','dirty_sha256','runtime')}
            db.execute('INSERT OR IGNORE INTO capture_generations VALUES(?,?,?,?,?,?,?)', (document_id,row['snapshot_sha256'],None,row['capture_json'],encoded(old_producer),'original-capture',row['created_at']))
            existing = db.execute('SELECT prior_snapshot_sha256 FROM capture_generations WHERE document_id=? AND snapshot_sha256=?', (document_id,snapshot)).fetchone()
            if existing:
                # Never form a lineage cycle or silently reactivate an old generation.
                return {'appended': False, 'activated': False, 'snapshot_sha256': snapshot}
            db.execute('INSERT INTO capture_generations VALUES(?,?,?,?,?,?,?)', (document_id,snapshot,active['snapshot_sha256'],serialized,encoded(producer),reason,now()))
            reviewed = db.execute('SELECT 1 FROM reviews WHERE document_id=? LIMIT 1', (document_id,)).fetchone()
            activate = row['status'] == 'pending_review' and reviewed is None
            if activate:
                db.execute('INSERT INTO active_capture VALUES(?,?) ON CONFLICT(document_id) DO UPDATE SET snapshot_sha256=excluded.snapshot_sha256', (document_id,snapshot))
                db.execute('UPDATE documents SET effective_fields_json=?,revision=revision+1,updated_at=? WHERE id=?', (encoded(field_map(captured['observed_pipeline'])),now(),document_id))
        return {'appended': True, 'activated': activate, 'snapshot_sha256': snapshot}

    def _document(self, row):
        if row is None:
            return None
        if row['capture_json'] and sha(row['capture_json'].encode()) != row['snapshot_sha256']:
            raise ReviewError('snapshot_integrity_failed')
        captured = json.loads(row['capture_json']) if row['capture_json'] else None
        if captured:
            self._validate_capture(captured, row['asset_sha256'])
        observed = dict(captured['observed_pipeline']) if captured else {}
        observed.pop('raw_text', None)
        fields = field_map(observed)
        if row['effective_fields_json']:
            fields.update(json.loads(row['effective_fields_json']))
        projection = project(observed, fields, pending=row['status'] == 'pending_review')
        metadata = json.loads(row['metadata_json'])
        reviewed_subject = metadata.get('owner_review_subject')
        if row['status'] != 'pending_review' and isinstance(reviewed_subject, dict):
            role = reviewed_subject.get('role')
            projection.update(subject_role=role if role in ROLES else None,
                              subject_is_debtor=role == 'debtor',
                              subject_role_location=deepcopy(reviewed_subject.get('role_location')),
                              subject_warning=reviewed_subject.get('warning'))
        projection['raw_capture_sha256'] = row['snapshot_sha256']
        display_sha = sha(encoded({'store_identity': self.store_identity, 'projection': projection, 'effective': fields}).encode())
        projection['projection_sha256'] = display_sha
        evidence = captured.get('capture', {}) if captured else {}
        return {'id': row['id'], 'revision': row['revision'], 'status': row['status'], 'fields': projection['fields'],
                'snapshot_sha256': display_sha, 'raw_capture_sha256': row['snapshot_sha256'],
                'original_capture_sha256': row.get('original_capture_sha256', row['snapshot_sha256']) if isinstance(row, dict) else row['snapshot_sha256'],
                'review_projection': projection,
                'original_fields': field_map(observed), 'observed_pipeline': observed,
                'parser_identity': {k: evidence.get(k) for k in ('revision', 'source_dirty', 'dirty_sha256', 'runtime')},
                'original_url': f"/api/documents/{row['id']}/original", 'media_type': MEDIA[row['extension']],
                'preview_url': f"/api/documents/{row['id']}/preview/1" if row['extension'] != '.pdf' else None,
                'page_count': len(captured.get('primary_pages', [])) if captured else 0,
                'error_code': row['error_code'], 'feedback_kind': 'owner-feedback-not-gold'}

    def get_document(self, document_id, expose=True):
        with self.connection(write=expose) as db:
            if not expose:
                db.execute('BEGIN')
            row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if row and expose:
                db.execute('INSERT OR IGNORE INTO exposure(document_id,first_seen_at) VALUES(?,?)', (document_id, now()))
            return self._document(self._active_row(db, row))

    def next_document(self, after=None):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM documents WHERE status='pending_review' AND id>? ORDER BY id LIMIT 1", (after or '',)).fetchone()
            if row is None:
                row = db.execute("SELECT * FROM documents WHERE status='pending_review' AND id<>? ORDER BY id LIMIT 1", (after or '',)).fetchone()
            if row:
                db.execute('INSERT OR IGNORE INTO exposure(document_id,first_seen_at) VALUES(?,?)', (row['id'], now()))
            return self._document(self._active_row(db, row))

    def asset(self, document_id):
        with self.connection() as db:
            row = db.execute('SELECT asset_sha256,extension FROM documents WHERE id=?', (document_id,)).fetchone()
        if row is None:
            raise ReviewError('document_not_found')
        path = self.objects / (row['asset_sha256'] + row['extension'])
        if path.is_symlink() or not path.is_file() or sha(path.read_bytes()) != row['asset_sha256']:
            raise ReviewError('original_integrity_failed')
        return path, MEDIA[row['extension']]

    def preview(self, document_id, page):
        if type(page) is not int or not 1 <= page <= 60:
            raise ReviewError('invalid_preview_page')
        source, media = self.asset(document_id)
        if media == 'application/pdf':
            raise ReviewError('use_pdf_original')
        directory = self.root / 'previews'
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise ReviewError('private_storage_boundary')
        target = directory / f'{document_id}-{page}.png'
        if target.is_symlink():
            raise ReviewError('private_storage_boundary')
        if target.is_file():
            return target
        if not PREVIEW_LOCK.acquire(blocking=False):
            raise ReviewError('preview_busy_retry')
        temporary = None
        try:
            from PIL import Image, ImageOps
            if source.suffix == '.heic':
                from pillow_heif import register_heif_opener
                register_heif_opener()
            with Image.open(source) as image:
                if image.width * image.height > 40_000_000 or page > getattr(image, 'n_frames', 1):
                    raise ReviewError('invalid_preview_page_or_size')
                image.seek(page - 1)
                if image.width * image.height > 40_000_000:
                    raise ReviewError('invalid_preview_page_or_size')
                rendered = ImageOps.exif_transpose(image).convert('RGB')
                rendered.thumbnail((4096, 4096))
                with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                    temporary = Path(stream.name)
                    rendered.save(stream, format='PNG')
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, target)
            return target
        except ReviewError:
            raise
        except (OSError, ValueError, ImportError):
            raise ReviewError('preview_unavailable') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            PREVIEW_LOCK.release()

    def apply_review(self, document_id, payload):
        if not isinstance(payload, dict) or set(payload) - {'action_id','expected_revision','expected_snapshot_sha256','expected_projection_sha256','reviewed_fields','selected_candidate_id','decision','corrected_fields','incorrect_fields','comment'}:
            raise ReviewError('invalid_review')
        action = payload.get('action_id'); expected = payload.get('expected_revision'); decision = payload.get('decision')
        corrections = payload.get('corrected_fields', {}); incorrect = payload.get('incorrect_fields', []); comment = payload.get('comment', '')
        if (not isinstance(action, str) or not 8 <= len(action) <= 100 or type(expected) is not int or expected < 0
                or not isinstance(decision, str) or decision not in {'approved','rejected'} or not isinstance(corrections, dict) or set(corrections) - set(FIELDS)
                or any(v is not None and (not isinstance(v, str) or len(v) > 20000) for v in corrections.values())
                or not isinstance(incorrect, list) or any(v not in FIELDS for v in incorrect) or len(set(incorrect)) != len(incorrect)
                or not isinstance(comment, str) or len(comment) > 20000):
            raise ReviewError('invalid_review')
        if decision == 'approved' and (incorrect or corrections):
            raise ReviewError('approval_cannot_correct')
        if decision == 'rejected' and not (incorrect or comment.strip()):
            raise ReviewError('rejection_needs_reason')
        if set(corrections) - set(incorrect):
            raise ReviewError('correction_needs_incorrect_field')
        digest = sha(encoded({'document_id': document_id, **payload}).encode())
        with self.connection(write=True) as db:
            previous = db.execute('SELECT * FROM reviews WHERE action_id=?', (action,)).fetchone()
            if previous:
                if previous['document_id'] != document_id or previous['payload_sha256'] != digest:
                    raise ReviewConflict('action_identity_conflict')
                row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
                return {'document': self._document(self._active_row(db, row)), 'stats': self.stats(), 'replayed': True}
            row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if row is None:
                raise ReviewError('document_not_found')
            row = self._active_row(db, row)
            displayed = self._document(row)
            if not isinstance(payload.get('expected_snapshot_sha256'), str) or payload['expected_snapshot_sha256'] != displayed['snapshot_sha256']:
                raise ReviewConflict('snapshot_identity_conflict')
            if payload.get('expected_projection_sha256', displayed['snapshot_sha256']) != displayed['snapshot_sha256']:
                raise ReviewConflict('projection_identity_conflict')
            if row['revision'] != expected or row['status'] != 'pending_review':
                raise ReviewConflict('review_conflict')
            if decision == 'approved' and displayed['review_projection'].get('selection_required'):
                raise ReviewConflict('subject_selection_required')
            scope = payload.get('reviewed_fields')
            if not isinstance(scope, list) or any(name not in FIELDS for name in scope) or len(set(scope)) != len(scope):
                raise ReviewConflict('review_scope_required')
            if set(corrections) - set(scope) or set(incorrect) - set(scope):
                raise ReviewError('correction_outside_review_scope')
            selected_id = payload.get('selected_candidate_id')
            if selected_id is not None and (not isinstance(selected_id, str) or decision != 'rejected' or selected_id not in {c['id'] for c in displayed['review_projection']['candidates']}):
                raise ReviewError('invalid_candidate_selection')
            selected_subject = next((item for item in displayed['review_projection']['candidates'] if item['id'] == selected_id), None)
            if 'entity_kind' in corrections and corrections['entity_kind'] not in KINDS | {None, ''}:
                raise ReviewError('invalid_entity_kind')
            captured = json.loads(row['capture_json']); fields = deepcopy(displayed['fields']); original = deepcopy(displayed['fields'])
            kind = corrections.get('entity_kind', displayed['review_projection']['entity_kind'])
            if kind not in KINDS:
                kind = None
            relevant = set(applicable_fields(kind))
            if set(scope) != relevant and set(scope) != set(FIELDS):
                raise ReviewConflict('review_scope_conflict')
            if decision == 'approved' and kind is None and set(scope) != set(FIELDS):
                raise ReviewConflict('review_scope_conflict')
            if kind is None and set(scope) == set(FIELDS):
                relevant = set(FIELDS)
            states = {name: ('owner-confirmed-present' if decision == 'approved' and name in scope and name in relevant and fields[name].get('value') not in {None, ''} else 'unreviewed') for name in FIELDS}
            for name, value in corrections.items():
                fields[name] = {'value': value, 'confidence': 0.0, 'source': 'owner-feedback',
                                'warning': 'owner-correction-not-independent-gold', 'source_variant': None,
                                'source_start': None, 'source_end': None, 'source_excerpt': None,
                                'source_page': None, 'source_boxes': []}
                states[name] = 'owner-corrected' if value not in {None, ''} else 'owner-cleared-unverified-absence'
            component_edit = bool(set(corrections) & set(NAME_PARTS))
            if component_edit:
                canonical = ' '.join((fields[name].get('value') or '').strip() for name in NAME_PARTS).strip()
                if 'fio_raw' in corrections and (corrections['fio_raw'] or '').strip() != canonical:
                    raise ReviewError('name_correction_conflict')
                fields['fio_raw'] = {'value': canonical or None, 'confidence': 0., 'source': 'owner-feedback',
                                     'derived_from_fields': list(NAME_PARTS), 'warning': 'ФИО собрано из исправленных компонентов; не независимый эталон.',
                                     'source_variant': None, 'source_start': None, 'source_end': None, 'source_page': None, 'source_excerpt': None, 'source_boxes': []}
                states['fio_raw'] = 'owner-derived-canonical-name'
            elif 'fio_raw' in corrections:
                parts = (corrections['fio_raw'] or '').split()
                for index, name in enumerate(NAME_PARTS):
                    fields[name] = {'value': parts[index] if len(parts) == 3 else None, 'confidence': 0., 'source': 'owner-feedback',
                                    'derived_from_fields': ['fio_raw'], 'warning': 'Разнесение исправленного ФИО; не независимый эталон.',
                                    'source_start': None, 'source_end': None, 'source_page': None, 'source_boxes': []}
                    states[name] = 'owner-derived-name-component' if len(parts) == 3 else 'owner-cleared-unverified-absence'
            parser_identity = {k: captured['capture'].get(k) for k in ('revision','source_dirty','dirty_sha256','runtime')}
            timestamp = now(); revision = expected + 1
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (action, document_id, digest, expected, decision, row['snapshot_sha256'], encoded(corrections), encoded(incorrect), comment, timestamp, encoded(parser_identity), encoded(states), revision))
            db.execute('INSERT INTO review_projections VALUES(?,?,?,?,?)', (action, displayed['snapshot_sha256'], encoded(displayed['review_projection']), encoded(scope), selected_id))
            for name in incorrect or (['_document'] if decision == 'rejected' else []):
                db.execute('INSERT INTO diagnostics(action_id,document_id,field_name,original_value_json,corrected_value_json,reason,parser_identity_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                           (action,document_id,name,encoded(original.get(name,{}).get('value')),encoded(corrections[name]) if name in corrections else None,comment,encoded(parser_identity),timestamp))
            db.execute('UPDATE documents SET status=?,effective_fields_json=?,revision=?,updated_at=? WHERE id=?',
                       (decision, encoded(fields), revision, timestamp, document_id))
            metadata = json.loads(row['metadata_json'])
            metadata['owner_review_subject'] = {
                'role': selected_subject.get('role') if selected_subject else displayed['review_projection'].get('subject_role'),
                'role_location': deepcopy(selected_subject.get('role_location')) if selected_subject else displayed['review_projection'].get('subject_role_location'),
                'warning': selected_subject.get('role_warning') if selected_subject else displayed['review_projection'].get('subject_warning'),
                'selected_candidate_id': selected_id, 'action_id': action}
            db.execute('UPDATE documents SET metadata_json=? WHERE id=?', (encoded(metadata),document_id))
            result = self._document(self._active_row(db, db.execute('SELECT * FROM documents WHERE id=?',(document_id,)).fetchone()))
        return {'document': result, 'stats': self.stats(), 'replayed': False}

    def diagnostics_summary(self):
        with self.connection() as db:
            grouped = Counter()
            for row in db.execute('SELECT field_name,status,original_value_json,corrected_value_json,reason,parser_identity_json FROM diagnostics'):
                field = row['field_name'] if row['field_name'] in FIELDS else '_document'
                status = row['status'] if row['status'] in {'open', 'investigating', 'resolved', 'dismissed'} else 'unknown'
                original = json.loads(row['original_value_json']) if row['original_value_json'] else None
                corrected = json.loads(row['corrected_value_json']) if row['corrected_value_json'] else None
                revision = json.loads(row['parser_identity_json']).get('revision', '')
                revision = revision if isinstance(revision, str) and re.fullmatch(r'[0-9a-f]{40}', revision) else 'unknown'
                unit = r'\b(?:кв\.?|квартира|ап\.?|апартаменты)\s*\d+'
                category = 'document_review'
                if field == 'address' and isinstance(corrected, str) and re.search(unit, corrected, re.I) and not re.search(unit, original or '', re.I):
                    category = 'missing_address_unit'
                elif field in {'fio_raw', 'inn', 'snils', 'address', 'legal_name', 'ogrn', 'ogrnip', 'kpp'} and re.search(r'принадлеж|управляющ|другому участник', row['reason'], re.I):
                    category = 'reported_participant_attribution'
                elif original in {None, ''} and corrected not in {None, ''}:
                    category = 'field_omission'
                elif row['corrected_value_json'] is not None:
                    category = 'cleared_value' if corrected in {None, ''} else 'corrected_value'
                grouped[(field, status, category, revision)] += 1
            exposed = db.execute('SELECT COUNT(*) FROM exposure').fetchone()[0]
        return {'diagnostics': [{'field': field, 'status': status, 'category': category, 'parser_revision': revision, 'count': count}
                                for (field, status, category, revision), count in sorted(grouped.items())],
                'category_qualification': 'heuristic-owner-feedback-not-adjudicated',
                'owner_feedback_is_gold': False, 'independently_verified_references': 0, 'review_exposed_documents': exposed}

