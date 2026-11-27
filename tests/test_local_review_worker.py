"""Synthetic acquisition -> capture -> durable owner feedback integration."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.dev.quality import corpus_acquisition as acquisition
from sro_robot.research.review_schema import CAPTION_RESCAN_PROTOCOL
from sro_robot.research.review_store import ReviewStore
from sro_robot.research.review_worker import ReviewWorker


def captured(document_id):
    return {'input_sha256': document_id, 'observed_pipeline': {'case_number': {'value': 'А77-42/2026'}},
            'primary_pages': [{'page': 1, 'origin': 'embedded-text-weak', 'text': 'Synthetic only'}],
            'capture': {'accepted_parser_identity': True, 'revision': 'a'*40, 'source_dirty': False,
                        'dirty_sha256': 'b'*64, 'runtime': {'synthetic': True}}}


class ReviewWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.corpus = self.base / 'corpus'
        self.exports = self.base / 'exports'; self.exports.mkdir(mode=0o700)
        acquisition.init(self.corpus)
        asset = self.exports / 'synthetic.pdf'; asset.write_bytes(b'%PDF-1.4\nSynthetic only\n%%EOF\n')
        manifest = self.base / 'feed.jsonl'
        manifest.write_text(json.dumps({'source_id': 'synthetic-source', 'channel': 'internal-export',
                                        'local_path': str(asset), 'authority': 'owner_assertion'}) + '\n')
        acquisition.enqueue(self.corpus, manifest, export_root=self.exports)
        acquisition.run(self.corpus)
        self.store = ReviewStore(self.base / 'reviews')

    def tearDown(self):
        self.temporary.cleanup()

    def test_new_original_reaches_review_once_and_feedback_survives_worker_restart(self):
        calls = []
        def run(document_id):
            calls.append(document_id); return captured(document_id)
        worker = ReviewWorker(self.store, self.corpus, capture_runner=run)
        self.assertEqual(worker.tick()['processed'], 1)
        document = self.store.next_document()
        self.assertEqual(self.store.stats()['pending_review'], 1)
        self.store.apply_review(document['id'], {'action_id': 'synthetic-reject', 'expected_revision': document['revision'],
            'reviewed_fields': document['review_projection']['applicable_fields'],
            'expected_snapshot_sha256': document['snapshot_sha256'],
            'decision': 'rejected', 'incorrect_fields': ['case_number'], 'corrected_fields': {'case_number': 'А77-43/2026'},
            'comment': 'Synthetic correction'})
        restarted = ReviewWorker(ReviewStore(self.store.root), self.corpus, capture_runner=run)
        self.assertEqual(restarted.tick()['processed'], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.stats()['rejected'], 1)
        self.assertEqual(self.store.get_document(document['id'], expose=False)['fields']['case_number']['value'], 'А77-43/2026')

    def test_disk_backpressure_does_not_capture_or_lose_original(self):
        worker = ReviewWorker(self.store, self.corpus, min_free_bytes=10**18,
                              capture_runner=lambda _: self.fail('OCR despite disk boundary'))
        self.assertEqual(worker.tick()['state'], 'backpressure')
        self.assertEqual(self.store.stats()['total'], 0)
        self.assertEqual(acquisition.status(self.corpus)['complete'], 1)

    def test_backlog_boundary_preserves_uncaptured_work(self):
        worker = ReviewWorker(self.store, self.corpus, max_pending=0,
                              capture_runner=lambda _: self.fail('OCR despite review backlog'))
        self.assertEqual(worker.tick()['state'], 'backpressure')
        self.assertEqual(acquisition.status(self.corpus)['complete'], 1)

    def test_complete_private_capture_after_crash_is_adopted_without_rerun(self):
        worker = ReviewWorker(self.store, self.corpus)
        worker.sync()
        document = self.store.unprocessed()[0]
        directory = self.store.root / 'captures'; directory.mkdir(mode=0o700)
        (directory / (document['id'] + '.json')).write_text(json.dumps(captured(document['id'])))
        with patch('sro_robot.research.review_worker.run_owned', side_effect=AssertionError('OCR rerun')):
            self.assertEqual(worker.tick()['processed'], 1)
        self.assertEqual(self.store.stats()['pending_review'], 1)

    def test_appointment_photo_uses_separate_capture_and_resumes_without_ocr(self):
        from PIL import Image

        worker = ReviewWorker(self.store, self.corpus)
        photo = self.exports / 'synthetic.png'
        with Image.new('RGB', (30, 40), 'white') as image:
            image.save(photo)
        document_id = self.store.enqueue_asset(photo, {'expert_review_kind': 'appointment'})
        directory = self.store.root / 'captures'
        directory.mkdir(mode=0o700)
        standard = directory / (document_id + '.json')
        standard.write_text(json.dumps(captured(document_id)))
        scratch = []

        def run(command, **kwargs):
            self.assertIn('--appointment-photo-caption', command)
            output = Path(command[command.index('--output') + 1])
            self.assertNotEqual(output, standard)
            workspace = Path(kwargs['env']['TMPDIR'])
            self.assertEqual(workspace.parent, directory)
            self.assertTrue(workspace.is_dir())
            scratch.append(workspace)
            result = captured(document_id)
            result['capture']['protocol'] = CAPTION_RESCAN_PROTOCOL
            output.write_text(json.dumps(result))

        with patch('sro_robot.research.review_worker.run_owned', side_effect=run) as runner:
            result = worker.capture(document_id)
            self.assertEqual(worker.capture(document_id), result)
            runner.assert_called_once()
        self.assertFalse(scratch[0].exists())
        self.assertIs(self.store.put_capture(document_id, result), True)
        self.assertEqual(self.store.stats(queue='appointments')['pending_review'], 1)
        worker.max_pending = 1
        self.assertEqual(worker.tick()['state'], 'backpressure')
