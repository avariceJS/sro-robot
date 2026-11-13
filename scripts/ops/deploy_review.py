#!/usr/bin/env python3
"""Bootstrap an isolated expert-review host once, from published develop source.

Requires root SSH with a pinned host key, independently checked machine-id and
pre-existing TLS certificate. Never updates an existing review installation.
Queue snapshots and credentials remain in an auto-cleaned private temporary dir.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[2]
# Retain the maintained import graph (including scenario/platform definitions),
# but never package office settings, databases, workers or browser profiles.
SOURCE_PATHS = ('sro_robot/__init__.py', 'sro_robot/config.py', 'sro_robot/core',
                'sro_robot/scenarios', 'sro_robot/platform',
                'sro_robot/research/__init__.py', 'sro_robot/research/review_api.py',
                'sro_robot/research/review_store.py', 'sro_robot/research/review_schema.py',
                'sro_robot/research/review_projection.py', 'sro_robot/research/review_diagnostics.py',
                'sro_robot/research/static', 'app/__init__.py', 'app/services/__init__.py',
                'app/services/fields.py', 'scripts/dev/quality/court_dataset.py',
                'scripts/dev/review-remote.py', 'requirements.txt')


def run(command, **kwargs):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=kwargs.pop('timeout', 60), **kwargs)
    if result.returncode:
        raise ValueError('command_failed')
    return result.stdout


def regular_private(path: Path):
    if path.is_symlink():
        raise ValueError('private_path_required')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
            or info.st_uid != os.geteuid()):
        raise ValueError('private_file_required')
    return path.resolve()


def validate(args):
    if (not re.fullmatch(r'[0-9a-f]{40}', args.commit)
            or not re.fullmatch(r'[0-9a-f]{32}', args.expected_machine_id)
            or not re.fullmatch(r'root@[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}', args.host)
            or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', args.expert)):
        raise ValueError('invalid_deployment_identity')
    parsed = urlsplit(args.origin)
    hostname = parsed.hostname
    if (not hostname or parsed.scheme != 'https' or parsed.path or parsed.query or parsed.fragment
            or parsed.username or parsed.password or parsed.port is not None
            or args.origin != 'https://' + hostname
            or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', hostname)
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                   for label in hostname.split('.'))):
        raise ValueError('exact_https_origin_required')
    root = args.queue_root.expanduser()
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077 or root.stat().st_uid != os.geteuid():
        raise ValueError('private_queue_required')
    root = root.resolve()
    if root.is_relative_to(PROJECT) or PROJECT.is_relative_to(root):
        raise ValueError('queue_outside_checkout_required')
    regular_private(root / 'review.sqlite3')
    for name in ('identity_file', 'known_hosts', 'token_file'):
        path = regular_private(getattr(args, name).expanduser())
        if path.is_relative_to(PROJECT):
            raise ValueError('private_files_outside_checkout_required')
        setattr(args, name, path)
    token = args.token_file.read_bytes()
    if not 32 <= len(token.strip()) <= 256 or any(chr(c).isspace() for c in token.strip()):
        raise ValueError('invalid_private_token')
    return root, hostname


def snapshot_queue(root, target):
    """Take a consistent SQLite backup; copy only its immutable referenced assets."""
    target.mkdir(mode=0o700)
    with sqlite3.connect((root / 'review.sqlite3').as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(target / 'review.sqlite3') as dest:
            deadline = time.monotonic() + 30
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise ValueError('queue_backup_busy')
            source.backup(dest, pages=256, progress=progress)
    os.chmod(target / 'review.sqlite3', 0o600)
    with sqlite3.connect(target / 'review.sqlite3') as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('queue_integrity_failed')
        identity = db.execute("SELECT value FROM review_metadata WHERE key='store_identity'").fetchone()
        if not identity or not re.fullmatch('[0-9a-f]{64}', identity[0]):
            raise ValueError('queue_identity_missing')
        rows = db.execute('SELECT DISTINCT asset_sha256,extension FROM documents').fetchall()
    objects = root / 'objects'
    if objects.is_symlink() or not objects.is_dir() or objects.stat().st_mode & 0o077:
        raise ValueError('private_objects_required')
    (target / 'objects').mkdir(mode=0o700)
    for digest, extension in rows:
        if (not re.fullmatch('[0-9a-f]{64}', digest)
                or extension not in {'.pdf', '.png', '.jpg', '.jpeg', '.heic', '.tif', '.tiff'}):
            raise ValueError('invalid_asset_identity')
        source = regular_private(objects / (digest + extension))
        # Do not follow a link changed after lstat; O_NOFOLLOW closes that race.
        fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
                raise ValueError('asset_boundary')
            data = stream.read(64 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('asset_integrity_failed')
        output = target / 'objects' / (digest + extension)
        output.write_bytes(data)
        output.chmod(0o600)
    return {'documents': len(rows), 'store_identity': identity[0]}


def unit(commit, origin, expert):
    return f'''[Unit]
Description=Isolated SRO expert document review
After=network.target
[Service]
User=sro-review
Group=sro-review
WorkingDirectory=/opt/sro-review/{commit}
ExecStart=/opt/sro-review/{commit}/.venv/bin/python scripts/dev/review-remote.py --root /var/lib/sro-review/queue --public-origin {origin} --expert {expert} --token-file /var/lib/sro-review/login.key
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/sro-review/queue
StandardOutput=null
StandardError=null
[Install]
WantedBy=multi-user.target
'''


def nginx(hostname):
    return f'''server {{
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name {hostname};
    access_log off;
    error_log /dev/null;
    if ($http_host != "{hostname}") {{ return 444; }}
    location ~ ^/\\.well-known/acme-challenge/[A-Za-z0-9_-]+$ {{
        root /var/lib/sro-review-acme/www;
        default_type text/plain;
        limit_except GET {{ deny all; }}
        try_files $uri =404;
    }}
    location / {{ return 444; }}
}}
server {{
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name {hostname};
    ssl_certificate /etc/letsencrypt/live/{hostname}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{hostname}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    access_log off;
    error_log /dev/null;
    if ($http_host != "{hostname}") {{ return 444; }}
    client_max_body_size 1m;
    location / {{
        proxy_pass http://127.0.0.1:8767;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For "";
        proxy_set_header X-Forwarded-Proto "";
        proxy_set_header Forwarded "";
        proxy_buffering off;
        proxy_max_temp_file_size 0;
    }}
}}
'''


def preflight(machine_id, hostname):
    """Read-only refusal gates run before transfer and again before installation."""
    try:
        ipaddress.ip_address(hostname)
        certificate_check = '-checkip'
    except ValueError:
        certificate_check = '-checkhost'
    return f'''set -euo pipefail
umask 077
test "$(id -u)" = 0
test "$(cat /etc/machine-id)" = {machine_id}
test "$(. /etc/os-release; echo "$ID")" = ubuntu
command -v python3 >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 12)'
command -v ss >/dev/null
! ss -H -ltn | awk '{{print $4}}' | grep -Eq ':(80|443|8767)$'
for path in /opt/sro-review /var/lib/sro-review /opt/sro-robot /etc/systemd/system/sro-review.service /etc/nginx; do
    test ! -e "$path" && test ! -L "$path"
done
! getent passwd sro-review >/dev/null
! getent group sro-review >/dev/null
test -f /etc/letsencrypt/live/{hostname}/fullchain.pem
test -f /etc/letsencrypt/live/{hostname}/privkey.pem
openssl x509 -in /etc/letsencrypt/live/{hostname}/fullchain.pem -noout -checkend 86400 >/dev/null
openssl x509 -in /etc/letsencrypt/live/{hostname}/fullchain.pem -noout {certificate_check} {hostname} >/dev/null
'''


def machine_guard(machine_id):
    return f'''set -euo pipefail
test "$(id -u)" = 0
test "$(cat /etc/machine-id)" = {machine_id}
'''


def transport_cleanup(machine_id, transport):
    return machine_guard(machine_id) + 'rm -f -- ' + shlex.quote(transport) + '\n'


def install_script(args, hostname, transport, digest):
    commit = args.commit
    guarded_trap = machine_guard(args.expected_machine_id) + f"trap 'rm -f -- {transport}' EXIT\n"
    return guarded_trap + preflight(args.expected_machine_id, hostname) + f'''
exec 9>/run/lock/sro-review-bootstrap.lock
flock -n 9
test "$(sha256sum {transport} | cut -d' ' -f1)" = {digest}
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y --no-install-recommends python3-venv nginx >/dev/null 2>&1
useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin sro-review
install -d -m 0755 /opt/sro-review
install -d -m 0755 /var/lib/sro-review-acme/www/.well-known/acme-challenge
install -d -m 0700 /var/lib/sro-review
# This is a one-time bootstrap. Never extract over existing remote data.
mkdir -m 0700 /var/lib/sro-review/incoming
tar -xf {transport} -C /var/lib/sro-review/incoming --no-same-owner
mv /var/lib/sro-review/incoming/source /opt/sro-review/{commit}
python3 -m venv /opt/sro-review/{commit}/.venv
/opt/sro-review/{commit}/.venv/bin/pip install --disable-pip-version-check --require-hashes -r /opt/sro-review/{commit}/requirements.txt >/dev/null 2>&1
cd /opt/sro-review/{commit}
.venv/bin/python -c 'from sro_robot.research.review_api import create_app; import uvicorn; from PIL import Image; import pillow_heif'
python3 - <<'VERIFY'
import hashlib, json, sqlite3
from pathlib import Path
root=Path('/var/lib/sro-review/incoming/queue')
manifest=json.loads(Path('/var/lib/sro-review/incoming/bootstrap.json').read_text())
with sqlite3.connect(root/'review.sqlite3') as db:
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert db.execute("SELECT value FROM review_metadata WHERE key='store_identity'").fetchone()[0]==manifest['store_identity']
    rows=db.execute('SELECT DISTINCT asset_sha256,extension FROM documents').fetchall()
    assert len(rows)==manifest['documents']
    for digest, extension in rows:
        assert hashlib.sha256((root/'objects'/(digest+extension)).read_bytes()).hexdigest()==digest
VERIFY
mv /var/lib/sro-review/incoming/queue /var/lib/sro-review/queue
mv /var/lib/sro-review/incoming/login.key /var/lib/sro-review/login.key
chown -R sro-review:sro-review /var/lib/sro-review/queue
chown sro-review:sro-review /var/lib/sro-review/login.key
# Parent traversal is needed for the service user; unrelated users have no access.
chown root:sro-review /var/lib/sro-review
chmod 0710 /var/lib/sro-review
chmod -R go-rwx /var/lib/sro-review/queue
chmod 0600 /var/lib/sro-review/login.key
chmod -R a+rX /opt/sro-review/{commit}
install -m 0644 /var/lib/sro-review/incoming/sro-review.service /etc/systemd/system/sro-review.service
install -m 0644 /var/lib/sro-review/incoming/nginx.conf /etc/nginx/sites-available/sro-review
# nginx was absent at preflight; this link belongs to the newly installed package.
rm -f /etc/nginx/sites-enabled/default
ln -s /etc/nginx/sites-available/sro-review /etc/nginx/sites-enabled/sro-review
nginx -t >/dev/null 2>&1
systemctl daemon-reload
systemctl enable --now sro-review >/dev/null 2>&1
systemctl enable nginx >/dev/null 2>&1
systemctl reload-or-restart nginx
python3 - <<'HEALTH'
import time, urllib.request
for attempt in range(30):
    try:
        request=urllib.request.Request('http://127.0.0.1:8767/login',headers={{'Host':'{hostname}'}})
        with urllib.request.urlopen(request,timeout=2) as response:
            assert response.status==200
        break
    except Exception:
        if attempt==29: raise
        time.sleep(1)
HEALTH
systemctl is-active --quiet sro-review nginx
rm -rf /var/lib/sro-review/incoming
rm -f {transport}
printf 'review_bootstrap_healthy\\n'
'''


@contextmanager
def bundle(args, root, hostname):
    # tempfile defaults to an OS-owned private location outside the checkout.
    with tempfile.TemporaryDirectory(prefix='sro-review-bootstrap-') as temp:
        work = Path(temp)
        if work.is_relative_to(PROJECT):
            raise ValueError('temporary_storage_boundary')
        payload = work / 'payload'
        payload.mkdir(mode=0o700)
        (payload / 'source').mkdir(mode=0o700)
        archive = run(['git', 'archive', '--format=tar', args.commit, '--', *SOURCE_PATHS], cwd=PROJECT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            # Reject every symbolic/hard link; git source has no reason to need one here.
            for member in source.getmembers():
                if not (member.isfile() or member.isdir()) or Path(member.name).is_absolute() or '..' in Path(member.name).parts:
                    raise ValueError('source_archive_boundary')
            source.extractall(payload / 'source', filter='data')
        for path in SOURCE_PATHS:
            if not (payload / 'source' / path).exists():
                raise ValueError('pinned_source_incomplete')
        summary = snapshot_queue(root, payload / 'queue')
        (payload / 'bootstrap.json').write_text(json.dumps(summary))
        (payload / 'login.key').write_bytes(args.token_file.read_bytes())
        (payload / 'login.key').chmod(0o600)
        (payload / 'sro-review.service').write_text(unit(args.commit, args.origin, args.expert))
        (payload / 'nginx.conf').write_text(nginx(hostname))
        package = work / 'bootstrap.tar'
        with tarfile.open(package, 'w') as output:
            for path in sorted(payload.iterdir()):
                output.add(path, arcname=path.name, recursive=True)
        package.chmod(0o600)
        yield package, summary


def send_payload(ssh, args, hostname, package):
    """An uncertain transfer/install is never retried; only its transport is removed."""
    transport = '/tmp/sro-review-' + secrets.token_hex(16) + '.tar'
    try:
        data = package.read_bytes()
        transfer = (machine_guard(args.expected_machine_id)
                    + 'umask 077; set -C; cat > ' + shlex.quote(transport))
        run([*ssh, transfer], input=data, timeout=180)
        script = install_script(args, hostname, transport, hashlib.sha256(data).hexdigest())
        return run([*ssh, 'bash -s'], input=script.encode(), timeout=1200)
    finally:
        # Reconnect only to remove this random private transport, with machine
        # identity checked again. Never retry installation or touch queue state.
        try:
            run([*ssh, 'bash -s'],
                input=transport_cleanup(args.expected_machine_id, transport).encode())
        except (OSError, ValueError, subprocess.SubprocessError):
            print('Private transport cleanup could not be confirmed; inspect the dedicated host privately.', file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='root@dedicated-review-host')
    parser.add_argument('--expected-machine-id', required=True, help='independently verified dedicated host identity')
    parser.add_argument('--origin', required=True, help='exact HTTPS origin; standard port 443')
    parser.add_argument('--queue-root', type=Path, required=True)
    parser.add_argument('--commit', required=True, help='full published develop commit')
    parser.add_argument('--identity-file', type=Path, required=True)
    parser.add_argument('--known-hosts', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--expert', required=True)
    args = parser.parse_args(argv)
    try:
        root, hostname = validate(args)
        # Publication evidence is exact; do not fetch, switch or mutate local refs.
        published = run(['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/develop'], cwd=PROJECT).decode().split()[0]
        if published != args.commit:
            raise ValueError('published_develop_commit_required')
        ssh = ['ssh', '-i', str(args.identity_file), '-o', 'IdentitiesOnly=yes',
               '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'UserKnownHostsFile=' + str(args.known_hosts),
               '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=15',
               '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3', args.host]
        run([*ssh, 'bash -s'], input=preflight(args.expected_machine_id, hostname).encode())
        with bundle(args, root, hostname) as (package, summary):
            result = send_payload(ssh, args, hostname, package)
            if result != b'review_bootstrap_healthy\n':
                raise ValueError('unexpected_health_readback')
            print(json.dumps({'healthy': True, 'commit': args.commit, 'documents': summary['documents']}))
        return 0
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError, tarfile.TarError):
        # SSH/package/application output may include private paths or values.
        print('Review bootstrap stopped. Inspect the dedicated host privately; reconcile before any retry.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
