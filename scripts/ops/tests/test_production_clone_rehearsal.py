import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).with_name('production_clone_rehearsal.py')
spec = importlib.util.spec_from_file_location('rehearsal', SCRIPT)
rehearsal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rehearsal)


class RehearsalHelperTests(unittest.TestCase):
    def setUp(self):
        rehearsal.CLONE_NAME = None
        rehearsal.TEMP = None
        rehearsal.PRESERVE_TEMP = False
        rehearsal.CANDIDATE_COMMIT = None
        rehearsal.CHECKPOINT_SHA256 = None
        rehearsal.FAILURE_WORKSPACE = None
        rehearsal.CLEANUP_ERROR = None
        rehearsal.BACKUP_ROOT = Path('/var/backups/sro-robot')

    def test_venv_marker_uses_sys_prefix_for_symlinked_python(self):
        with tempfile.TemporaryDirectory(prefix='sro-venv-test-') as tmp:
            root = Path(tmp)
            candidate = root / 'candidate'
            candidate.mkdir()
            requirements = candidate / 'requirements.txt'
            requirements.write_text('example==1\n', encoding='utf-8')
            venv = root / 'venv'
            subprocess.run([os.environ.get('PYTHON', 'python3'), '-m', 'venv', str(venv)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            python = venv / 'bin' / 'python'
            marker = venv / '.requirements-sha256'
            marker.write_text(hashlib.sha256(requirements.read_bytes()).hexdigest() + '\n', encoding='ascii')
            # Most Unix venvs symlink to the base interpreter; prove the test
            # exercises that case when the platform provides it.
            if python.is_symlink():
                self.assertNotEqual(python.resolve().parent.parent, venv)
            rehearsal.check_venv(python, candidate)
            marker.write_text('0' * 64 + '\n', encoding='ascii')
            with self.assertRaisesRegex(rehearsal.RehearsalError, 'CandidateEnvironmentRequirementsMismatch'):
                rehearsal.check_venv(python, candidate)

    def test_clone_name_registered_before_failed_docker_run_and_cleanup_removes_it(self):
        with tempfile.TemporaryDirectory(prefix='sro-clone-test-') as tmp:
            root = Path(tmp)
            dump = root / 'dump'
            dump.write_bytes(b'disposable synthetic dump')
            socket_dir = root / 'socket'
            socket_dir.mkdir()
            with mock.patch.object(rehearsal, 'run', side_effect=rehearsal.RehearsalError('CommandFailed')):
                with self.assertRaises(rehearsal.RehearsalError):
                    rehearsal.create_clone('synthetic-image', 'synthetic_db', dump, socket_dir)
            clone = rehearsal.CLONE_NAME
            self.assertRegex(clone, r'^sro-migration-rehearsal-[0-9a-f]{12}$')
            with mock.patch.object(rehearsal.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, b'', b''),
                subprocess.CompletedProcess([], 0, b'', b''),
            ]) as docker_run:
                rehearsal.cleanup()
            self.assertEqual(docker_run.call_count, 2)
            self.assertEqual(docker_run.call_args_list[0].args[0], ['docker', 'rm', '-f', clone])
            self.assertEqual(docker_run.call_args_list[1].args[0], [
                'docker', 'ps', '-a', '--filter', f'name=^/{clone}$', '--format', '{{.Names}}'
            ])
            self.assertIsNone(rehearsal.CLONE_NAME)

    def test_cleanup_fails_closed_when_clone_removal_cannot_be_verified(self):
        rehearsal.CLONE_NAME = 'sro-migration-rehearsal-0123456789ab'
        with mock.patch.object(rehearsal.subprocess, 'run', side_effect=[
            subprocess.CompletedProcess([], 0, b'', b''),
            subprocess.CompletedProcess([], 0, b'sro-migration-rehearsal-0123456789ab\n', b''),
        ]):
            with self.assertRaisesRegex(rehearsal.RehearsalError, 'CloneRemovalUnverified'):
                rehearsal.cleanup()
        self.assertIsNotNone(rehearsal.CLONE_NAME)
        self.assertEqual(rehearsal.CLEANUP_ERROR, 'CloneRemovalUnverified')

    def test_row_signature_is_order_independent_without_sorting_rows(self):
        with mock.patch.object(rehearsal, 'sql_lines', side_effect=[[b'row-a', b'row-b'], [b'row-b', b'row-a']]) as rows:
            first = rehearsal.row_table_signature('source', 'db', 'safe_table', ['value'], clone=False)
            second = rehearsal.row_table_signature('clone', 'db', 'safe_table', ['value'], clone=True)
        self.assertEqual(first, second)
        self.assertEqual(rows.call_count, 2)
        self.assertNotIn('ORDER BY', rows.call_args_list[0].args[2])

    def test_source_sql_is_read_only_and_query_timeouts_are_set(self):
        result = subprocess.CompletedProcess([], 0, b'42\n', b'')
        with mock.patch.object(rehearsal, 'run', return_value=result) as command:
            self.assertEqual(rehearsal.sql_capture('source', 'db', 'SELECT 42;', clone=False), '42')
        sql = command.call_args.kwargs['stdin'].decode()
        self.assertIn('SET default_transaction_read_only = on;', sql)
        self.assertIn(f"SET statement_timeout = '{rehearsal.QUERY_TIMEOUT_SECONDS}s';", sql)
        self.assertIn("SET lock_timeout = '5s';", sql)

    def test_sequence_snapshot_filters_relations_to_sequences(self):
        with mock.patch.object(rehearsal, 'sql_capture', return_value='[]') as query:
            self.assertEqual(rehearsal.sequence_snapshot('synthetic', 'db', clone=True), {})
        sql = query.call_args.args[2]
        self.assertIn("seq.relkind='S'", sql)
        self.assertNotIn('dep.deptype', sql)

    def test_clone_role_setup_transfers_only_application_relations(self):
        with mock.patch.object(rehearsal, 'sql_capture', side_effect=['false', '0', '', '', '']) as query:
            rehearsal.configure_clone_roles('synthetic-clone', 'synthetic_db')
        role_setup = query.call_args.args[2]
        self.assertIn("relation.relkind IN ('r','p','v','m','S')", role_setup)
        self.assertIn("dependency.deptype IN ('a','i')", role_setup)
        self.assertIn('ALTER %s %I.%I OWNER TO %I', role_setup)
        self.assertIn("routine.prokind IN ('f','p')", role_setup)
        self.assertIn('ALTER %s %I.%I(%s) OWNER TO %I', role_setup)
        self.assertNotIn('REASSIGN OWNED BY postgres', role_setup)

    def test_target_invariants_use_current_card_request_table(self):
        source = inspect.getsource(rehearsal.target_invariants)
        self.assertIn('"debtor_card_create_request"', source)
        self.assertNotIn('"manual_card_create_request"', source)

    def test_revision_pins_are_required_and_match_schema_mode(self):
        with self.assertRaisesRegex(rehearsal.RehearsalError, 'ExpectedRevisionPinsRequired'):
            rehearsal.validate_revision_pair('same-schema', None, 'source_revision')
        with self.assertRaisesRegex(rehearsal.RehearsalError, 'ExpectedRevisionPinsRequired'):
            rehearsal.validate_revision_pair('migration', 'source_revision', None)
        with self.assertRaisesRegex(rehearsal.RehearsalError, 'InvalidRehearsalMode'):
            rehearsal.validate_revision_pair('unreviewed', 'source_revision', 'target_revision')
        with self.assertRaisesRegex(rehearsal.RehearsalError, 'InvalidRevisionPin'):
            rehearsal.validate_revision_pair('same-schema', 'invalid revision', 'invalid revision')
        with self.assertRaisesRegex(rehearsal.RehearsalError, 'RevisionPinsDoNotMatchMode'):
            rehearsal.validate_revision_pair('same-schema', 'source_revision', 'target_revision')
        with self.assertRaisesRegex(rehearsal.RehearsalError, 'RevisionPinsDoNotMatchMode'):
            rehearsal.validate_revision_pair('migration', 'same_revision', 'same_revision')
        rehearsal.validate_revision_pair('same-schema', 'same_revision', 'same_revision')
        rehearsal.validate_revision_pair('migration', 'source_revision', 'target_revision')

    def test_same_schema_accepts_existing_workflow_rows_and_requires_exact_state(self):
        schema = {
            'alembic_version': ['version_num'],
            'assignment_draft': ['id', 'source_task_id'],
            'debtor_card_draft': ['id', 'preparation_state'],
            'debtor_card_draft_revision': ['id', 'snapshot'],
            'upload_transport_batch': ['id', 'reprocess_parent_id'],
        }
        rows = {
            'assignment_draft': (1, 'a' * 64),
            'debtor_card_draft': (1, 'b' * 64),
            'debtor_card_draft_revision': (1, 'c' * 64),
            'upload_transport_batch': (1, 'd' * 64),
        }
        sequences = {'assignment_draft_id_seq': '17:true', 'debtor_card_draft_id_seq': '9:true'}
        indexes = {'ix_assignment_draft_debtor_card_id_id': ('CREATE INDEX ...', False, True, True)}
        rehearsal.assert_same_schema_state(schema, dict(schema), rows, dict(rows),
                                           sequences, dict(sequences), indexes, dict(indexes))

        changes = [
            ('schema', schema, {**schema, 'new_table': ['id']}, rows, rows, sequences, sequences,
             indexes, indexes, 'SameSchemaTableOrColumnMismatch'),
            ('column', schema, {**schema, 'debtor_card_draft': ['id']}, rows, rows, sequences, sequences,
             indexes, indexes, 'SameSchemaTableOrColumnMismatch'),
            ('row', schema, schema, rows, {**rows, 'debtor_card_draft': (1, 'e' * 64)}, sequences, sequences,
             indexes, indexes, 'SameSchemaRowSignatureMismatch'),
            ('sequence', schema, schema, rows, rows, sequences, {'assignment_draft_id_seq': '18:true'},
             indexes, indexes, 'SameSchemaSequenceSignatureMismatch'),
            ('index', schema, schema, rows, rows, sequences, sequences, indexes,
             {'ix_assignment_draft_debtor_card_id_id': ('CREATE INDEX changed ...', False, True, True)},
             'SameSchemaIndexMismatch'),
        ]
        for (name, source_schema, candidate_schema, source_rows, candidate_rows,
             source_sequences, candidate_sequences, source_indexes, candidate_indexes, error) in changes:
            with self.subTest(name=name), self.assertRaisesRegex(rehearsal.RehearsalError, error):
                rehearsal.assert_same_schema_state(
                    source_schema, candidate_schema, source_rows, candidate_rows,
                    source_sequences, candidate_sequences, source_indexes, candidate_indexes,
                )

    def test_init_db_runs_for_both_reviewed_modes(self):
        with mock.patch.object(rehearsal, 'run_cli') as run_cli:
            for mode in ('migration', 'same-schema'):
                rehearsal.initialize_candidate(mode, Path('/candidate/python'), Path('/candidate'),
                                               Path('/private'), {})
        self.assertEqual([call.args[-1] for call in run_cli.call_args_list], ['init-db', 'init-db'])

    def test_same_schema_candidate_check_avoids_migration_only_invariants(self):
        schema = {'assignment_draft': ['id', 'source_task_id']}
        rows = {'assignment_draft': (1, 'a' * 64)}
        sequences = {'assignment_draft_id_seq': '4:true'}
        indexes = {'ix_assignment_draft_debtor_card_id_id': ('CREATE INDEX ...', False, True, True)}
        with mock.patch.object(rehearsal, 'table_columns', return_value=dict(schema)), \
             mock.patch.object(rehearsal, 'all_row_signatures', return_value=dict(rows)), \
             mock.patch.object(rehearsal, 'sequence_snapshot', return_value=dict(sequences)), \
             mock.patch.object(rehearsal, 'index_snapshot', return_value=dict(indexes)), \
             mock.patch.object(rehearsal, 'candidate_index_invariants') as index_check, \
             mock.patch.object(rehearsal, 'target_invariants') as migration_check:
            self.assertEqual(rehearsal.validate_candidate_mode(
                'same-schema', 'clone', 'synthetic_db', schema, rows, sequences, indexes,
            ), (None, None))
        index_check.assert_called_once_with('clone', 'synthetic_db')
        migration_check.assert_not_called()

    def test_dump_stream_has_a_hard_size_bound(self):
        with tempfile.TemporaryFile() as output:
            with self.assertRaisesRegex(rehearsal.RehearsalError, 'SourceDumpSizeLimitExceeded'):
                rehearsal.run_to_file_bounded(
                    ['python3', '-c', "import sys; sys.stdout.buffer.write(b'x' * 100)"],
                    output, max_bytes=32, timeout=10,
                )
            output.seek(0)
            self.assertLessEqual(len(output.read()), 32)

    def test_dump_stream_has_a_hard_time_bound(self):
        with tempfile.TemporaryFile() as output:
            with self.assertRaisesRegex(rehearsal.RehearsalError, 'SourceDumpTimeout'):
                rehearsal.run_to_file_bounded(
                    ['python3', '-c', 'import time; time.sleep(10)'],
                    output, max_bytes=32, timeout=0.1,
                )

    def test_storage_budget_rejects_insufficient_disk_before_dump(self):
        with tempfile.TemporaryDirectory(prefix='sro-storage-budget-test-') as tmp:
            root = Path(tmp)
            rehearsal.BACKUP_ROOT = root
            with mock.patch.object(rehearsal, 'sql_capture', return_value='100'), \
                 mock.patch.object(rehearsal, 'run', return_value=subprocess.CompletedProcess([], 0, str(root).encode(), b'')), \
                 mock.patch.object(rehearsal.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 0, 100)):
                with self.assertRaisesRegex(rehearsal.RehearsalError, 'InsufficientRehearsalDiskBudget'):
                    rehearsal.preflight_storage('synthetic_db')

    def test_failed_workspace_is_private_and_retained_but_success_cleanup_removes(self):
        with tempfile.TemporaryDirectory(prefix='sro-retain-test-') as tmp:
            retained = Path(tmp) / 'failed'
            retained.mkdir(mode=0o700)
            rehearsal.TEMP = retained
            rehearsal.STAGE = 'candidate_migration'
            rehearsal.CANDIDATE_COMMIT = 'a' * 40
            rehearsal.CHECKPOINT_SHA256 = 'b' * 64
            rehearsal.preserve_failure(rehearsal.RehearsalError('MigrationFailed'))
            report_path = retained / 'failure.json'
            self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
            report = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertEqual(report['stage'], 'candidate_migration')
            self.assertEqual(report['error'], 'MigrationFailed')
            self.assertNotIn('secret', report_path.read_text(encoding='utf-8').lower())
            rehearsal.cleanup()
            self.assertTrue(retained.is_dir())

            rehearsal.PRESERVE_TEMP = False
            rehearsal.FAILURE_WORKSPACE = None
            successful = Path(tmp) / 'success'
            successful.mkdir()
            rehearsal.TEMP = successful
            rehearsal.cleanup()
            self.assertFalse(successful.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
