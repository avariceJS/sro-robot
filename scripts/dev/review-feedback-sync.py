#!/usr/bin/env python3
"""Pull a consistent private reviewer snapshot over SSH; print safe aggregates only."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import select
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
REMOTE_DATABASE = '/var/lib/sro-review/queue/review.sqlite3'
MAX_BYTES = 256 * 1024 * 1024
TIMEOUT = 180
MIRROR_NAME = 'feedback.sqlite3'

# No user-controlled paths or commands enter the remote shell. SQLite's backup
# API includes committed WAL pages without stopping the remote reviewer.
REMOTE_PROGRAM = r'''
import contextlib, os, pathlib, signal, sqlite3, stat, sys, tempfile, time
source = pathlib.Path('/var/lib/sro-review/queue/review.sqlite3')
target = None
def interrupted(*_):
    raise SystemExit(1)
for interruption in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
    signal.signal(interruption, interrupted)
try:
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError()
    fd, target = tempfile.mkstemp(prefix='sro-feedback-', suffix='.sqlite3')
    os.close(fd)
    deadline = time.monotonic() + 120
    def progress(status, remaining, total):
        if time.monotonic() > deadline or os.path.getsize(target) > 268435456:
            raise ValueError()
    with contextlib.closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)) as live:
        with contextlib.closing(sqlite3.connect(target)) as snapshot:
            live.backup(snapshot, pages=256, progress=progress)
            snapshot.execute('PRAGMA journal_mode=DELETE')
    if os.path.getsize(target) > 268435456:
        raise ValueError()
    with open(target, 'rb') as stream:
        while chunk := stream.read(65536):
            sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
except Exception:
    sys.exit(1)
finally:
    if target:
        for suffix in ('', '-wal', '-shm', '-journal'):
            try:
                os.unlink(target + suffix)
            except FileNotFoundError:
                pass
'''


class SyncError(ValueError):
    """A fixed error category, safe to report without private exception text."""


def private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
            or info.st_uid != os.geteuid() or info.st_nlink != 1):
        raise SyncError('private_file_required')


@contextmanager
def readonly(path):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    try:
        yield connection
    finally:
        connection.close()


def inspect_snapshot(path):
    private_file(path)
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise SyncError('snapshot_size_limit')
    with readonly(path) as db:
        if [row[0] for row in db.execute('PRAGMA integrity_check')] != ['ok']:
            raise SyncError('snapshot_integrity_failed')
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise SyncError('snapshot_foreign_key_failed')
        identity = db.execute("SELECT value FROM review_metadata WHERE key='store_identity'").fetchone()
        if identity is None or not re.fullmatch('[0-9a-f]{64}', identity[0]):
            raise SyncError('snapshot_identity_invalid')
        count = db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]
    return identity[0], count


def check_successor(previous, incoming):
    old_identity, old_count = inspect_snapshot(previous)
    identity, count = inspect_snapshot(incoming)
    if identity != old_identity:
        raise SyncError('store_identity_changed')
    if count < old_count:
        raise SyncError('review_history_regressed')
    with readonly(incoming) as db:
        db.execute('ATTACH DATABASE ? AS previous', (previous.as_uri() + '?mode=ro&immutable=1',))
        # Counts alone miss deletion+replacement and changed corrections. These
        # rows are append-only in ReviewStore, including projection and actor.
        for table in ('reviews', 'review_projections', 'review_actors', 'diagnostics'):
            old_columns = [row[1] for row in db.execute(f'PRAGMA previous.table_info({table})')]
            new_columns = [row[1] for row in db.execute(f'PRAGMA main.table_info({table})')]
            if not old_columns or old_columns != new_columns:
                raise SyncError('review_schema_changed')
            # Diagnostic status is mutable workflow state; the source feedback,
            # identity, attribution and original timestamp must remain intact.
            columns = ('id,action_id,document_id,field_name,original_value_json,'
                       'corrected_value_json,reason,parser_identity_json,created_at'
                       if table == 'diagnostics' else '*')
            if db.execute(f'SELECT {columns} FROM previous.{table} '
                          f'EXCEPT SELECT {columns} FROM main.{table} LIMIT 1').fetchone():
                raise SyncError('diagnostic_history_changed' if table == 'diagnostics' else 'review_history_changed')


def packet(path):
    from sro_robot.research.review_store import ReviewStore
    from sro_robot.research.review_diagnostics import diagnostic_packet

    # Reuse the maintained classification and export allowlist, without opening
    # ReviewStore's writable/migrating connection or creating an objects folder.
    class Snapshot(ReviewStore):
        def __init__(self):
            self.database = path

        def connection(self, write=False):
            if write:
                raise SyncError('readonly_snapshot_required')
            return readonly(self.database)

    with readonly(path) as db:
        count = db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]
    return {'reviews': count, **diagnostic_packet(Snapshot().diagnostics_summary())}


def download(host, target):
    if not re.fullmatch(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.:-]*', host):
        raise SyncError('invalid_ssh_destination')
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15',
               '-o', 'ServerAliveCountMax=2', '--', host,
               'python3 -c ' + shlex.quote(REMOTE_PROGRAM)]
    # Never forward remote stderr, banners or binary stdout into diagnostics.
    with target.open('wb') as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + TIMEOUT
        received = 0
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    raise subprocess.TimeoutExpired('ssh', TIMEOUT)
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_BYTES:
                    raise SyncError('snapshot_size_limit')
                output.write(chunk)
            returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if returncode:
                raise subprocess.CalledProcessError(returncode, 'ssh')
            output.flush()
            os.fsync(output.fileno())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


def sync(host, destination, transport=download):
    raw = Path(destination).expanduser()
    if raw.is_symlink():
        raise SyncError('private_destination_required')
    destination = raw.resolve()
    if destination.is_relative_to(PROJECT) or PROJECT.is_relative_to(destination):
        raise SyncError('destination_must_be_outside_checkout')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = destination.stat()
    if info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise SyncError('private_destination_required')
    # A feedback mirror has no originals and must never adopt a live Mac queue.
    if (destination / 'review.sqlite3').exists() or (destination / 'objects').exists():
        raise SyncError('live_queue_is_not_a_mirror')
    lock = destination / '.feedback-sync.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        private_file(lock)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mirror = destination / MIRROR_NAME
        if mirror.exists() or mirror.is_symlink():
            inspect_snapshot(mirror)
        fd, name = tempfile.mkstemp(prefix='.feedback-', suffix='.sqlite3', dir=destination)
        os.close(fd)
        incoming = Path(name)
        try:
            transport(host, incoming)
            inspect_snapshot(incoming)
            if mirror.exists():
                check_successor(mirror, incoming)
            result = packet(incoming)
            os.replace(incoming, mirror)
            directory_fd = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return result
        finally:
            incoming.unlink(missing_ok=True)
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='Known SSH host/alias, optionally user@host')
    parser.add_argument('--destination', required=True, type=Path, help='Private mirror directory outside Git')
    args = parser.parse_args(argv)
    try:
        result = sync(args.host, args.destination)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception:
        # Do not print remote messages, paths, identifiers or SQLite row values.
        print('Feedback sync failed; inspect the private mirror and check SSH, storage and source continuity.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
