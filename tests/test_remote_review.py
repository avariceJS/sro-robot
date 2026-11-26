"""Synthetic HTTPS review authentication, identity and private-launch boundaries."""
import asyncio
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from sro_robot.core.extract import add_review_candidates, parse_variants_for_review
from sro_robot.research.review_api import create_app, REMOTE_COOKIE, remote_origin
from sro_robot.research.review_projection import EXPERT_FIELDS, expert_projection
from sro_robot.research.review_store import ReviewStore, ReviewConflict
from tests.test_local_review import ReviewFixture

ORIGIN = 'https://review.example.test'
TOKEN = 'synthetic-remote-password-' + 'a' * 40
EXPERT = 'synthetic-expert'
SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/dev/review-remote.py'
spec = importlib.util.spec_from_file_location('remote_review_launcher_test', SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class RemoteReviewTests(ReviewFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        self.client = TestClient(self.app, base_url=ORIGIN)

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def make_app(self, **kwargs):
        return create_app(self.store, launch_token=TOKEN, public_origin=ORIGIN,
                          expert_identity=kwargs.pop('expert_identity', EXPERT), session_seconds=60, **kwargs)

    def login(self, **kwargs):
        return self.client.post('/login', data={'username': EXPERT, 'password': TOKEN},
                                headers={'Origin': ORIGIN}, follow_redirects=False, **kwargs)

    def expert_card(self, *, missing_snils=False, suffix='test', variants=None):
        raw = ('Р Е Ш Е Н И Е\nДело № А99-123/2099\n     25.11.2099\n'
               'Резолютивная часть объявлена 24.11.2099.\n'
               'Рассмотрев заявление о признании гражданина несостоятельным (банкротом).\nУСТАНОВИЛ:\n'
               'РЕШИЛ:\nПризнать Макетову Анну Петровну (дата рождения: 01.01.1980; '
               'адрес регистрации:\n999999, Россия, Макетная область, д. Условная, д. 17; '
               'СНИЛС: 112-\n233-445-95; ИНН: 123456789047) несостоятельной (банкротом).')
        if missing_snils:
            raw = raw.replace('СНИЛС: 112-\n233-445-95; ', '')
        variants = [raw] if variants is None else variants
        recognized = parse_variants_for_review(variants)
        add_review_candidates(recognized, variants)
        path = Path(self.temp.name) / f'expert-{suffix}.pdf'
        path.write_bytes(b'%PDF-1.4\n' + suffix.encode() + b'\n%%EOF\n')
        key = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.import_capture(path, dict(self.captured, input_sha256=key, variants=variants,
                                             observed_pipeline=asdict(recognized)))
        return key

    def verdict(self, key, **edits):
        doc = self.store.get_document(key, expose=False, expert=True)
        payload = self.payload(expected_revision=doc['revision'], expected_snapshot_sha256=doc['snapshot_sha256'],
                               reviewed_fields=doc['review_projection']['applicable_fields'])
        return dict(payload, **edits)

    def test_no_secret_url_bootstrap_and_login_form_uses_password_post(self):
        self.assertEqual(self.client.get('/', follow_redirects=False).headers['location'], '/login')
        self.assertEqual(self.client.get('/api/stats').status_code, 401)
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['referrer-policy'], 'same-origin')
        self.assertIn('type="password"', response.text)
        self.assertIn('method="post"', response.text)
        self.assertNotIn(TOKEN, response.text)
        self.assertEqual(self.client.get('/session', params={'token': TOKEN}).status_code, 404)
        self.assertEqual(self.client.get('/api/stats').status_code, 401)

    def test_pdf_preview_requires_session_and_returns_image_with_no_store(self):
        key = self.expert_card()
        endpoint = f'/api/documents/{key}/preview/1'
        png = b'\x89PNG\r\n\x1a\n'
        with patch('app.services.pdf_preview.render_pdf_preview', return_value=(png, 1)) as render:
            self.assertEqual(self.client.get(endpoint).status_code, 401)
            render.assert_not_called()
            self.assertEqual(self.login().status_code, 303)
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['content-type'], 'image/png')
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.content, png)
            self.assertEqual(render.call_args.kwargs, {'page_number': 1, 'max_pages': 80})

    def test_secure_finite_cookie_restart_expiry_and_tampering(self):
        with patch('sro_robot.research.review_api.time.time', return_value=1000):
            response = self.login()
            self.assertEqual(response.status_code, 303)
            cookie = response.headers['set-cookie']
            for attribute in ('Secure', 'HttpOnly', 'SameSite=strict', 'Max-Age=60', 'Path=/'):
                self.assertIn(attribute, cookie)
            restarted = TestClient(self.make_app(), base_url=ORIGIN)
            restarted.cookies.update(self.client.cookies)
            self.assertEqual(restarted.get('/api/stats').status_code, 200)
            self.assertEqual(restarted.get('/').headers['strict-transport-security'], 'max-age=31536000')
        with patch('sro_robot.research.review_api.time.time', return_value=1060):
            self.assertEqual(restarted.get('/api/stats').status_code, 401)
        with patch('sro_robot.research.review_api.time.time', return_value=999):
            self.assertEqual(restarted.get('/api/stats').status_code, 401)
        with patch('sro_robot.research.review_api.time.time', return_value=1000):
            value = self.client.cookies.get(REMOTE_COOKIE)
            restarted.cookies.clear()
            restarted.cookies.set(REMOTE_COOKIE, value[:-1] + ('0' if value[-1] != '0' else '1'))
            self.assertEqual(restarted.get('/api/stats').status_code, 401)
        restarted.close()

    def test_exact_host_and_origin_ignores_forwarded_headers_and_rejects_duplicates(self):
        self.login()
        for headers in ({'Host': 'foreign.test', 'X-Forwarded-Host': 'review.example.test', 'X-Forwarded-Proto': 'https'},
                        {'Host': '127.0.0.1:8767', 'Forwarded': 'host=review.example.test;proto=https'},
                        {'Origin': 'https://review.example.test.attacker.test'},
                        {'Origin': 'http://review.example.test'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.get('/api/stats', headers=headers).status_code, 403)
        self.assertEqual(self.client.get('/api/stats', headers=[('Host', 'review.example.test'), ('Host', 'foreign.test')]).status_code, 403)
        self.assertEqual(self.client.get('/api/stats', headers=[('Origin', ORIGIN), ('Origin', ORIGIN)]).status_code, 403)
        for origin in (None, 'https://foreign.test'):
            headers = {} if origin is None else {'Origin': origin}
            self.assertEqual(self.client.post('/api/documents/' + self.key + '/review', json=self.payload(), headers=headers).status_code, 403)
        self.assertEqual(self.store.stats()['approved'], 0)

    def test_login_invalid_credentials_size_and_global_bounded_throttle(self):
        self.assertEqual(self.client.post('/login', data={'username': EXPERT, 'password': TOKEN}).status_code, 403)
        self.assertEqual(self.client.post('/login', data={'username': EXPERT, 'password': TOKEN}, headers={'Origin': 'null'}).status_code, 403)
        response = self.login()
        self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
        self.assertEqual(self.client.get('/').headers['referrer-policy'], 'no-referrer')
        self.assertEqual(self.client.post('/login', content=b'x' * 4097, headers={'Origin': ORIGIN}).status_code, 413)
        for _ in range(10):
            result = self.client.post('/login', data={'username': EXPERT, 'password': 'synthetic-wrong'}, headers={'Origin': ORIGIN})
            self.assertEqual(result.status_code, 401)
            self.assertNotIn(TOKEN, result.text)
        response = self.login()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '60')

    def test_concurrent_streaming_logins_share_failure_budget(self):
        async def exercise():
            # Direct ASGI requests suspend every body reader until all twenty
            # have passed the pre-body gate, reproducing the real streaming race.
            ready = 0
            release = asyncio.Event()
            async def attempt():
                nonlocal ready
                status = None
                received = False
                response_done = asyncio.Event()
                async def receive():
                    nonlocal ready, received
                    if not received:
                        received = True
                        ready += 1
                        if ready == 20:
                            release.set()
                        await release.wait()
                        return {'type': 'http.request', 'body': urlencode({'username': EXPERT, 'password': 'synthetic-wrong'}).encode(), 'more_body': False}
                    await response_done.wait()
                    return {'type': 'http.disconnect'}
                async def send(message):
                    nonlocal status
                    if message['type'] == 'http.response.start':
                        status = message['status']
                    if message['type'] == 'http.response.body' and not message.get('more_body', False):
                        response_done.set()
                scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                         'method': 'POST', 'scheme': 'https', 'path': '/login', 'raw_path': b'/login',
                         'query_string': b'', 'root_path': '', 'server': ('review.example.test', 443),
                         'client': ('127.0.0.1', 1000), 'headers': [(b'host', b'review.example.test'),
                         (b'origin', ORIGIN.encode()), (b'content-type', b'application/x-www-form-urlencoded')]}
                await self.app(scope, receive, send)
                return status
            return await asyncio.wait_for(asyncio.gather(*(attempt() for _ in range(20))), timeout=5)
        statuses = asyncio.run(exercise())
        self.assertEqual(statuses.count(401), 10)
        self.assertEqual(statuses.count(429), 10)
        self.assertEqual(self.login().status_code, 429)

    def test_account_identity_is_session_bound_and_recorded_transactionally(self):
        self.login()
        other = TestClient(self.make_app(expert_identity='different-expert'), base_url=ORIGIN)
        other.cookies.update(self.client.cookies)
        self.assertEqual(other.get('/api/stats').status_code, 401)
        other.close()
        other_origin = 'https://other-review.example.test'
        other = TestClient(create_app(self.store, launch_token=TOKEN, public_origin=other_origin,
                                     expert_identity=EXPERT, session_seconds=60), base_url=other_origin)
        other.cookies.set(REMOTE_COOKIE, self.client.cookies.get(REMOTE_COOKIE))
        self.assertEqual(other.get('/api/stats').status_code, 401)
        other.close()
        key = self.expert_card()
        payload = self.verdict(key)
        forged = dict(payload, actor='client-selected-identity')
        result = self.client.post('/api/documents/' + key + '/review', json=forged, headers={'Origin': ORIGIN})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(self.store.stats()['approved'], 0)
        result = self.client.post('/api/documents/' + key + '/review', json=payload, headers={'Origin': ORIGIN})
        self.assertEqual(result.status_code, 200)
        restarted = ReviewStore(self.root)
        with restarted.connection() as db:
            self.assertEqual(db.execute('SELECT actor FROM review_actors').fetchone()[0], EXPERT)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 1)
        self.assertTrue(restarted.apply_review(key, payload, actor=EXPERT, expert=True)['replayed'])
        with self.assertRaisesRegex(ReviewConflict, 'action_actor_conflict'):
            restarted.apply_review(key, payload, actor='different-expert', expert=True)
        with self.assertRaisesRegex(ReviewConflict, 'action_actor_conflict'):
            restarted.apply_review(key, payload, expert=True)

    def test_expert_queue_has_only_complete_cards_and_records_rejection_reason(self):
        self.login()
        complete = self.expert_card(suffix='complete')
        incomplete = self.expert_card(missing_snils=True, suffix='incomplete')
        unrelated = self.expert_card(suffix='civil', variants=['РЕШЕНИЕ\nДело № А99-345/2099\nО взыскании оплаты.'])
        ready = self.client.get('/api/next').json()
        self.assertEqual(ready['stats']['pending_review'], 1)
        self.assertEqual(ready['stats']['needs_extraction'], 2)
        self.assertEqual(ready['stats']['outside_scope'], 1)
        self.assertEqual(ready['document']['id'], complete)
        self.assertEqual(ready['document']['review_projection']['applicable_fields'], list(EXPERT_FIELDS['person']))
        self.assertEqual(tuple(ready['document']['fields']), EXPERT_FIELDS['person'])
        self.assertTrue(all(field['value'] for field in ready['document']['fields'].values()))
        self.assertNotIn('observed_pipeline', ready['document'])
        self.assertNotIn('candidates', ready['document']['review_projection'])
        with self.store.connection() as db:
            raw = json.loads(db.execute('SELECT capture_json FROM documents WHERE id=?', (complete,)).fetchone()[0])['variants'][0]
        date = ready['document']['fields']['doc_date']
        self.assertEqual(raw[date['source_start']:date['source_end']], '25.11.2099')
        for key in (incomplete, unrelated):
            self.assertEqual(self.client.get(f'/api/documents/{key}').status_code, 404)
            self.assertEqual(self.client.get(f'/api/documents/{key}/original').status_code, 404)
            self.assertEqual(self.client.get(f'/api/documents/{key}/preview/1').status_code, 404)
        url = f'/api/documents/{complete}/review'
        reason = 'Synthetic: the parsed registration address omits a room number.'
        verdict = self.verdict(complete, decision='rejected', comment=reason)
        self.assertEqual(self.client.post(url, json=dict(verdict, reviewed_fields=['inn']),
                                          headers={'Origin': ORIGIN}).status_code, 409)
        self.assertEqual(self.client.post(url, json=dict(verdict, corrected_fields={'inn': '000000000000'}, incorrect_fields=['inn']),
                                          headers={'Origin': ORIGIN}).status_code, 422)
        for missing_reason in ('', '  \n '):
            response = self.client.post(url, json=dict(verdict, comment=missing_reason), headers={'Origin': ORIGIN})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()['detail'], 'rejection_needs_reason')
        self.assertEqual(self.client.post(url, json=verdict, headers={'Origin': ORIGIN}).status_code, 200)
        self.assertTrue(self.client.post(url, json=verdict, headers={'Origin': ORIGIN}).json()['replayed'])
        self.assertEqual(self.client.post(url, json=dict(verdict, comment='Changed comment'), headers={'Origin': ORIGIN}).status_code, 409)
        self.assertIsNone(self.client.get('/api/next').json()['document'])
        with self.store.connection() as db:
            review = db.execute('SELECT comment,corrected_fields_json FROM reviews').fetchone()
            self.assertEqual(tuple(review), (reason, '{}'))
            self.assertEqual(tuple(db.execute('SELECT field_name,reason FROM diagnostics').fetchone()), ('_document', reason))

    def test_person_verdict_covers_only_extracted_address_fields(self):
        self.login()
        seed = self.expert_card(suffix='optional-address-seed')
        with self.store.connection() as db:
            raw = json.loads(db.execute('SELECT capture_json FROM documents WHERE id=?', (seed,)).fetchone()[0])['variants'][0]
        original = 'адрес регистрации:\n999999, Россия, Макетная область, д. Условная, д. 17; '
        partial = 'г. Тестоград, ул. Макетная, д. 7'
        for index, (replacement, expected_address) in enumerate((
            ('', None),
            ('адрес регистрации: г. Тестоград, ул. Учебная; ', None),
            (f'адрес регистрации: {partial}, кв. неизвестна; ', partial),
            (f'адрес проживания: {partial}; ', None),
        )):
            with self.subTest(replacement=replacement):
                text = raw.replace(original, replacement)
                key = self.expert_card(suffix=f'optional-address-{index}', variants=[text])
                document = self.client.get(f'/api/documents/{key}').json()['document']
                scope = ['fio_raw', 'inn', 'snils'] + (['address'] if expected_address else []) + ['case_number', 'doc_date']
                self.assertEqual(document['review_projection']['applicable_fields'], scope)
                self.assertEqual(list(document['fields']), scope)
                if expected_address is None:
                    self.assertNotIn('address', document['fields'])
                else:
                    address = document['fields']['address']
                    self.assertEqual(address['value'], expected_address)
                    self.assertEqual(text[address['source_start']:address['source_end']], expected_address)
                payload = self.verdict(key, action_id=f'optional-address-action-{index}')
                wrong_scope = scope + ['address'] if expected_address is None else [name for name in scope if name != 'address']
                self.assertEqual(self.client.post(f'/api/documents/{key}/review',
                    json=dict(payload, reviewed_fields=wrong_scope), headers={'Origin': ORIGIN}).status_code, 409)
                self.assertEqual(self.client.post(f'/api/documents/{key}/review',
                    json=payload, headers={'Origin': ORIGIN}).status_code, 200)
                with self.store.connection() as db:
                    saved_scope = json.loads(db.execute('SELECT reviewed_fields_json FROM review_projections WHERE action_id=?',
                                                       (payload['action_id'],)).fetchone()[0])
                    states = json.loads(db.execute('SELECT field_states_json FROM reviews WHERE action_id=?',
                                                   (payload['action_id'],)).fetchone()[0])
                self.assertEqual(saved_scope, scope)
                self.assertEqual(states['address'], 'owner-confirmed-present' if expected_address else 'unreviewed')

    def test_optional_address_does_not_admit_missing_or_manager_owned_identifiers(self):
        seed = self.expert_card(suffix='optional-address-identity-seed')
        with self.store.connection() as db:
            raw = json.loads(db.execute('SELECT capture_json FROM documents WHERE id=?', (seed,)).fetchone()[0])['variants'][0]
        raw = raw.replace('адрес регистрации:\n999999, Россия, Макетная область, д. Условная, д. 17; ', '')
        for name, owned_label in (('snils', 'СНИЛС: 112-\n233-445-95; '), ('inn', '; ИНН: 123456789047')):
            for manager in (False, True):
                with self.subTest(missing=name, manager=manager):
                    text = raw.replace(owned_label, '')
                    if manager:
                        text += '\nУтвердить финансовым управляющим Тестова Петра Ивановича (ИНН: 123456789047; СНИЛС: 112-233-445-95).'
                    key = self.expert_card(suffix=f'optional-address-held-{name}-{manager}', variants=[text])
                    self.assertIsNone(self.store.get_document(key, expose=False, expert=True))

    def test_expert_caption_uses_full_text_date_from_exact_decision_pair(self):
        seed = self.expert_card(suffix='date-pair-seed')
        with self.store.connection() as db:
            raw = json.loads(db.execute('SELECT capture_json FROM documents WHERE id=?', (seed,)).fetchone()[0])['variants'][0]
        original = '     25.11.2099\nРезолютивная часть объявлена 24.11.2099.\n'
        pair = ('Резолютивная часть решения оглашена 24.11.2099.\n'
                'Полный текст решения изготовлен 25.11.2099.\n')
        text = raw.replace(original, pair)
        for index, variant in enumerate((text, text.replace('оглашена', 'объявлена'),
                                        text.replace('Дело №', '«25» ноября 2099 года    Дело №'))):
            key = self.expert_card(suffix=f'date-pair-{index}', variants=[variant])
            document = self.store.get_document(key, expose=False, expert=True)
            self.assertIsNotNone(document)
            field = document['fields']['doc_date']
            self.assertEqual(field['value'], '25.11.2099')
            self.assertEqual(variant[field['source_start']:field['source_end']], '25.11.2099')
        for index, variants in enumerate((
            [text.replace('изготовлен 25.11.2099.', 'изготовлен 25.11.2099 по сведениям заявителя.')],
            [text.replace('Полный текст', 'Обстоятельства дела.\nПолный текст')],
            [text.replace('Рассмотрев', 'Полный текст решения изготовлен 26.11.2099.\nРассмотрев')],
            [text.replace('оглашена 24.11.2099', 'оглашена 26.11.2099')],
            [text, text.replace('изготовлен 25.11.2099', 'изготовлен 26.11.2099')],
            [text.replace('Дело №', '«24» ноября 2099 года    Дело №')],
            [text.replace('Дело №', '«25» ноября 2099 года Дело №')],
        )):
            with self.subTest(index=index):
                key = self.expert_card(suffix=f'date-pair-held-{index}', variants=variants)
                self.assertIsNone(self.store.get_document(key, expose=False, expert=True))

    def test_case_warning_requires_exact_full_text_caption_in_every_reading(self):
        seed = self.expert_card(suffix='cited-case-seed')
        with self.store.connection() as db:
            raw = json.loads(db.execute('SELECT capture_json FROM documents WHERE id=?', (seed,)).fetchone()[0])['variants'][0]
        legacy = raw.replace('УСТАНОВИЛ:\n', 'УСТАНОВИЛ:\nТакже рассматривалось дело № А99-456/2099.\n')
        exact = legacy.replace('     25.11.2099\nРезолютивная часть объявлена 24.11.2099.\n',
            'Резолютивная часть решения оглашена 24.11.2099.\nПолный текст решения изготовлен 25.11.2099.\n')
        key = self.expert_card(suffix='cited-case-exact', variants=[exact])
        with self.store.connection() as db:
            captured = json.loads(db.execute('SELECT capture_json FROM documents WHERE id=?', (key,)).fetchone()[0])
        self.assertTrue(captured['observed_pipeline']['case_number']['warning'])
        self.assertIsNotNone(self.store.get_document(key, expose=False, expert=True))
        for index, variants in enumerate(([legacy], [exact, legacy], [exact.replace(
            'Рассмотрев', 'Дело № А99-456/2099\nРассмотрев')])):
            with self.subTest(index=index):
                held = self.expert_card(suffix=f'cited-case-held-{index}', variants=variants)
                self.assertIsNone(self.store.get_document(held, expose=False, expert=True))

    def test_native_person_name_allows_inflection_but_not_other_subject_or_kind(self):
        seed = self.expert_card(suffix='native-name-seed')
        with self.store.connection() as db:
            snapshot = db.execute('SELECT capture_json FROM documents WHERE id=?', (seed,)).fetchone()[0]
        for field, source, name, ready in (
            ('fio_raw', 'debtor_role:v1', 'Макетову Анну Петровну', True),
            ('fio_raw', 'debtor_role:v1', 'Петрову Анну Петровну', False),
            ('legal_name', 'debtor_entity_role:v1', 'ООО «Учебная организация»', False),
        ):
            with self.subTest(name=name):
                captured = json.loads(snapshot)
                captured['observed_pipeline'][field] = {'source': source, 'value': name}
                projection, status = expert_projection(captured)
                self.assertEqual(status, 'ready' if ready else 'needs_extraction')
                if ready:
                    self.assertEqual(projection['fields']['fio_raw']['value'], 'Макетова Анна Петровна')

    def test_expert_caption_accepts_same_line_date_and_case_with_year_suffix(self):
        self.login()
        seed = self.expert_card(suffix='caption-layout-seed')
        with self.store.connection() as db:
            raw = json.loads(db.execute('SELECT capture_json FROM documents WHERE id=?', (seed,)).fetchone()[0])['variants'][0]
        header = 'Дело № А99-123/2099\n     25.11.2099\n'
        for index, year_suffix in enumerate(('', ' г.', ' года')):
            with self.subTest(year_suffix=year_suffix):
                text = raw.replace(header, f'   25.11.2099{year_suffix}          Дело № А99-123/2099\n')
                key = self.expert_card(suffix=f'caption-layout-{index}', variants=[text])
                document = self.store.get_document(key, expose=False, expert=True)
                self.assertIsNotNone(document)
                for name, literal in (('doc_date', '25.11.2099'), ('case_number', 'А99-123/2099')):
                    field = document['fields'][name]
                    self.assertEqual(text[field['source_start']:field['source_end']], literal)
                response = self.client.post(f'/api/documents/{key}/review',
                    json=self.verdict(key, decision='rejected', comment=''), headers={'Origin': ORIGIN})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()['detail'], 'rejection_needs_reason')
        for index, invalid_header in enumerate((
            '25.11.2099 доказательства получены          Дело № А99-123/2099\n',
            '25.11.2099 г.          Дело № А99-123/2099\n26.11.2099\n',
            '25.11.2099 года          Дело № А99-123/2099\n26.11.2099\n',
        )):
            with self.subTest(invalid_header=invalid_header):
                key = self.expert_card(suffix=f'caption-layout-held-{index}',
                                       variants=[raw.replace(header, invalid_header)])
                self.assertIsNone(self.store.get_document(key, expose=False, expert=True))

    def test_expert_caption_binds_date_in_the_city_row(self):
        key = self.expert_card(suffix='city-date-seed')
        with self.store.connection() as db:
            raw = json.loads(db.execute('SELECT capture_json FROM documents WHERE id=?', (key,)).fetchone()[0])['variants'][0]
        line = '     25.11.2099\n'
        for index, prefix in enumerate(('город Тестоград        ', '\nгород Тестоград        ')):
            rendered = raw.replace(line, prefix + '25.11.2099 года\n')
            target = self.expert_card(suffix=f'city-date-{index}', variants=[rendered])
            card = self.store.get_document(target, expose=False, expert=True)
            parsed = card['fields']['doc_date']
            self.assertEqual(parsed['value'], '25.11.2099')
            self.assertEqual(rendered[parsed['source_start']:parsed['source_end']], '25.11.2099')
        for index, extra in enumerate((
            'из города Тестограда        25.11.2099 года\n',
            'город Тестоград        25.11.2099 года состоялось заседание\n',
        )):
            target = self.expert_card(suffix=f'city-date-held-{index}', variants=[raw.replace(line, extra)])
            self.assertIsNone(self.store.get_document(target, expose=False, expert=True))

    def test_expert_gate_rejects_unrelated_caption_or_conflicting_explicit_debtor(self):
        ready = self.expert_card()
        with self.store.connection() as db:
            raw = json.loads(db.execute('SELECT capture_json FROM documents WHERE id=?', (ready,)).fetchone()[0])['variants'][0]
        for index, altered in enumerate((
            raw.replace('     25.11.2099', 'Доказательства получены 25.11.2099'),
            raw.replace('заявление о признании гражданина несостоятельным (банкротом)', 'иск о взыскании долга'),
            raw.replace('заявление о признании гражданина несостоятельным (банкротом)',
                        'дело о несостоятельности Ивановой Ирины Сергеевны'),
            raw.replace('заявление о признании гражданина несостоятельным (банкротом)',
                        'заявление о признании Ивановой Ирины Сергеевны несостоятельной (банкротом)'),
            raw.replace('заявление о признании гражданина несостоятельным (банкротом)',
                        'заявление Ивановой Ирины Сергеевны о признании её несостоятельной (банкротом)'),
            raw.replace('заявление о признании гражданина несостоятельным (банкротом)',
                        'материалы, содержащие выписку по делу о несостоятельности Макетовой Анны Петровны'),
            raw.replace('УСТАНОВИЛ:', 'Должник: ООО «Учебная организация» ИНН 7700000016.\nУСТАНОВИЛ:'),
            raw.replace('УСТАНОВИЛ:', 'Должник: Иванова Ирина Сергеевна.\nУСТАНОВИЛ:'),
            raw.replace('УСТАНОВИЛ:\n', '') + '\nУСТАНОВИЛ:\n',
            raw.replace('заявление о признании гражданина несостоятельным (банкротом)',
                        'заявление о признании Ивановой Ирины Сергеевны '
                        '(место рождения: Тестоград; дата рождения: 01.01.1980) несостоятельной (банкротом)'),
        )):
            with self.subTest(index=index):
                key = self.expert_card(suffix=f'held-{index}', variants=[altered])
                self.assertIsNone(self.store.get_document(key, expose=False, expert=True))

        with_biography = raw.replace('заявление о признании гражданина несостоятельным (банкротом)',
                                     'заявление о признании Макетовой Анны Петровны '
                                     '(место рождения: Тестоград; дата рождения: 01.01.1980) '
                                     'несостоятельной (банкротом)')
        self.assertIsNotNone(self.store.get_document(self.expert_card(suffix='caption-biography',
            variants=[with_biography]), expose=False, expert=True))

    def test_preexisting_expert_edit_replays_without_creating_or_changing_a_verdict(self):
        prior = self.payload(decision='rejected', comment='Synthetic legacy reason')
        self.store.apply_review(self.key, prior, actor=EXPERT)
        self.login()
        url = f'/api/documents/{self.key}/review'
        response = self.client.post(url, json=prior, headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['replayed'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT comment FROM reviews').fetchone()[0], 'Synthetic legacy reason')

    def test_old_review_without_actor_stays_unchanged_and_cannot_be_rebound(self):
        self.store.apply_review(self.key, self.payload())
        with self.store.connection(write=True) as db:
            before = tuple(db.execute('SELECT * FROM reviews').fetchone())
            db.execute('DROP TABLE review_actors')
        restarted = ReviewStore(self.root)
        self.assertTrue(restarted.apply_review(self.key, self.payload())['replayed'])
        with self.assertRaisesRegex(ReviewConflict, 'action_actor_conflict'):
            restarted.apply_review(self.key, self.payload(), actor=EXPERT)
        with restarted.connection() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM reviews').fetchone()), before)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_actors').fetchone()[0], 0)

    def test_logout_requires_origin_and_clears_browser_cookie(self):
        self.login()
        self.assertEqual(self.client.post('/logout').status_code, 403)
        response = self.client.post('/logout', headers={'Origin': ORIGIN}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/login')
        self.assertIn('Max-Age=0', response.headers['set-cookie'])
        self.assertEqual(self.client.get('/api/stats').status_code, 401)

    def test_launcher_private_token_and_loopback_no_proxy_trust(self):
        token_file = Path(self.temp.name) / 'synthetic.key'
        token_file.write_text(TOKEN)
        token_file.chmod(0o600)
        argv = ['--root', str(self.root), '--public-origin', ORIGIN, '--expert', EXPERT,
                '--token-file', str(token_file)]
        with patch('uvicorn.run') as run:
            self.assertEqual(launcher.main(argv), 0)
            self.assertEqual(run.call_args.kwargs['host'], '127.0.0.1')
            self.assertFalse(run.call_args.kwargs['proxy_headers'])
            self.assertFalse(run.call_args.kwargs['access_log'])
        token_file.chmod(0o644)
        with self.assertRaises(ValueError):
            launcher.private_token(token_file)
        token_file.chmod(0o600)
        link = Path(self.temp.name) / 'link.key'
        link.symlink_to(token_file)
        with self.assertRaises(OSError):
            launcher.private_token(link)
        missing_root = Path(self.temp.name) / 'must-not-be-created'
        argv[1] = str(missing_root)
        argv[3] = 'http://insecure.test'
        with patch('builtins.print'), patch('uvicorn.run') as run:
            self.assertEqual(launcher.main(argv), 1)
            run.assert_not_called()
        self.assertFalse(missing_root.exists())


class RemoteConfigurationTests(unittest.TestCase):
    def test_canonical_origin_only(self):
        for value in ('http://review.test', 'https://*.test', 'https://review.test/',
                      'https://user:password@review.test', 'https://review.test#fragment',
                      'https://review.test?query', 'https://REVIEW.test', 'https://review.test:443',
                      'https://review.test:bad', 'https://review.test:0', 'https://review_test',
                      'https://review.test\\foreign', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                remote_origin(value)
        for value in (ORIGIN, 'https://review.example.test:8443', 'https://127.0.0.1', 'https://[::1]'):
            self.assertEqual(remote_origin(value), value)


if __name__ == '__main__':
    unittest.main()
