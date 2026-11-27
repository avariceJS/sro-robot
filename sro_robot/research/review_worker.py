"""One owned, bounded local capture worker; never sends document data to stdout."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from scripts.dev.quality import corpus_acquisition as acquisition
from scripts.dev.quality.court_dataset import write_json
from scripts.dev.quality.dataset_capture import source_identity
from scripts.dev.tesseract_native import LIMIT_ENV, run_owned, resources, admit
from .review_schema import APPOINTMENT_PHOTO_EXTENSIONS, CAPTION_RESCAN_PROTOCOL

PROJECT = Path(__file__).resolve().parents[2]


class ReviewWorker:
    def __init__(self, store, corpus_root, *, max_pending=1000, max_processing=500,
                 min_free_bytes=2 * 1024**3, capture_runner=None):
        self.store, self.corpus_root = store, Path(corpus_root)
        self.max_pending, self.max_processing = max_pending, max_processing
        self.min_free_bytes = min_free_bytes
        self.capture_runner = capture_runner or self.capture
        self.stopping = threading.Event()

    def backlog(self):
        with self.store.connection() as db:
            return dict(db.execute("SELECT status,COUNT(*) FROM documents WHERE status IN ('pending_review','processing') GROUP BY status"))

    def sync(self):
        state = acquisition.load_queue(self.corpus_root)
        self.store.set_download_status(sum(i['status'] == 'pending' for i in state['items']))
        backlog = self.backlog()
        processing = backlog.get('processing', 0)
        if (backlog.get('pending_review', 0) >= self.max_pending or processing >= self.max_processing
                or shutil.disk_usage(self.store.root).free < self.min_free_bytes):
            return 0
        with self.store.connection() as db:
            known = {r[0] for r in db.execute('SELECT asset_sha256 FROM documents')}
        added = 0
        bad_sources = 0
        for item in state['items']:
            if item['status'] != 'complete' or item['sha256'] in known:
                continue
            if added + processing >= self.max_processing:
                break
            asset = self.corpus_root / 'inbox' / (item['sha256'] + '.pdf')
            if (not asset.is_file() or asset.is_symlink() or asset.stat().st_size > 64 * 1024**2
                    or hashlib.sha256(asset.read_bytes()).hexdigest() != item['sha256']):
                bad_sources += 1
                continue
            if shutil.disk_usage(self.store.root).free - asset.stat().st_size < self.min_free_bytes:
                break
            metadata = dict(item.get('metadata', {}), source_queue_key=item['source_key'], source='local-acquisition')
            document_id = self.store.enqueue_asset(asset, metadata)
            if document_id != item['sha256']:
                raise ValueError('worker_source_identity')
            known.add(item['sha256']); added += 1
        if bad_sources:
            write_json(self.store.root / 'source-integrity.json', {'failed_sources': bad_sources, 'updated_at': time.time()})
        return added

    def capture(self, document_id):
        source, _ = self.store.asset(document_id)
        appointment_photo = source.suffix.lower() in APPOINTMENT_PHOTO_EXTENSIONS and self.store.is_appointment(document_id)
        directory = self.store.root / 'captures'
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise ValueError('worker_private_boundary')
        suffix = f'.{CAPTION_RESCAN_PROTOCOL}.json' if appointment_photo else '.json'
        output = directory / (document_id + suffix)
        if output.is_symlink():
            raise ValueError('worker_private_boundary')
        # A completed output survives a worker crash before the DB transaction.
        if not output.exists():
            environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TESSDATA_PREFIX', 'SRO_TESSERACT_NATIVE') if key in os.environ}
            environment.update(LIMIT_ENV)
            start_swap = resources()[1] if sys.platform == 'darwin' else 0
            command = [sys.executable, str(PROJECT / 'scripts/dev/quality/dataset_capture.py'),
                       '--input', str(source), '--output', str(output), '--private-root', str(self.store.root)]
            if appointment_photo:
                command.append('--appointment-photo-caption')
            with tempfile.TemporaryDirectory(prefix='ocr-', dir=directory) as scratch:
                environment['TMPDIR'] = scratch
                run_owned(command, timeout=65, env=environment, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL,
                          health=(lambda: admit(start_swap)) if sys.platform == 'darwin' else None)
        data = json.loads(output.read_text())
        self.store._validate_capture(data, document_id)
        caption_proof = data['capture'].get('protocol') == CAPTION_RESCAN_PROTOCOL
        if appointment_photo != caption_proof:
            raise ValueError('appointment_caption_capture_protocol')
        return data

    def tick(self):
        added = self.sync()
        if (self.backlog().get('pending_review', 0) >= self.max_pending
                or shutil.disk_usage(self.store.root).free < self.min_free_bytes):
            return {'added': added, 'processed': 0, 'state': 'backpressure'}
        candidates = self.store.unprocessed(limit=1)
        if not candidates:
            return {'added': added, 'processed': 0, 'state': 'idle'}
        document_id = candidates[0]['id']
        producer = source_identity()
        try:
            result = self.capture_runner(document_id)
            self.store.put_capture(document_id, result)
            return {'added': added, 'processed': 1, 'state': 'running'}
        except Exception:
            # Source edits during a capture cannot qualify a result. Retain work
            # for a later stable producer rather than accepting a mixed snapshot.
            if producer == source_identity():
                self.store.set_status(document_id, 'error', error_code='capture_failed')
            return {'added': added, 'processed': 0, 'state': 'capture_error'}

    def run(self, interval=3):
        lock = self.store.root / '.worker.lock'
        if lock.is_symlink():
            raise ValueError('worker_lock_boundary')
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while not self.stopping.is_set():
                try:
                    result = self.tick()
                except Exception:
                    result = {'state': 'worker_error'}
                write_json(self.store.root / 'worker-health.json', dict(result, updated_at=time.time()))
                # Drain useful work promptly; idle polling is the only cadence.
                self.stopping.wait(.05 if result.get('processed') else interval)
        finally:
            os.close(descriptor)
