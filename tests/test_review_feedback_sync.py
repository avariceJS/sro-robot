"""Synthetic private feedback transport, continuity and safe output boundaries."""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sro_robot.research.review_schema import FIELDS
from sro_robot.research.review_store import ReviewStore

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/dev/review-feedback-sync.py'
spec = importlib.util.spec_from_file_location('feedback_sync', SCRIPT)
feedback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feedback)


class FeedbackSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = ReviewStore(self.base / 'source')
        self.destination = self.base / 'mirror'
        self.original = self.base / 'synthetic.pdf'
        self.original.write_bytes(b'%PDF-1.4\nPRIVATE_SYNTHETIC_ORIGINAL\n%%EOF\n')
        self.key = hashlib.sha256(self.original.read_bytes()).hexdigest()
        capture = {'input_sha256': self.key,
                   'observed_pipeline': {'address': {'value': None}},
                   'capture': {'accepted_parser_identity': True, 'revision': 'a' * 40, 'source_dirty': False, 'dirty_sha256': 'synthetic', 'runtime': {}}}
        self.source.import_capture(self.original, capture)

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, host, target):
        # Run the actual remote backup/stream/cleanup program against only a
        # disposable synthetic WAL database; no SSH or private corpus access.
        program = feedback.REMOTE_PROGRAM.replace(feedback.REMOTE_DATABASE, str(self.source.database))
        program = program.replace('tempfile.mkstemp(prefix=', 'tempfile.mkstemp(dir=str(source.parent), prefix=')
        with target.open('wb') as output:
            subprocess.run([sys.executable, '-c', program], stdout=output,
                           stderr=subprocess.PIPE, check=True, timeout=15)
        self.assertEqual(list(self.source.root.glob('sro-feedback-*')), [])

    def sync(self):
        return feedback.sync('synthetic-host', self.destination, self.transport)

    def review(self, key=None, action='PRIVATE_SYNTHETIC_ACTION'):
        key = key or self.key
        document = self.source.get_document(key, expose=False)
        self.source.apply_review(key, {
            'action_id': action, 'expected_revision': document['revision'],
            'expected_snapshot_sha256': document['snapshot_sha256'],
            'reviewed_fields': list(FIELDS), 'decision': 'rejected',
            'corrected_fields': {'address': 'PRIVATE_SYNTHETIC_ADDRESS, кв. 728'},
            'incorrect_fields': ['address'], 'comment': 'PRIVATE_SYNTHETIC_COMMENT'})

    def mirror_bytes(self):
        return (self.destination / feedback.MIRROR_NAME).read_bytes()

    def test_actual_backup_includes_committed_wal_and_is_idempotent_private_and_safe(self):
        self.assertEqual(self.sync()['reviews'], 0)
        # A reader of the old state prevents the later committed review from
        # checkpointing. Copying only review.sqlite3 would report zero reviews.
        with contextlib.closing(sqlite3.connect(self.source.database)) as reader:
            reader.execute('BEGIN')
            self.assertEqual(reader.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 0)
            self.review()
            with feedback.readonly(self.source.database) as main_file_only:
                self.assertEqual(main_file_only.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 0)
            result = self.sync()
        self.assertEqual(result['reviews'], 1)
        self.assertEqual(result['diagnostics'][0]['category'], 'missing_address_unit')
        encoded = json.dumps(result)
        for private in (self.key, 'PRIVATE_', self.source.store_identity, '728'):
            self.assertNotIn(private, encoded)
        self.assertEqual(self.sync(), result)
        self.assertEqual(feedback.inspect_snapshot(self.destination / feedback.MIRROR_NAME)[1], 1)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.destination / feedback.MIRROR_NAME).stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.destination / 'objects').exists())
        self.assertFalse((self.destination / 'review.sqlite3').exists())
        self.assertEqual(list(self.destination.glob('.feedback-*.sqlite3')), [])

    def test_identity_drift_does_not_replace_previous_snapshot(self):
        self.sync()
        previous = self.mirror_bytes()
        with self.source.connection(write=True) as db:
            db.execute("UPDATE review_metadata SET value=? WHERE key='store_identity'", ('b' * 64,))
        with self.assertRaisesRegex(feedback.SyncError, 'store_identity_changed'):
            self.sync()
        self.assertEqual(self.mirror_bytes(), previous)

    def test_review_mutation_or_deletion_does_not_replace_mirror(self):
        self.review()
        self.sync()
        previous = self.mirror_bytes()
        for statement in ("UPDATE reviews SET comment='changed'", 'DELETE FROM review_actors'):
            # Actor deletion is tested with an explicit previously mirrored row.
            if statement.startswith('DELETE'):
                with self.source.connection(write=True) as db:
                    db.execute('UPDATE reviews SET comment=?', ('PRIVATE_SYNTHETIC_COMMENT',))
                    db.execute("INSERT INTO review_actors VALUES('PRIVATE_SYNTHETIC_ACTION','synthetic-expert')")
                self.sync()
                previous = self.mirror_bytes()
            with self.source.connection(write=True) as db:
                db.execute(statement)
            with self.assertRaisesRegex(feedback.SyncError, 'review_history_changed'):
                self.sync()
            self.assertEqual(self.mirror_bytes(), previous)
        with self.source.connection(write=True) as db:
            db.execute('DELETE FROM diagnostics')
            db.execute('DELETE FROM review_projections')
            db.execute('DELETE FROM reviews')
        with self.assertRaisesRegex(feedback.SyncError, 'review_history_regressed'):
            self.sync()
        self.assertEqual(self.mirror_bytes(), previous)

    def test_diagnostic_deletion_and_each_immutable_column_change_preserve_mirror(self):
        self.review()
        # A second valid reviewed document lets the attribution mutation tests
        # satisfy foreign keys and reach the continuity check itself.
        other_original = self.base / 'other-synthetic.pdf'
        other_original.write_bytes(b'%PDF-1.4\nOTHER_SYNTHETIC_DOCUMENT\n%%EOF\n')
        other_key = hashlib.sha256(other_original.read_bytes()).hexdigest()
        self.source.import_capture(other_original, {
            'input_sha256': other_key, 'observed_pipeline': {},
            'capture': {'accepted_parser_identity': True, 'revision': 'a' * 40,
                        'source_dirty': False, 'dirty_sha256': 'synthetic', 'runtime': {}}})
        self.review(other_key, 'OTHER_SYNTHETIC_ACTION')
        self.sync()
        previous = self.mirror_bytes()
        with self.source.connection() as db:
            original = dict(db.execute('SELECT * FROM diagnostics WHERE action_id=?',
                                       ('PRIVATE_SYNTHETIC_ACTION',)).fetchone())
        replacements = {
            'id': original['id'] + 100, 'action_id': 'OTHER_SYNTHETIC_ACTION',
            'document_id': other_key, 'field_name': 'inn', 'original_value_json': '"changed"',
            'corrected_value_json': '"changed"', 'reason': 'changed',
            'parser_identity_json': '{"revision":"changed"}', 'created_at': 'changed'}
        for column, value in replacements.items():
            with self.subTest(column=column):
                with self.source.connection(write=True) as db:
                    db.execute(f'UPDATE diagnostics SET {column}=? WHERE id=?', (value, original['id']))
                with self.assertRaisesRegex(feedback.SyncError, 'diagnostic_history_changed'):
                    self.sync()
                self.assertEqual(self.mirror_bytes(), previous)
                with self.source.connection(write=True) as db:
                    db.execute(f'UPDATE diagnostics SET {column}=? WHERE id=?',
                               (original[column], value if column == 'id' else original['id']))
        with self.source.connection(write=True) as db:
            db.execute('DELETE FROM diagnostics WHERE id=?', (original['id'],))
        with self.assertRaisesRegex(feedback.SyncError, 'diagnostic_history_changed'):
            self.sync()
        self.assertEqual(self.mirror_bytes(), previous)
        self.assertEqual(list(self.destination.glob('.feedback-*.sqlite3')), [])

    def test_diagnostic_status_change_is_allowed(self):
        self.review()
        self.sync()
        with self.source.connection(write=True) as db:
            db.execute("UPDATE diagnostics SET status='investigating'")
        result = self.sync()
        self.assertEqual(result['reviews'], 1)
        self.assertEqual(result['diagnostics'][0]['status'], 'investigating')
        self.assertEqual(result['diagnostics'][0]['count'], 1)
        with feedback.readonly(self.destination / feedback.MIRROR_NAME) as db:
            self.assertEqual(db.execute('SELECT status FROM diagnostics').fetchone()[0], 'investigating')

    def test_failed_or_corrupt_transfer_keeps_previous_and_removes_staging(self):
        self.sync()
        previous = self.mirror_bytes()
        def corrupt(host, target):
            target.write_bytes(b'PRIVATE_SYNTHETIC_BAD_SQLITE')
        def failed(host, target):
            target.write_bytes(b'partial')
            raise subprocess.CalledProcessError(1, 'ssh', stderr='PRIVATE_REMOTE_DETAIL')
        for transport in (corrupt, failed):
            with self.assertRaises((sqlite3.Error, subprocess.CalledProcessError)):
                feedback.sync('synthetic', self.destination, transport)
            self.assertEqual(self.mirror_bytes(), previous)
            self.assertEqual(list(self.destination.glob('.feedback-*.sqlite3')), [])

    def test_live_queue_checkout_and_symlink_destinations_rejected(self):
        with self.assertRaisesRegex(feedback.SyncError, 'live_queue_is_not_a_mirror'):
            feedback.sync('synthetic', self.source.root, self.transport)
        with self.assertRaisesRegex(feedback.SyncError, 'outside_checkout'):
            feedback.sync('synthetic', SCRIPT.parent / 'private-test', self.transport)
        link = self.base / 'link'
        link.symlink_to(self.source.root)
        with self.assertRaisesRegex(feedback.SyncError, 'private_destination_required'):
            feedback.sync('synthetic', link, self.transport)
        self.assertEqual(self.source.stats()['pending_review'], 1)

    def test_exclusive_lock_and_symlink_mirror_rejected(self):
        self.sync()
        with (self.destination / '.feedback-sync.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.sync()
        mirror = self.destination / feedback.MIRROR_NAME
        mirror.unlink()
        mirror.symlink_to(self.source.database)
        with self.assertRaisesRegex(feedback.SyncError, 'private_file_required'):
            self.sync()

    def test_local_transfer_enforces_byte_and_time_budget(self):
        original_popen = subprocess.Popen
        children = []
        def spawn(code):
            def transport_process(*args, **kwargs):
                child = original_popen([sys.executable, '-c', code], **kwargs)
                children.append(child)
                return child
            return transport_process
        target = self.base / 'bounded-transfer'
        with patch.object(feedback.subprocess, 'Popen', spawn("import sys; sys.stdout.buffer.write(b'x' * 128)")):
            with patch.object(feedback, 'MAX_BYTES', 16):
                with self.assertRaisesRegex(feedback.SyncError, 'snapshot_size_limit'):
                    feedback.download('synthetic', target)
        self.assertLessEqual(target.stat().st_size, 16)
        with patch.object(feedback.subprocess, 'Popen', spawn('import time; time.sleep(2)')):
            with patch.object(feedback, 'TIMEOUT', 0.05):
                with self.assertRaises(subprocess.TimeoutExpired):
                    feedback.download('synthetic', target)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_remote_failed_backup_cleans_private_temporary_files(self):
        program = feedback.REMOTE_PROGRAM.replace(feedback.REMOTE_DATABASE, str(self.source.database))
        program = program.replace('tempfile.mkstemp(prefix=', 'tempfile.mkstemp(dir=str(source.parent), prefix=')
        program = program.replace('268435456', '1')
        completed = subprocess.run([sys.executable, '-c', program], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, b'')
        self.assertEqual(completed.stderr, b'')
        self.assertEqual(list(self.source.root.glob('sro-feedback-*')), [])

    def test_ssh_host_key_and_noninteractive_options_and_no_remote_error_output(self):
        target = self.base / 'transport'
        with patch.object(feedback.subprocess, 'Popen', side_effect=OSError('synthetic')) as popen:
            with self.assertRaises(OSError):
                feedback.download('reviewer@synthetic-host', target)
        options = popen.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=yes', options)
        self.assertIn('BatchMode=yes', options)
        self.assertEqual(popen.call_args.kwargs['stderr'], subprocess.DEVNULL)
        for host in ('-oProxyCommand=bad', 'host; bad', 'host\ncommand'):
            with self.assertRaisesRegex(feedback.SyncError, 'invalid_ssh_destination'):
                feedback.download(host, target)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(feedback, 'sync', side_effect=ValueError('PRIVATE_SOURCE_VALUE')):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(feedback.main(['--host', 'synthetic', '--destination', str(self.destination)]), 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertNotIn('PRIVATE', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
