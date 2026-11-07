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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

COMMON_FIELDS = {'entity_kind', 'case_number', 'doc_date', 'court', 'doc_type'}
TYPED_FIELDS = {
    'person': COMMON_FIELDS | {'last_name', 'first_name', 'middle_name', 'inn', 'snils', 'address', 'address_kind'},
    'legal_entity': COMMON_FIELDS | {'legal_name', 'short_name', 'legal_form', 'inn', 'ogrn', 'kpp', 'address', 'address_kind'},
    'sole_proprietor': COMMON_FIELDS | {'debtor_name', 'inn', 'ogrnip', 'address', 'address_kind'},
}
ALL_FIELDS = set().union(*TYPED_FIELDS.values()) | {'fio_raw'}


def run(screenshot=None):
    import uvicorn
    from PIL import Image, ImageDraw
    from playwright.sync_api import sync_playwright
    from sro_robot.research.review_store import ReviewStore
    from sro_robot.research.review_api import create_app
    from sro_robot.research.review_schema import FIELDS, EVALUATION_FIELDS

    assert set(FIELDS) == ALL_FIELDS and len(FIELDS) == 20
    assert len(EVALUATION_FIELDS) == 17

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

        def add(number, *, pdf=False, capture_revision='synthetic-browser-e2e', kind='person', observed_extra=None):
            image = Image.new('RGB', (700, 1000), (255, 255 - number * 10, 255))
            ImageDraw.Draw(image).text((40, 40), f'SYNTHETIC COURT DOCUMENT {number}', fill='black')
            path = private / (f'synthetic-{number}.pdf' if pdf else f'synthetic-{number}.png')
            if pdf:
                image.save(path, save_all=True, append_images=[Image.new('RGB', (700, 1000), 'white')])
            else:
                image.save(path)
            key = hashlib.sha256(path.read_bytes()).hexdigest()
            captured = {'input_sha256': key, 'observed_pipeline': {
                'fio_raw': {'value': 'Synthetic Participant Example', 'source': 'debtor_role:v1', 'source_page': 1},
                'last_name': {'value': 'Synthetic', 'source': 'debtor_role:v1', 'source_page': 1},
                'first_name': {'value': 'Participant', 'source': 'debtor_role:v1', 'source_page': 1},
                'middle_name': {'value': 'Example', 'source': 'debtor_role:v1', 'source_page': 1},
                'address': {'value': 'Synthetic address, house 2', 'source_page': 1},
                'case_number': {'value': 'SYNTHETIC CASE'},
                'entity_kind': {'value': kind},
                'review_suggestions': [{'kind': 'entity_candidate', 'copyable_fields': ['legal_name', 'inn'],
                    'fields': {'entity_kind': {'value': 'legal_entity'}, 'legal_name': {'value': 'Synthetic Organization'}, 'inn': {'value': 'synthetic-id'}}}],
                'applicant_evidence': {'name': 'Synthetic Applicant'}},
                'capture': {'accepted_parser_identity': True, 'revision': capture_revision, 'source_dirty': False,
                            'dirty_sha256': 'synthetic', 'runtime': {}, 'completed_ocr': 0 if pdf else 7, 'seconds': 1.2},
                'variants': [f'Synthetic saved reading {number}'],
                'primary_pages': [{'page': page, 'origin': 'embedded-text-weak' if pdf else 'ocr'} for page in range(1, 3 if pdf else 2)]}
            captured['observed_pipeline'].update(observed_extra or {})
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
                page.wait_for_selector('#field-entity_kind')
                def select(target, browser_page=page):
                    for _ in range(5):
                        if target[:12] in browser_page.locator('#document-id').inner_text():
                            return
                        previous = browser_page.locator('#document-id').inner_text()
                        browser_page.click('#skip')
                        browser_page.wait_for_function("(previous) => document.getElementById('document-id').textContent !== previous", arg=previous)
                    raise AssertionError('synthetic_target_not_reached')
                select(first)
                assert page.locator('#fields textarea, #fields select').count() == 20
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
                page.wait_for_selector('#field-entity_kind')
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
                other.wait_for_selector('#field-entity_kind')
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
                page.wait_for_selector('#field-entity_kind')
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

                # Three explicit card types share raw assets/snapshots, but only
                # visible, applicable fields belong to a particular decision.
                context.close()
                stop(running)
                store = ReviewStore(private / 'typed-store')

                def candidate_field(value):
                    return {'value': value, 'confidence': .35, 'source_page': 1, 'source_variant': 0,
                            'source_start': 0, 'source_end': len(value), 'source_excerpt': value}

                person = add(5, observed_extra={
                    'fio_raw': {'value': None}, 'inn': {'value': None}, 'snils': {'value': 'synthetic-person-snils'},
                    'last_name': {'value': None}, 'first_name': {'value': None}, 'middle_name': {'value': None},
                    'legal_name': {'value': 'Hidden raw company'}, 'ogrn': {'value': 'hidden-company-ogrn'},
                    'review_suggestions': [{'kind': 'application_person', 'warning': 'Synthetic unconfirmed person',
                        'copyable_fields': ['last_name', 'first_name', 'middle_name', 'fio_raw', 'inn', 'snils', 'address'],
                        'fields': {name: candidate_field(value) for name, value in {
                            'last_name': 'Synthetic', 'first_name': 'Candidate', 'middle_name': 'Person',
                            'fio_raw': 'Synthetic Candidate Person', 'inn': 'synthetic-person-inn',
                            'snils': 'synthetic-person-snils', 'address': 'Synthetic address, house 2'}.items()}}]})
                legal = add(6, kind='legal_entity', observed_extra={
                    'legal_name': {'value': None}, 'short_name': {'value': 'Synthetic Company'},
                    'legal_form': {'value': 'Synthetic Form'}, 'inn': {'value': None},
                    'ogrn': {'value': None}, 'kpp': {'value': 'synthetic-legal-kpp'},
                    'snils': {'value': 'Hidden raw person snils'}, 'ogrnip': {'value': 'hidden-ip-ogrnip'},
                    'review_suggestions': [{'kind': 'entity_candidate', 'warning': 'Synthetic unconfirmed company',
                        'copyable_fields': ['legal_name', 'inn', 'ogrn', 'kpp', 'address'],
                        'fields': {name: candidate_field(value) for name, value in {
                            'entity_kind': 'legal_entity', 'legal_name': 'Synthetic Legal Company',
                            'inn': 'synthetic-legal-inn', 'ogrn': 'synthetic-legal-ogrn',
                            'kpp': 'synthetic-legal-kpp', 'address': 'Synthetic address, house 2'}.items()}}]})
                proprietor = add(7, kind='sole_proprietor', observed_extra={
                    'fio_raw': {'value': None}, 'debtor_name': {'value': None},
                    'inn': {'value': None}, 'ogrnip': {'value': None},
                    'legal_name': {'value': 'Hidden raw legal company'}, 'kpp': {'value': 'hidden-legal-kpp'},
                    'review_suggestions': [{'kind': 'entity_candidate', 'warning': 'Synthetic unconfirmed proprietor',
                        'copyable_fields': ['fio_raw', 'debtor_name', 'inn', 'ogrnip', 'address'],
                        'fields': {name: candidate_field(value) for name, value in {
                            'entity_kind': 'sole_proprietor', 'fio_raw': 'Synthetic Proprietor',
                            'debtor_name': 'ИП Synthetic Proprietor', 'inn': 'synthetic-ip-inn',
                            'ogrnip': 'synthetic-ip-ogrnip', 'address': 'Synthetic address, house 2'}.items()}}]})
                running = start(store)
                typed_context = browser.new_context(viewport={'width': 1440, 'height': 1050})
                typed = typed_context.new_page()
                typed.on('pageerror', lambda error: errors.append(type(error).__name__))
                typed.goto(base + '/session?token=' + token)
                typed.wait_for_selector('#field-entity_kind')

                def visible_fields():
                    return {name for name in ALL_FIELDS if typed.locator('#field-' + name).is_visible()}

                def assert_type(kind):
                    assert typed.locator('#field-entity_kind').input_value() == kind
                    assert visible_fields() == TYPED_FIELDS[kind]

                select(person, typed)
                assert_type('person')
                assert typed.locator('#field-last_name').input_value() == 'Synthetic'
                assert typed.locator('#field-first_name').input_value() == 'Candidate'
                assert typed.locator('#field-middle_name').input_value() == 'Person'
                assert typed.locator('#field-inn').input_value() == 'synthetic-person-inn'
                projected_person = store.get_document(person, expose=False)
                assert projected_person['original_fields']['fio_raw']['value'] is None
                assert projected_person['review_projection']['fields']['fio_raw']['review_only'] is True
                typed.fill('#field-first_name', 'Retained')
                assert typed.locator('#field-fio_raw').input_value() == 'Synthetic Candidate Person'
                typed.select_option('#field-entity_kind', 'legal_entity')
                assert_type('legal_entity')
                typed.fill('#field-legal_name', 'Synthetic retained legal draft')
                typed.select_option('#field-entity_kind', 'person')
                assert_type('person')
                assert typed.locator('#field-first_name').input_value() == 'Retained'
                assert typed.locator('#field-legal_name').input_value() == 'Synthetic retained legal draft'
                typed.check('#show-all-fields')
                assert visible_fields() == ALL_FIELDS
                typed.uncheck('#show-all-fields')
                assert_type('person')
                typed.fill('#review-comment', 'Synthetic typed person correction')
                typed.click('#reject')
                # Hidden edited values are not silently dropped: the first
                # attempt expands the explicit review scope without committing.
                typed.wait_for_function("() => document.getElementById('show-all-fields').checked")
                assert visible_fields() == ALL_FIELDS
                assert store.get_document(person, expose=False)['status'] == 'pending_review'
                typed.click('#reject')
                typed.wait_for_function("() => document.getElementById('count-rejected').textContent==='1'")
                with store.connection() as db:
                    decision = db.execute('SELECT * FROM reviews WHERE document_id=?', (person,)).fetchone()
                    assert set(json.loads(decision['corrected_fields_json'])) == {'first_name', 'legal_name'}
                    assert json.loads(decision['field_states_json'])['legal_name'] == 'owner-corrected'
                    proposal = db.execute('SELECT projection_json FROM review_projections WHERE action_id=?',
                                          (decision['action_id'],)).fetchone()
                    assert json.loads(proposal[0])['fields']['first_name']['value'] == 'Candidate'
                corrected = store.get_document(person, expose=False)
                assert corrected['fields']['fio_raw']['value'] == 'Synthetic Retained Person'
                assert corrected['original_fields']['first_name']['value'] is None

                select(legal, typed)
                assert_type('legal_entity')
                assert typed.locator('#field-legal_name').input_value() == 'Synthetic Legal Company'
                assert typed.locator('#field-ogrn').input_value() == 'synthetic-legal-ogrn'
                typed.click('#approve')
                typed.wait_for_function("() => document.getElementById('count-approved').textContent==='1'")
                select(proprietor, typed)
                assert_type('sole_proprietor')
                assert typed.locator('#field-ogrnip').input_value() == 'synthetic-ip-ogrnip'
                typed.click('#approve')
                typed.wait_for_function("() => document.getElementById('count-approved').textContent==='2'")
                with store.connection() as db:
                    for document_id, kind, hidden in ((legal, 'legal_entity', ('last_name', 'first_name', 'middle_name', 'fio_raw', 'snils', 'ogrnip')),
                                                      (proprietor, 'sole_proprietor', ('last_name', 'first_name', 'middle_name', 'fio_raw', 'snils', 'legal_name', 'kpp', 'ogrn'))):
                        decision = db.execute('SELECT * FROM reviews WHERE document_id=?', (document_id,)).fetchone()
                        states = json.loads(decision['field_states_json'])
                        assert all(states[field] == 'unreviewed' for field in hidden)
                        scope = db.execute('SELECT reviewed_fields_json FROM review_projections WHERE action_id=?',
                                           (decision['action_id'],)).fetchone()
                        assert set(json.loads(scope['reviewed_fields_json'])) == TYPED_FIELDS[kind]
                assert store.get_document(legal, expose=False)['original_fields']['legal_name']['value'] is None
                assert store.get_document(proprietor, expose=False)['original_fields']['debtor_name']['value'] is None

                # A changed projection must invalidate an old browser action
                # even when raw capture, store, asset and row revision agree.
                versioned = add(8)
                typed.reload()
                typed.wait_for_selector('#field-entity_kind')
                select(versioned, typed)
                before_projection = store.get_document(versioned, expose=False)
                typed.route('**/api/documents/' + versioned + '/review', lambda route: route.abort())
                typed.fill('#field-address', 'Synthetic projection-version draft')
                typed.fill('#review-comment', 'Synthetic projection-version correction')
                typed.click('#reject')
                typed.wait_for_selector('#retry-action:not([hidden])')
                pending = typed.evaluate("() => JSON.parse(sessionStorage.getItem('sro-local-review-pending-v1')).payload")
                typed.unroute('**/api/documents/' + versioned + '/review')
                typed.on('dialog', lambda dialog: dialog.accept())
                stop(running)
                from sro_robot.research import review_projection
                with patch.object(review_projection, 'PROJECTION_VERSION', review_projection.PROJECTION_VERSION + '-synthetic-next'):
                    running = start(store)
                    stale = typed_context.request.post(base + f'/api/documents/{versioned}/review', data=pending,
                                                       headers={'Origin': base})
                    assert stale.status == 409
                    typed.reload()
                    typed.wait_for_selector('#conflict-actions:not([hidden])')
                    assert typed.locator('#field-address').input_value() == 'Synthetic projection-version draft'
                    current_projection = store.get_document(versioned, expose=False)
                    assert current_projection['snapshot_sha256'] != before_projection['snapshot_sha256']
                    assert current_projection['raw_capture_sha256'] == before_projection['raw_capture_sha256']
                    assert current_projection['revision'] == before_projection['revision']
                    assert current_projection['status'] == 'pending_review'
                    assert {record['asset_sha256'] for record in store.exposure_records()} == {person, legal, proprietor, versioned}
                    stop(running)
                typed_context.close()

                # Saved-reading refresh appends a producer generation. An old
                # browser cannot review the replacement, and a concurrent human
                # verdict prevents replacement activation without erasing history.
                store = ReviewStore(private / 'generation-store')
                refreshed, concurrently_reviewed = add(9), add(10)
                running = start(store)
                generation_context = browser.new_context(viewport={'width': 1440, 'height': 1050})
                stale_tab = generation_context.new_page()
                stale_tab.on('pageerror', lambda error: errors.append(type(error).__name__))
                stale_tab.goto(base + '/session?token=' + token)
                stale_tab.wait_for_selector('#field-entity_kind')
                select(refreshed, stale_tab)
                stale_tab.fill('#field-address', 'Synthetic retained stale-generation draft')
                stale_tab.fill('#review-comment', 'Synthetic old producer feedback')
                concurrent_tab = generation_context.new_page()
                concurrent_tab.goto(base + '/')
                concurrent_tab.wait_for_selector('#field-entity_kind')
                select(concurrently_reviewed, concurrent_tab)
                original_documents = {key: store.get_document(key, expose=False)
                                      for key in (refreshed, concurrently_reviewed)}
                with store.connection() as db:
                    original_captures = {row['id']: row['capture_json'] for row in db.execute('SELECT id,capture_json FROM documents')}

                def civil_party(role, name, identifier):
                    return {'kind': 'entity_participant', 'role': role,
                            'role_location': {'source_variant': 0, 'source_page': 1, 'source_start': 0, 'source_end': 9},
                            'copyable_fields': ['legal_name', 'ogrn'],
                            'fields': {key: candidate_field(value) for key, value in {
                                'entity_kind': 'legal_entity', 'legal_name': name, 'ogrn': identifier}.items()}}

                parser_entered, release_parser = threading.Event(), threading.Event()
                refresh_result = {}
                producer = {'revision': 'e' * 40, 'source_dirty': False, 'dirty_sha256': 'f' * 64}

                def saved_parser(variants):
                    from sro_robot.core.entity_extract import EntityDebtorCard
                    if variants == ['Synthetic saved reading 10']:
                        parser_entered.set()
                        if not release_parser.wait(10):
                            raise ValueError('synthetic_concurrent_review_timeout')
                    card = EntityDebtorCard()
                    card.review_suggestions = [
                        civil_party('plaintiff', 'Synthetic Civil Claimant', 'synthetic-plaintiff-ogrn'),
                        civil_party('defendant', 'Synthetic Civil Defendant', 'synthetic-defendant-ogrn')]
                    return card

                def refresh_saved():
                    from sro_robot.research.review_reprocess import reprocess_saved_variants
                    try:
                        refresh_result.update(reprocess_saved_variants(store, parser=saved_parser, identity=lambda: producer))
                    except BaseException as error:
                        refresh_result['failure'] = type(error).__name__

                refresh_thread = threading.Thread(target=refresh_saved, daemon=True)
                try:
                    with patch('sro_robot.core.extract.parse_file', side_effect=AssertionError('OCR/file parsing must not run')):
                        refresh_thread.start()
                        assert parser_entered.wait(10)
                        concurrent_tab.fill('#field-address', 'Synthetic concurrent owner correction')
                        concurrent_tab.fill('#review-comment', 'Synthetic owner verdict during refresh')
                        concurrent_tab.click('#reject')
                        concurrent_tab.wait_for_function("() => document.getElementById('count-rejected').textContent==='1'")
                        with store.connection() as db:
                            committed_review = dict(db.execute('SELECT * FROM reviews WHERE document_id=?', (concurrently_reviewed,)).fetchone())
                            committed_projection = dict(db.execute('SELECT * FROM review_projections WHERE action_id=?',
                                                                    (committed_review['action_id'],)).fetchone())
                        release_parser.set()
                        refresh_thread.join(10)
                finally:
                    release_parser.set()
                    refresh_thread.join(10)
                assert not refresh_thread.is_alive() and 'failure' not in refresh_result
                assert refresh_result['activated'] == 1 and refresh_result['preserved_reviewed'] == 1
                assert refresh_result['ocr_calls'] == 0 and refresh_result['errors'] == 0
                for key in (refreshed, concurrently_reviewed):
                    with store.connection() as db:
                        assert db.execute('SELECT capture_json FROM documents WHERE id=?', (key,)).fetchone()[0] == original_captures[key]
                    assert len(store.capture_lineage(key)['generations']) == 2
                current = store.get_document(refreshed, expose=False)
                assert current['revision'] == original_documents[refreshed]['revision'] + 1
                assert current['original_capture_sha256'] == original_documents[refreshed]['raw_capture_sha256']
                assert current['raw_capture_sha256'] != original_documents[refreshed]['raw_capture_sha256']
                kept = store.get_document(concurrently_reviewed, expose=False)
                assert kept['status'] == 'rejected' and kept['fields']['address']['value'] == 'Synthetic concurrent owner correction'
                assert kept['raw_capture_sha256'] == original_documents[concurrently_reviewed]['raw_capture_sha256']
                with store.connection() as db:
                    assert dict(db.execute('SELECT * FROM reviews WHERE document_id=?', (concurrently_reviewed,)).fetchone()) == committed_review
                    assert dict(db.execute('SELECT * FROM review_projections WHERE action_id=?',
                                           (committed_review['action_id'],)).fetchone()) == committed_projection
                    active = store._active_row(db, db.execute('SELECT * FROM documents WHERE id=?', (refreshed,)).fetchone())
                    active_capture = json.loads(active['capture_json'])
                    assert active_capture['variants'] == json.loads(original_captures[refreshed])['variants']
                    assert active_capture['capture']['completed_ocr'] == 0
                    assert active_capture['capture']['reprocess']['method'] == 'saved-variants-no-ocr'
                    assert active_capture['capture']['reprocess']['reading_producer']['completed_ocr'] == 7
                stale_tab.click('#reject')
                stale_tab.wait_for_selector('#conflict-actions:not([hidden])')
                assert stale_tab.locator('#field-address').input_value() == 'Synthetic retained stale-generation draft'
                stale_tab.on('dialog', lambda dialog: dialog.accept())
                stale_tab.click('#conflict-next')
                stale_tab.wait_for_selector('#field-legal_name:visible')
                assert stale_tab.locator('#field-entity_kind').input_value() == 'legal_entity'
                assert stale_tab.locator('#field-legal_name').input_value() == 'Synthetic Civil Defendant'
                assert stale_tab.locator('#field-ogrn').input_value() == 'synthetic-defendant-ogrn'
                assert 'Ответчик' in stale_tab.locator('#subject-role').inner_text()
                assert 'ответчика' in stale_tab.locator('label[for="field-legal_name"]').inner_text()
                assert not current['review_projection']['subject_is_debtor']

                sole_party = add(11, kind=None, observed_extra={
                    **{name: {'value': None} for name in ('fio_raw', 'last_name', 'first_name', 'middle_name', 'address')},
                    'review_suggestions': [civil_party('plaintiff', 'Synthetic Sole Claimant', 'synthetic-sole-ogrn')]})
                select(sole_party, stale_tab)
                assert stale_tab.locator('#field-legal_name').input_value() == 'Synthetic Sole Claimant'
                assert 'Истец' in stale_tab.locator('#subject-role').inner_text()
                assert not store.get_document(sole_party, expose=False)['review_projection']['subject_is_debtor']
                assert {record['asset_sha256'] for record in store.exposure_records()} == {refreshed, concurrently_reviewed, sole_party}
                generation_context.close()
                stop(running)
                assert not errors
                browser.close()
        finally:
            for pair in servers:
                if pair[1].is_alive():
                    stop(pair)
    return {'status': 'passed', 'synthetic_documents': 11, 'fields': 20, 'evaluation_fields': 17, 'approved': 4,
            'rejected_with_correction': 3, 'skip_preserves_pending': True,
            'live_counter': True, 'tab_conflict_preserves_draft': True,
            'service_restart_persists_reviews': True, 'cross_store_snapshot_mismatch': True, 'pdf_range': True, 'photo_zoom': True,
            'entity_types': 3, 'candidate_projection': True, 'typed_scopes': True, 'type_switch_retains_drafts': True,
            'hidden_fields_not_confirmed': True, 'projection_version_conflict': True,
            'name_components_preserve_canonical_fio': True, 'civil_roles_not_debtor': True,
            'saved_variant_refresh_without_ocr': True, 'capture_lineage_preserved': True,
            'concurrent_owner_review_not_replaced': True, 'old_generation_tab_conflict': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshot', type=Path, help='Optional synthetic-only screenshot')
    args = parser.parse_args()
    result = run(args.screenshot)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
