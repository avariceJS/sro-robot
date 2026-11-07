#!/usr/bin/env python3
"""Real-browser local review integration using disposable synthetic documents only.

Never connects to the owner's service/corpus or accepts a real-data root. Run from
any directory with the existing project environment and installed Playwright.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def run(screenshot=None):
    import uvicorn
    from PIL import Image, ImageDraw
    from playwright.sync_api import sync_playwright
    from sro_robot.research.review_store import ReviewStore
    from sro_robot.research.review_api import create_app

    with tempfile.TemporaryDirectory(prefix='sro-local-review-e2e-') as temporary:
        private = Path(temporary)
        store = ReviewStore(private / 'store')
        token = secrets.token_urlsafe(32)
        with socket.socket() as available:
            available.bind(('127.0.0.1', 0))
            port = available.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        servers = []

        def start(current_store):
            server = uvicorn.Server(uvicorn.Config(create_app(current_store, launch_token=token, port=port), host='127.0.0.1', port=port, log_level='critical', access_log=False))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.02)
            if not server.started:
                raise RuntimeError('synthetic_service_start_failed')
            servers.append((server, thread))
            return server, thread

        def stop(pair):
            pair[0].should_exit = True
            pair[1].join(10)
            if pair[1].is_alive():
                raise RuntimeError('synthetic_service_shutdown_failed')

        def add(number, *, pdf=False, capture_revision='synthetic-browser-e2e'):
            image = Image.new('RGB', (700, 1000), (255, 255 - number * 10, 255))
            ImageDraw.Draw(image).text((40, 40), f'SYNTHETIC COURT DOCUMENT {number}', fill='black')
            path = private / (f'synthetic-{number}.pdf' if pdf else f'synthetic-{number}.png')
            if pdf:
                image.save(path, save_all=True, append_images=[Image.new('RGB', (700, 1000), 'white')])
            else:
                image.save(path)
            key = hashlib.sha256(path.read_bytes()).hexdigest()
            captured = {'input_sha256': key, 'observed_pipeline': {
                'fio_raw': {'value': 'Synthetic Participant', 'source': 'debtor_role:v1', 'source_page': 1},
                'address': {'value': 'Synthetic address, house 2', 'source_page': 1},
                'case_number': {'value': 'SYNTHETIC CASE'},
                'review_suggestions': [{'kind': 'entity', 'fields': {'legal_name': {'value': 'Synthetic Organization'}, 'inn': {'value': 'synthetic-id'}}}],
                'applicant_evidence': {'name': 'Synthetic Applicant'}},
                'capture': {'accepted_parser_identity': True, 'revision': capture_revision, 'source_dirty': False, 'dirty_sha256': 'synthetic', 'runtime': {}},
                'primary_pages': [{'page': page, 'origin': 'embedded-text-weak' if pdf else 'ocr'} for page in range(1, 3 if pdf else 2)]}
            store.import_capture(path, captured, {'synthetic': True})
            return key

        first, second, third = add(1), add(2, pdf=True), add(3)
        running = start(store)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1050})
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(type(error).__name__))
                page.goto(base + '/session?token=' + token)
                page.wait_for_selector('#field-fio_raw')
                def select(target, browser_page=page):
                    for _ in range(5):
                        if target[:12] in browser_page.locator('#document-id').inner_text():
                            return
                        previous = browser_page.locator('#document-id').inner_text()
                        browser_page.click('#skip')
                        browser_page.wait_for_function("(previous) => document.getElementById('document-id').textContent !== previous", arg=previous)
                    raise AssertionError('synthetic_target_not_reached')
                select(first)
                assert page.locator('#fields textarea').count() == 17
                assert page.locator('#count-pending_review').inner_text() == '3'
                page.wait_for_selector('#document-photo', state='visible')
                page.click('#zoom-in')
                assert page.locator('#zoom-reset').inner_text() == '125%'
                page.click('#participant-context summary')
                assert 'Synthetic Organization' in page.locator('#participants').inner_text()
                if screenshot:
                    screenshot = Path(screenshot).resolve()
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(screenshot))
                page.click('#approve')
                page.wait_for_function("() => document.getElementById('count-approved').textContent==='1'")
                page.wait_for_selector('#field-fio_raw')
                select(second)
                assert page.locator('#document-pdf').is_visible()
                page.click('#next-page')
                assert 'page=2' in page.locator('#document-pdf').get_attribute('src')
                response = context.request.get(base + f'/api/documents/{second}/original', headers={'Range': 'bytes=0-20'})
                assert response.status == 206 and response.body().startswith(b'%PDF')
                page.fill('#field-address', 'Synthetic address, house 2, unit 9')
                page.fill('#review-comment', 'Synthetic missing apartment')
                page.click('#reject')
                page.wait_for_function("() => document.getElementById('count-rejected').textContent==='1'")
                page.wait_for_selector('#document-photo', state='visible')
                assert store.get_document(second, expose=False)['fields']['address']['value'].endswith('unit 9')
                assert store.get_document(second, expose=False)['original_fields']['address']['value'] == 'Synthetic address, house 2'

                fourth = add(4)
                page.wait_for_function("() => document.getElementById('count-pending_review').textContent==='2'", timeout=6000)
                page.click('#skip')
                page.wait_for_function("(prefix) => document.getElementById('document-id').textContent.includes(prefix)", arg=fourth[:12])
                assert store.get_document(third, expose=False)['status'] == 'pending_review'
                other = context.new_page()
                other.goto(base + '/')
                other.wait_for_selector('#field-fio_raw')
                select(fourth, other)
                other.fill('#field-address', 'Synthetic conflicting correction')
                other.fill('#review-comment', 'Synthetic conflict reason')
                page.click('#approve')
                page.wait_for_function("() => document.getElementById('count-approved').textContent==='2'")
                other.click('#reject')
                other.wait_for_selector('#conflict-actions:not([hidden])')
                assert other.locator('#field-address').input_value() == 'Synthetic conflicting correction'
                assert store.get_document(fourth, expose=False)['status'] == 'approved'
                other.click('#conflict-next')
                other.wait_for_function("(prefix) => document.getElementById('document-id').textContent.includes(prefix)", arg=third[:12])
                stop(running)
                restarted = ReviewStore(private / 'store')
                running = start(restarted)
                page.reload()
                page.wait_for_selector('#field-fio_raw')
                assert page.locator('#count-approved').inner_text() == '2'
                assert page.locator('#count-rejected').inner_text() == '1'
                assert restarted.stats()['pending_review'] == 1
                assert restarted.get_document(second, expose=False)['fields']['address']['value'].endswith('unit 9')
                with restarted.connection() as db:
                    reviews = db.execute('SELECT * FROM reviews ORDER BY reviewed_at').fetchall()
                    assert len(reviews) == 3
                    assert json.loads(reviews[0]['field_states_json'])['inn'] == 'unreviewed'
                    rejected = next(row for row in reviews if row['decision'] == 'rejected')
                    assert rejected['comment'] == 'Synthetic missing apartment'
                    assert json.loads(rejected['parser_identity_json'])['revision'] == 'synthetic-browser-e2e'
                # An unacknowledged action must not cross into another private
                # database that happens to share the port, asset and revision.
                select(third)
                page.route('**/api/documents/' + third + '/review', lambda route: route.abort())
                page.fill('#field-address', 'Synthetic retained draft')
                page.fill('#review-comment', 'Synthetic uncertain request')
                page.click('#reject')
                page.wait_for_selector('#retry-action:not([hidden])')
                page.unroute('**/api/documents/' + third + '/review')
                page.on('dialog', lambda dialog: dialog.accept())
                stop(running)
                store = ReviewStore(private / 'other-store')
                assert add(3, capture_revision='synthetic-different-capture') == third
                running = start(store)
                page.reload()
                page.wait_for_selector('#conflict-actions:not([hidden])')
                assert page.locator('#field-address').input_value() == 'Synthetic retained draft'
                assert store.stats()['approved'] == 0 and store.stats()['rejected'] == 0
                assert store.stats()['pending_review'] == 1
                assert not errors
                browser.close()
        finally:
            for pair in servers:
                if pair[1].is_alive():
                    stop(pair)
    return {'status': 'passed', 'synthetic_documents': 4, 'fields': 17, 'approved': 2,
            'rejected_with_correction': 1, 'skip_preserves_pending': True,
            'live_counter': True, 'tab_conflict_preserves_draft': True,
            'service_restart_persists_reviews': True, 'cross_store_snapshot_mismatch': True, 'pdf_range': True, 'photo_zoom': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshot', type=Path, help='Optional synthetic-only screenshot')
    args = parser.parse_args()
    result = run(args.screenshot)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
