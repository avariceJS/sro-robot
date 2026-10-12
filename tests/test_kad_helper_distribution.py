from __future__ import annotations

from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from zipfile import ZipFile

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.api.deps import current_user
from app.api import routes_kad_helper


class KadHelperDistributionTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(routes_kad_helper.router)
        self.app.mount('/static', StaticFiles(directory='app/web/static'), name='static')
        self.app.add_api_route('/', lambda: {}, name='tasks_page')
        self.app.add_api_route('/logout', lambda: {}, name='logout')
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def test_authenticated_routes_have_no_database_or_queue_side_effects(self):
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(display_name='Synthetic employee')
        page = self.client.get('/kad-helper')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Ручное скачивание и загрузка не требуются', page.text)
        self.assertIn('Firefox 128', page.text)
        self.assertIn('about:debugging', page.text)
        self.assertIn('временн', page.text.lower())
        archive = self.client.get('/kad-helper/download')
        self.assertEqual(archive.status_code, 200)
        self.assertEqual(archive.headers['content-type'], 'application/zip')

    def test_both_routes_require_authentication_before_packaging(self):
        def deny():
            raise HTTPException(status_code=401)
        self.app.dependency_overrides[current_user] = deny
        for path in ('/kad-helper', '/kad-helper/download'):
            self.assertEqual(self.client.get(path).status_code, 401)

    def test_archive_is_deterministic_and_contains_only_explicit_sources(self):
        raw = routes_kad_helper.helper_archive()
        self.assertEqual(raw, routes_kad_helper.helper_archive())
        with ZipFile(BytesIO(raw)) as archive:
            self.assertEqual(set(archive.namelist()), set(routes_kad_helper.HELPER_FILES))
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['permissions'], ['storage'])
            self.assertEqual(manifest['optional_permissions'], ['webRequest', 'webRequestBlocking', 'webRequestFilterResponse'])
            self.assertEqual(manifest['host_permissions'], ['https://kad.arbitr.ru/*', 'http://localhost/*', 'http://127.0.0.1/*'])
            self.assertEqual(manifest['version'], '0.2.3')
            self.assertEqual(manifest['browser_specific_settings']['gecko']['strict_min_version'], '128')
            self.assertEqual(manifest['background']['scripts'], ['protocol.js', 'native-pdf.js', 'bridge.js', 'background.js'])
            self.assertEqual(manifest['background']['service_worker'], 'background.js')
            self.assertNotIn('externally_connectable', manifest)
            for script in manifest['content_scripts'][0]['js']:
                self.assertIn(script, archive.namelist())


if __name__ == '__main__':
    unittest.main()
