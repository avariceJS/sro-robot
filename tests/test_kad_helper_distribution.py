from __future__ import annotations

from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.api.deps import current_user
from app.api import routes_kad_helper
from app.kad_helper_package import FILES, STORE_ORIGIN, development_archive, store_archive


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

    def test_staff_page_hides_unpublished_unpacked_install_and_explains_workflow(self):
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(display_name='Synthetic employee')
        page = self.client.get('/kad-helper')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Публикация готовится', page.text)
        self.assertIn('Установка сотрудникам пока недоступна', page.text)
        self.assertIn('kad-helper-connection-status', page.text)
        self.assertIn('/kad-helper/privacy', page.text)
        self.assertNotIn('development ZIP', page.text)
        self.assertNotIn('Загрузить распакованное расширение', page.text)

    def test_privacy_disclosure_is_public_and_describes_unselected_candidates(self):
        response = self.client.get('/kad-helper/privacy')
        self.assertEqual(response.status_code, 200)
        self.assertIn('полный список видимых строк хронологии', response.text)
        self.assertIn('на сервере организации', response.text)

    def test_authentication_precedes_store_page_and_developer_package(self):
        def deny():
            raise HTTPException(status_code=401)
        self.app.dependency_overrides[current_user] = deny
        self.assertEqual(self.client.get('/kad-helper').status_code, 401)
        self.assertEqual(self.client.get('/kad-helper/download').status_code, 401)

    def test_production_has_no_unpacked_download_route(self):
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(display_name='Synthetic employee')
        with patch.object(routes_kad_helper.settings, 'debug', False):
            self.assertEqual(self.client.get('/kad-helper/download').status_code, 404)

    def test_development_archive_is_deterministic_and_explicitly_local(self):
        raw = development_archive()
        self.assertEqual(raw, development_archive())
        with ZipFile(BytesIO(raw)) as archive:
            self.assertEqual(set(archive.namelist()), set(FILES))
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['version'], '0.3.0')
            self.assertEqual(manifest['permissions'], ['storage', 'debugger'])
            self.assertIn('http://localhost/*', manifest['host_permissions'])

    def test_static_store_packages_are_origin_pinned_and_browser_specific(self):
        self.assertEqual(store_archive('chrome'), store_archive('chrome'))
        for browser in ('chrome', 'edge', 'firefox'):
            with self.subTest(browser=browser), ZipFile(BytesIO(store_archive(browser))) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(set(archive.namelist()), set(FILES) - {'README.md'})
                self.assertEqual(manifest['host_permissions'], ['https://kad.arbitr.ru/*', STORE_ORIGIN + '/*'])
                service = next(item for item in manifest['content_scripts'] if 'service.js' in item['js'])
                self.assertEqual(service['matches'], [STORE_ORIGIN + '/*'])
                self.assertNotIn('externally_connectable', manifest)
                self.assertNotIn('localhost', archive.read('bridge.js').decode())
                self.assertNotIn('127.0.0.1', archive.read('service.js').decode())
                self.assertIn(STORE_ORIGIN, archive.read('popup.js').decode())
                if browser in {'chrome', 'edge'}:
                    self.assertEqual(manifest['permissions'], ['storage', 'debugger'])
                    self.assertNotIn('optional_permissions', manifest)
                    self.assertNotIn('scripts', manifest['background'])
                    self.assertIn('service_worker', manifest['background'])
                else:
                    self.assertEqual(manifest['permissions'], ['storage'])
                    self.assertEqual(manifest['optional_permissions'], ['webRequest', 'webRequestBlocking', 'webRequestFilterResponse'])
                    self.assertNotIn('service_worker', manifest['background'])
                    self.assertIn('scripts', manifest['background'])

    def test_packager_rejects_unknown_browser_without_reading_sources(self):
        with self.assertRaises(ValueError):
            store_archive('safari')


if __name__ == '__main__':
    unittest.main()
