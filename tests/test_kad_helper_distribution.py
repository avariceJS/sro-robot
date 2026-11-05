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

    def test_staff_page_uses_configured_unlisted_chrome_store_as_primary_install_cta(self):
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(display_name='Synthetic employee')
        store_url = 'https://chromewebstore.google.com/detail/gidpbdligakmphhjmnkndmhnemlppdhg'
        with patch.object(routes_kad_helper.settings, 'kad_chrome_store_url', store_url), \
             patch.object(routes_kad_helper.settings, 'kad_edge_store_url', None), \
             patch.object(routes_kad_helper.settings, 'kad_firefox_store_url', None):
            page = self.client.get('/kad-helper')
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'href="{store_url}"', page.text)
        self.assertIn('Установить помощник', page.text)
        self.assertIn('Откроется Chrome Web Store; подтвердите установку в Chrome.', page.text)
        self.assertNotIn('Публикация готовится', page.text)
        self.assertNotIn('Установка сотрудникам пока недоступна', page.text)
        self.assertNotIn('Другие магазины', page.text)
        self.assertNotIn('Как проходит поиск', page.text)
        self.assertIn('kad-helper-connection-status', page.text)
        self.assertRegex(page.text, r'kad-helper-status\.js\?v=[0-9a-f]{16}')
        self.assertRegex(page.text, r'kad-page-location\.js\?v=[0-9a-f]{16}')
        self.assertLess(page.text.index('kad-page-location.js'), page.text.index('</head>'))
        self.assertIn('/kad-helper/privacy', page.text)
        self.assertIn('data-state="checking"', page.text)
        self.assertNotIn('development ZIP', page.text)
        self.assertNotIn('Загрузить распакованное расширение', page.text)

    def test_store_page_shows_only_configured_alternate_links_and_truthful_unconfigured_state(self):
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(display_name='Synthetic employee')
        with patch.object(routes_kad_helper.settings, 'kad_chrome_store_url', None), \
             patch.object(routes_kad_helper.settings, 'kad_edge_store_url', 'https://edge.example/store/extension'), \
             patch.object(routes_kad_helper.settings, 'kad_firefox_store_url', None):
            alternate = self.client.get('/kad-helper')
        self.assertEqual(alternate.status_code, 200)
        self.assertIn('Установить помощник', alternate.text)
        self.assertIn('https://edge.example/store/extension', alternate.text)
        self.assertIn('Откроется Microsoft Edge Add-ons; подтвердите установку в Edge.', alternate.text)
        self.assertNotIn('Chrome Web Store', alternate.text)
        self.assertNotIn('Firefox Add-ons', alternate.text)

        with patch.object(routes_kad_helper.settings, 'kad_chrome_store_url', None), \
             patch.object(routes_kad_helper.settings, 'kad_edge_store_url', None), \
             patch.object(routes_kad_helper.settings, 'kad_firefox_store_url', None):
            unconfigured = self.client.get('/kad-helper')
        self.assertEqual(unconfigured.status_code, 200)
        self.assertNotIn('data-kad-store-link', unconfigured.text)
        self.assertIn('Ссылка на официальный магазин пока не настроена', unconfigured.text)
        self.assertIn('data-stores-ready="false"', unconfigured.text)

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
