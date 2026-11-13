"""Synthetic HTTPS review authentication, identity and private-launch boundaries."""
import asyncio
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from sro_robot.research.review_api import create_app, REMOTE_COOKIE, remote_origin
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

    def test_no_secret_url_bootstrap_and_login_form_uses_password_post(self):
        self.assertEqual(self.client.get('/', follow_redirects=False).headers['location'], '/login')
        self.assertEqual(self.client.get('/api/stats').status_code, 401)
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 200)
        self.assertIn('type="password"', response.text)
        self.assertIn('method="post"', response.text)
        self.assertNotIn(TOKEN, response.text)
        self.assertEqual(self.client.get('/session', params={'token': TOKEN}).status_code, 404)
        self.assertEqual(self.client.get('/api/stats').status_code, 401)

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
        forged = dict(self.payload(), actor='client-selected-identity')
        result = self.client.post('/api/documents/' + self.key + '/review', json=forged, headers={'Origin': ORIGIN})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(self.store.stats()['approved'], 0)
        result = self.client.post('/api/documents/' + self.key + '/review', json=self.payload(), headers={'Origin': ORIGIN})
        self.assertEqual(result.status_code, 200)
        restarted = ReviewStore(self.root)
        with restarted.connection() as db:
            self.assertEqual(db.execute('SELECT actor FROM review_actors').fetchone()[0], EXPERT)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 1)
        self.assertTrue(restarted.apply_review(self.key, self.payload(), actor=EXPERT)['replayed'])
        with self.assertRaisesRegex(ReviewConflict, 'action_actor_conflict'):
            restarted.apply_review(self.key, self.payload(), actor='different-expert')
        with self.assertRaisesRegex(ReviewConflict, 'action_actor_conflict'):
            restarted.apply_review(self.key, self.payload())

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
