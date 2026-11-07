#!/usr/bin/env python3
"""Start the private loopback review service and its independently running worker."""
from __future__ import annotations

import argparse
from datetime import date
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
DEFAULT_ROOT = Path.home() / 'Library/Application Support/SRO Robot/review-queue'
DEFAULT_CORPUS = Path.home() / 'Library/Application Support/SRO Robot/ocr-research/corpus-2026-11-07'
DEFAULT_DATASET = Path.home() / 'Library/Application Support/SRO Robot/ocr-evaluation/generation-2026-11-07-100-new-source-custody'


def default_source_strata(today=None):
    """Seed both requested years, including the active month for replenishment."""
    today = today or date.today()
    available = [(year, month) for year in (2025, 2026) for month in range(1, 13)
                 if (year, month) <= (today.year, today.month)]
    preferred = [(year, month) for month in (3, 6, 9) for year in (2025, 2026)]
    ordered = [item for item in preferred if item in available]
    ordered.extend(item for item in available if item not in ordered)
    return [{'year': year, 'month': month, 'category': 'bankruptcy'} for year, month in ordered]


def secret(root):
    path = root / 'session.key'
    if path.is_symlink():
        raise ValueError('private_boundary')
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_hex(32))
    if path.stat().st_mode & 0o077:
        raise ValueError('private_permissions')
    value = path.read_text().strip()
    if len(value) != 64:
        raise ValueError('invalid_session_key')
    return value


def pid_path(root, kind):
    return root / (kind + '-process.json')


def owned(root, kind):
    path = pid_path(root, kind)
    if not path.exists() or path.is_symlink():
        return None
    state = json.loads(path.read_text())
    result = subprocess.run(['ps', '-p', str(state['pid']), '-o', 'command='], capture_output=True, text=True, timeout=3)
    command = result.stdout
    if result.returncode == 0 and str(Path(__file__).resolve()) in command and ('--instance ' + state['instance']) in command:
        return state
    return None


def spawn(args, kind):
    from scripts.dev.quality.court_dataset import write_json
    if owned(args.root, kind):
        return
    instance = secrets.token_hex(16)
    command = [sys.executable, str(Path(__file__).resolve()), kind, '--root', str(args.root),
               '--corpus', str(args.corpus), '--port', str(args.port), '--instance', instance]
    log = args.root / (kind + '.log')
    descriptor = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=descriptor, stderr=descriptor,
                                   start_new_session=True, cwd=PROJECT)
    finally:
        os.close(descriptor)
    write_json(pid_path(args.root, kind), {'pid': process.pid, 'instance': instance, 'port': args.port})


def stop_owned(root):
    # Recovery must work while the database is locked, migrating or unavailable.
    for kind in ('collect', 'worker', 'serve'):
        state = owned(root, kind)
        if state:
            try:
                if os.getpgid(state['pid']) == state['pid']:
                    os.killpg(state['pid'], signal.SIGTERM)
                else:
                    os.kill(state['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass  # The verified process exited between read-back and signal.


def status(args):
    from sro_robot.research.review_store import ReviewStore
    plan_path = args.root / 'source-plan.json'
    plan = json.loads(plan_path.read_text()) if plan_path.is_file() and not plan_path.is_symlink() else {}
    return {'url': f'http://127.0.0.1:{args.port}/',
            'server_running': bool(owned(args.root, 'serve')),
            'worker_running': bool(owned(args.root, 'worker')),
            'collector_running': bool(owned(args.root, 'collect')),
            'collection_enabled': bool(plan.get('enabled')),
            'counts': ReviewStore(args.root).stats()}


def open_browser(args):
    # The bootstrap secret never appears in console output or the final URL.
    url = f'http://127.0.0.1:{args.port}/session?token=' + secret(args.root)
    # Keep review separate from the collector's leased KAD window. Existing
    # owner tabs/drafts are preserved, and the source window is never repurposed.
    script = ('var c=Application("com.google.Chrome");if(!c.running())c.launch();'
              'var w=c.Window().make();w.index=1;w.activeTab.url=' + json.dumps(url) + ';c.activate();')
    subprocess.run(['osascript', '-l', 'JavaScript', '-'], input=script, text=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True)


def import_existing(args, store):
    if not args.dataset.exists():
        return {'imported': 0, 'existing': 0}
    # Producer provenance stays historical; unchanged core permits cache reuse.
    baseline = json.loads((args.dataset / 'baseline.json').read_text())
    revision = baseline['source']['revision']
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('invalid_baseline_identity')
    result = subprocess.run(['git', 'diff', '--quiet', revision, '--', 'sro_robot/core', 'requirements.in', 'requirements.txt'], cwd=PROJECT)
    if result.returncode:
        # Retained feedback must stay accessible after a parser update. Do not
        # import additional old-core caches as current work or replace reviews.
        return {'imported': 0, 'existing_queue_preserved': True, 'cache_import_deferred_core_changed': True}
    return store.import_dataset(args.dataset, limit=10000)


def collector(args):
    descriptor = os.open(args.root / '.collector.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return collector_loop(args)
    finally:
        os.close(descriptor)


def update_source_plan(path, update):
    from scripts.dev.quality.court_dataset import write_json
    descriptor = os.open(path.parent / '.source-plan.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if path.is_symlink():
            raise ValueError('source_plan_boundary')
        plan = json.loads(path.read_text()) if path.exists() else {}
        update(plan)
        write_json(path, plan)
        return plan
    finally:
        os.close(descriptor)


def collector_loop(args):
    from sro_robot.research.review_store import ReviewStore
    from scripts.dev.quality import corpus_acquisition as acquisition
    from scripts.dev.quality.court_dataset import write_json
    store = ReviewStore(args.root)
    plan_path = args.root / 'source-plan.json'
    while True:
        try:
            plan = json.loads(plan_path.read_text()) if plan_path.exists() else {'enabled': False}
            counts = store.stats()
            if not plan.get('enabled') or counts['pending_review'] + counts['processing'] >= int(plan.get('max_backlog', 1000)):
                time.sleep(15); continue
            import shutil
            if shutil.disk_usage(args.root).free < 2 * 1024**3:
                time.sleep(30); continue
            queue = acquisition.load_queue(args.corpus)
            control = queue.get('source_controls', {}).get('kad-browser-pdf', {})
            if control.get('disabled'):
                time.sleep(30); continue
            remaining = max(0, float(control.get('cooldown_until', 0) or 0) - time.time())
            if remaining:
                time.sleep(min(30, remaining)); continue
            from scripts.dev.quality.kad_discovery import ensure_owned_browser, restore_owner_focus
            ensure_owned_browser(args.corpus)
            before = acquisition.status(args.corpus)['complete']
            if sum(i['status'] == 'pending' for i in queue['items']) < 100:
                acquisition.enqueue_cached_candidates(args.corpus, limit=25, preferred_year=None,
                    allowed_case_years={2025, 2026})
            queue = acquisition.load_queue(args.corpus)
            if any(i['status'] == 'pending' and i['channel'] == 'kad-browser-pdf' for i in queue['items']):
                try:
                    outcome = acquisition.run(args.corpus, limit=25)
                finally:
                    restore_owner_focus(args.corpus)
                after = acquisition.status(args.corpus)['complete']
                def cached_finished(current):
                    current.update(last_added=after-before, last_mode='cached-authorized-queue')
                    if outcome['paused']:
                        controls = acquisition.load_queue(args.corpus).get('source_controls', {}).get('kad-browser-pdf', {})
                        retry = int(current.get('queue_retries', 0)) + 1
                        idle = int(current.get('queue_idle_seconds', 0)) + max(0, int(controls.get('cooldown_until', 0) - time.time()) + 1)
                        current.update(queue_retries=retry, queue_idle_seconds=idle)
                        if controls.get('disabled') or retry > 2 or idle > 300:
                            current.update(enabled=False, hold='source_queue_requires_reconciliation')
                    else:
                        current.update(queue_retries=0, queue_idle_seconds=0)
                update_source_plan(plan_path, cached_finished)
                time.sleep(1)
                continue
            strata = plan.get('strata', [])
            if not strata:
                time.sleep(15); continue
            index = int(plan.get('next_index', 0)) % len(strata)
            item = strata[index]
            year, category = int(item['year']), item['category']
            if not 1900 <= year <= 2200 or category not in {'civil', 'bankruptcy'}:
                raise ValueError('invalid_source_plan')
            before = acquisition.status(args.corpus)['complete']
            command = [sys.executable, str(PROJECT / 'scripts/dev/quality/corpus_acquisition_coordinator.py'),
                       '--root', str(args.corpus), '--year', str(year), '--category', category,
                       '--cases', str(min(25, max(1, int(plan.get('cases', 25))))), '--acts-per-case', '1', '--owned-browser']
            if item.get('month') is not None:
                command.extend(['--month', str(int(item['month']))])
            try:
                response = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2100)
            finally:
                restore_owner_focus(args.corpus)
            after = acquisition.status(args.corpus)['complete']
            def finished(current):
                current.update(next_index=index + 1, last_added=after-before, last_exit=response.returncode)
                if response.returncode:
                    current.update(enabled=False, hold='source_pulse_stopped_reconcile_before_resume')
            # Preserve an operator's collection-off during an in-flight pulse.
            update_source_plan(plan_path, finished)
            # The acquisition queue owns request spacing; there is no extra
            # per-pulse penalty while useful work is arriving.
            time.sleep(1 if after > before else 300)
        except Exception as error:
            reason = getattr(error, 'reason', None)
            if reason in {'identity', 'unsupported', 'challenge', 'rate_limited', 'access_denied'} or isinstance(error, subprocess.TimeoutExpired):
                update_source_plan(plan_path, lambda current: current.update(enabled=False, hold='source_requires_reconciliation'))
            write_json(args.root / 'collector-health.json', {'state': 'collector_error', 'updated_at': time.time()})
            time.sleep(30)


def main():
    from scripts.dev.quality.court_dataset import PrivateParser
    parser = PrivateParser(description=__doc__)
    parser.add_argument('command', choices=('start', 'status', 'open', 'stop', 'serve', 'worker', 'collect', 'diagnostics', 'reparse',
                                           'collection-on', 'collection-off', 'export-exposures'))
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--corpus', type=Path, default=DEFAULT_CORPUS)
    parser.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--instance', default='foreground')
    parser.add_argument('--no-worker', action='store_true')
    parser.add_argument('--no-open', action='store_true')
    try:
        args = parser.parse_args()
        if args.command == 'stop':
            raw = args.root.expanduser()
            if raw.is_symlink():
                raise ValueError('private_boundary')
            root = raw.resolve()
            if (not root.is_dir() or root.is_relative_to(PROJECT) or PROJECT.is_relative_to(root)
                    or root.stat().st_mode & 0o077):
                raise ValueError('private_boundary')
            stop_owned(root)
            print(json.dumps({'status': 'owned_process_stop_requested'}))
            return 0
        from sro_robot.research.review_store import ReviewStore
        store = ReviewStore(args.root); args.root = store.root
        if args.command == 'serve':
            import uvicorn
            from sro_robot.research.review_api import create_app
            uvicorn.run(create_app(store, launch_token=secret(args.root), port=args.port),
                        host='127.0.0.1', port=args.port, access_log=False, log_level='critical')
        elif args.command == 'worker':
            from sro_robot.research.review_worker import ReviewWorker
            ReviewWorker(store, args.corpus).run()
        elif args.command == 'collect':
            collector(args)
        elif args.command == 'start':
            imported = import_existing(args, store)
            secret(args.root)
            spawn(args, 'serve')
            if not args.no_worker:
                spawn(args, 'worker'); spawn(args, 'collect')
            for _ in range(50):
                try:
                    session = hmac.new(secret(args.root).encode(), b'sro-local-review-session-v1', hashlib.sha256).hexdigest()
                    request = urllib.request.Request(f'http://127.0.0.1:{args.port}/api/stats', headers={'Cookie': 'sro_local_review=' + session})
                    with urllib.request.urlopen(request, timeout=1) as response:
                        if response.status == 200: break
                except Exception:
                    time.sleep(.1)
            else:
                raise ValueError('local_service_not_ready')
            if not args.no_open:
                open_browser(args)
            print(json.dumps(dict(status(args), imported=imported)))
        elif args.command == 'open':
            open_browser(args)
        elif args.command == 'diagnostics':
            from sro_robot.research.review_diagnostics import diagnostic_packet
            print(json.dumps(diagnostic_packet(store.diagnostics_summary())))
        elif args.command == 'reparse':
            from sro_robot.research.review_reprocess import reprocess_saved_variants
            print(json.dumps(reprocess_saved_variants(store)))
        elif args.command in {'collection-on', 'collection-off'}:
            from scripts.dev.quality.court_dataset import write_json
            path = args.root / 'source-plan.json'
            if path.is_symlink():
                raise ValueError('source_plan_boundary')
            def configure(plan):
                if not plan:
                    plan.update(strata=default_source_strata(),
                                cases=25, max_backlog=1000, next_index=0)
                plan.update(enabled=args.command == 'collection-on')
            plan = update_source_plan(path, configure)
            if plan['enabled']:
                spawn(args, 'collect')
            print(json.dumps({'collection_enabled': plan['enabled']}))
        elif args.command == 'export-exposures':
            from scripts.dev.quality.court_dataset import write_json
            records = store.exposure_records()
            write_json(args.root / 'evaluation-exposure-ledger.json', {'schema': 1, 'exposures': records})
            print(json.dumps({'exposed_documents': len(records), 'export': 'private-evaluation-exposure-ledger'}))
        else:
            print(json.dumps(status(args)))
        return 0
    except Exception:
        print(json.dumps({'status': 'local_review_operation_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
