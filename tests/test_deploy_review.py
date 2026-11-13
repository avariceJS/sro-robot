"""Synthetic deployment-boundary tests; no SSH or private corpus access."""
import argparse
import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ops/deploy_review.py'
spec = importlib.util.spec_from_file_location('deploy_review_test', SCRIPT)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeployReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = self.root / 'queue'
        self.queue.mkdir(mode=0o700)
        (self.queue / 'objects').mkdir(mode=0o700)
        self.data = b'synthetic-original-document'
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.asset = self.queue / 'objects' / (self.digest + '.pdf')
        self.asset.write_bytes(self.data)
        self.asset.chmod(0o600)
        self.db = sqlite3.connect(self.queue / 'review.sqlite3')
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE documents (asset_sha256 TEXT, extension TEXT);
            CREATE TABLE review_metadata (key TEXT, value TEXT);
            CREATE TABLE reviews (decision TEXT);
        ''')
        self.db.execute('INSERT INTO documents VALUES(?,?)', (self.digest, '.pdf'))
        self.db.execute('INSERT INTO review_metadata VALUES(?,?)', ('store_identity', 'a' * 64))
        self.db.execute('INSERT INTO reviews VALUES(?)', ('synthetic-decision',))
        self.db.commit()
        (self.queue / 'review.sqlite3').chmod(0o600)
        self.private = {}
        for name in ('identity_file', 'known_hosts', 'token_file'):
            path = self.root / name
            path.write_text('x' * 64)
            path.chmod(0o600)
            self.private[name] = path
        self.args = argparse.Namespace(host='root@192.0.2.10', expected_machine_id='b' * 32,
            origin='https://192.0.2.10', commit='c' * 40, expert='expert-1',
            queue_root=self.queue, **self.private)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_consistent_wal_backup_preserves_identity_reviews_and_only_referenced_assets(self):
        (self.queue / 'objects' / 'unused').write_bytes(b'not copied')
        output = self.root / 'snapshot'
        summary = deploy.snapshot_queue(self.queue, output)
        self.assertEqual(summary, {'documents': 1, 'store_identity': 'a' * 64})
        with sqlite3.connect(output / 'review.sqlite3') as result:
            self.assertEqual(result.execute('SELECT decision FROM reviews').fetchone()[0], 'synthetic-decision')
        self.assertEqual([p.name for p in (output / 'objects').iterdir()], [self.asset.name])
        self.assertEqual((output / 'objects' / self.asset.name).read_bytes(), self.data)
        self.assertEqual((output / 'review.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_corrupted_or_symlinked_original_fails_closed(self):
        self.asset.write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError, 'asset_integrity_failed'):
            deploy.snapshot_queue(self.queue, self.root / 'corrupt')
        self.asset.unlink()
        outside = self.root / 'outside'
        outside.write_bytes(self.data)
        outside.chmod(0o600)
        self.asset.symlink_to(outside)
        with self.assertRaises(ValueError):
            deploy.snapshot_queue(self.queue, self.root / 'linked')

    def test_validates_private_source_and_canonical_origin(self):
        root, hostname = deploy.validate(self.args)
        self.assertEqual(root, self.queue.resolve())
        self.assertEqual(hostname, '192.0.2.10')
        for value in ('http://192.0.2.10', 'https://review.test/path', 'https://review.test:443',
                      'https://review.test;bad', 'https://user@review.test', 'https://review.test\n'):
            with self.subTest(origin=value), self.assertRaises(ValueError):
                self.args.origin = value
                deploy.validate(self.args)
        self.args.origin = 'https://review.test'
        self.args.token_file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private_file_required'):
            deploy.validate(self.args)

    def test_preflight_refuses_existing_hosts_before_any_install_and_ip_cert_check(self):
        text = deploy.preflight('a' * 32, '192.0.2.10')
        self.assertIn('-checkip 192.0.2.10', text)
        self.assertIn('/opt/sro-robot', text)
        self.assertIn('/etc/nginx', text)
        self.assertIn(':(80|443|8767)$', text)
        self.assertIn('cat /etc/machine-id', text)
        self.assertNotIn('apt-get', text)
        self.assertIn('-checkhost review.example.test', deploy.preflight('a' * 32, 'review.example.test'))

    def test_installer_is_parseable_and_never_overwrites_queue(self):
        script = deploy.install_script(self.args, '192.0.2.10', '/tmp/synthetic.tar', 'd' * 64)
        result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(script.index('for path in'), script.index('apt-get update'))
        self.assertIn('mkdir -m 0700 /var/lib/sro-review/incoming', script)
        self.assertIn('mv /var/lib/sro-review/incoming/queue /var/lib/sro-review/queue', script)
        self.assertNotIn('rm -rf /var/lib/sro-review/queue', script)
        self.assertIn('--require-hashes', script)
        self.assertNotIn('certbot', script)
        self.assertNotIn('sro-robot.service', script)

    def test_proxy_and_service_preserve_origin_and_private_logs(self):
        proxy = deploy.nginx('review.example.test')
        service = deploy.unit('c' * 40, 'https://review.example.test', 'expert-1')
        self.assertIn('if ($http_host != "review.example.test")', proxy)
        self.assertIn('proxy_set_header Host $http_host;', proxy)
        self.assertIn('access_log off;', proxy)
        self.assertIn('error_log /dev/null;', proxy)
        self.assertNotIn('proxy_ssl_verify off', proxy)
        self.assertIn('User=sro-review', service)
        self.assertIn('ProtectSystem=strict', service)
        self.assertIn('ReadWritePaths=/var/lib/sro-review/queue', service)
        self.assertIn('StandardError=null', service)
        self.assertNotIn('EnvironmentFile', service)

    def test_second_preflight_failure_cleans_private_archive(self):
        transport = self.root / 'private-transport.tar'
        transport.write_bytes(b'synthetic queue and token')
        with patch.object(deploy, 'machine_guard', return_value='set -eu\ntrue\n'), \
                patch.object(deploy, 'preflight', return_value='false\n'):
            script = deploy.install_script(self.args, '192.0.2.10', str(transport), 'd' * 64)
        result = subprocess.run(['bash'], input=script, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(transport.exists())
        self.assertLess(script.index('trap '), script.index('false'))

    def test_cleanup_requires_matching_machine_before_deletion(self):
        transport = self.root / 'must-preserve-on-wrong-machine.tar'
        transport.write_bytes(b'synthetic transport')
        with patch.object(deploy, 'machine_guard', return_value='set -eu\nfalse\n'):
            script = deploy.transport_cleanup('a' * 32, str(transport))
        result = subprocess.run(['bash'], input=script, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(transport.exists())

    def test_outer_cleanup_after_transfer_and_install_failures_never_retries_install(self):
        package = self.root / 'private-package.tar'
        package.write_bytes(b'synthetic queue and token')
        for outcomes, call_count in (([ValueError('transfer_failed'), b''], 2),
                                     ([b'', ValueError('install_failed'), b''], 3)):
            with self.subTest(calls=call_count), \
                    patch.object(deploy, 'run', side_effect=outcomes) as command:
                with self.assertRaises(ValueError):
                    deploy.send_payload(['ssh', 'synthetic-host'], self.args, '192.0.2.10', package)
                self.assertEqual(command.call_count, call_count)
                cleanup = command.call_args_list[-1].kwargs['input'].decode()
                self.assertIn('cat /etc/machine-id', cleanup)
                self.assertIn('rm -f -- /tmp/sro-review-', cleanup)
                self.assertNotIn('apt-get', cleanup)
                self.assertNotIn('/var/lib/sro-review/queue', cleanup)
                self.assertLess(cleanup.index('cat /etc/machine-id'), cleanup.index('rm -f'))

    def test_acme_http_serves_only_exact_host_challenge_token_paths(self):
        proxy = deploy.nginx('192.0.2.10')
        http = proxy.split('server {')[1]
        self.assertIn('if ($http_host != "192.0.2.10")', http)
        self.assertIn('^/\\.well-known/acme-challenge/[A-Za-z0-9_-]+$', http)
        self.assertIn('root /var/lib/sro-review-acme/www;', http)
        self.assertIn('location / { return 444; }', http)
        self.assertIn('limit_except GET { deny all; }', http)
        self.assertNotIn('proxy_pass', http)

    def test_effective_openssh_keeps_known_hosts_path_with_spaces_as_one_file(self):
        # ssh -G prints joined paths and cannot distinguish the broken list.
        # A local banner-only ProxyCommand reaches known-host loading without
        # opening a network socket or reading any real SSH identity/config.
        self.args.known_hosts = self.root / 'synthetic path' / 'known_hosts'
        self.args.identity_file = Path('/dev/null')
        self.args.host = 'root@synthetic.invalid'
        arguments = deploy.ssh_arguments(self.args)
        self.assertIn('UserKnownHostsFile="' + str(self.args.known_hosts) + '"', arguments)
        result = subprocess.run([arguments[0], '-F', '/dev/null', '-vvv', '-T',
            '-o', "ProxyCommand=printf 'SSH-2.0-synthetic\\r\\n'", *arguments[1:]],
            text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)  # Synthetic peer ends before authentication.
        marker = 'load_hostkeys: fopen ' + str(self.args.known_hosts) + ': '
        self.assertIn(marker, result.stderr)
        broken_first_path = 'load_hostkeys: fopen ' + str(self.root / 'synthetic') + ': '
        self.assertNotIn(broken_first_path, result.stderr)

    def test_known_hosts_configuration_cannot_inject_newline_options(self):
        self.args.known_hosts = Path('/tmp/synthetic\nProxyCommand invalid')
        with self.assertRaisesRegex(ValueError, 'invalid_known_hosts_path'):
            deploy.ssh_arguments(self.args)

    def test_acme_parents_remain_publicly_traversable_under_private_umask(self):
        script = deploy.install_script(self.args, '192.0.2.10', '/tmp/synthetic.tar', 'd' * 64)
        acme_root = self.root / 'synthetic-acme'
        commands = [line.replace('/var/lib/sro-review-acme', str(acme_root))
                    for line in script.splitlines()
                    if line.startswith('install -d -m 0755 /var/lib/sro-review-acme')]
        self.assertEqual(len(commands), 4)
        result = subprocess.run(['bash'], input='set -eu\numask 077\n' + '\n'.join(commands),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for suffix in ('', 'www', 'www/.well-known', 'www/.well-known/acme-challenge'):
            self.assertEqual((acme_root / suffix).stat().st_mode & 0o777, 0o755)
        # Queue and login key remain governed by their separate private modes.
        self.assertIn('install -d -m 0700 /var/lib/sro-review', script)
        self.assertIn('chmod 0600 /var/lib/sro-review/login.key', script)

    def test_command_failure_never_repeats_raw_stdout_or_stderr(self):
        with patch.object(deploy.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['ssh'], 1, stdout=b'synthetic-private-value', stderr=b'secret')):
            with self.assertRaisesRegex(ValueError, '^command_failed$'):
                deploy.run(['ssh'])


if __name__ == '__main__':
    unittest.main()
