from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.public_origin import public_app_origin
from app.security import _same_host_origin
from app.settings import Settings


class PublicOriginValidationTests(unittest.TestCase):
    def test_accepts_only_the_canonical_public_https_origin(self) -> None:
        self.assertEqual(public_app_origin("https://sro-office.ru"), "https://sro-office.ru")
        self.assertIsNone(public_app_origin(None))
        self.assertIsNone(public_app_origin(""))

    def test_rejects_noncanonical_or_nonpublic_origins(self) -> None:
        for value in (
            "http://sro-office.ru",
            "https://sro-office.ru/",
            "https://sro-office.ru:443",
            "https://user@sro-office.ru",
            "https://sro-office.ru/path",
            "https://sro-office.ru?next=/",
            "https://sro-office.ru#fragment",
            "https://office.localhost",
            "https://office.internal",
            "https://office.home.arpa",
            "https://127.0.0.1",
            "https://SRO-office.ru",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                public_app_origin(value)

    def test_settings_require_production_mode_and_distinct_postgres_roles(self) -> None:
        environment = {
            "APP_DEBUG": "0",
            "APP_PUBLIC_ORIGIN": "https://sro-office.ru",
            "APP_SECRET_KEY": "synthetic-production-secret-value-long-enough",
            "DATABASE_URL": "postgresql+psycopg://runtime:synthetic@127.0.0.1:5432/sro",
            "MIGRATION_DATABASE_URL": "postgresql+psycopg://migrator:synthetic@127.0.0.1:5432/sro",
            "REDIS_URL": "redis://:synthetic@127.0.0.1:6379/0",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("app.settings.load_env"),
            patch("app.settings._secure_private_directory"),
        ):
            settings = Settings.load()
        self.assertEqual(settings.public_app_origin, "https://sro-office.ru")
        self.assertFalse(settings.debug)

    def test_settings_reject_public_origin_in_debug_mode(self) -> None:
        environment = {
            "APP_DEBUG": "1",
            "APP_PUBLIC_ORIGIN": "https://sro-office.ru",
            "DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro",
            "REDIS_URL": "redis://synthetic:synthetic@127.0.0.1:1/0",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("app.settings.load_env"),
            patch("app.settings._secure_private_directory"),
            self.assertRaisesRegex(ValueError, "requires APP_DEBUG=0"),
        ):
            Settings.load()


class ConfiguredPublicMutationOriginTests(unittest.TestCase):
    origin = "https://sro-office.ru"

    def test_accepts_canonical_site_origin_and_secure_referer_fallback(self) -> None:
        for headers in (
            [
                (b"host", b"sro-office.ru"),
                (b"origin", b"https://sro-office.ru"),
            ],
            [
                (b"host", b"sro-office.ru:443"),
                (b"origin", b"https://sro-office.ru"),
            ],
            [
                (b"host", b"sro-office.ru"),
                (b"referer", b"https://sro-office.ru/tasks/1"),
            ],
            [
                (b"host", b"localhost:8000"),
                (b"origin", b"http://localhost:8000"),
            ],
        ):
            with self.subTest(headers=headers):
                self.assertTrue(_same_host_origin(headers, self.origin))

    def test_rejects_mismatched_hosts_downgrade_and_ambiguous_headers(self) -> None:
        for headers in (
            [(b"host", b"attacker.example"), (b"origin", b"https://sro-office.ru")],
            [(b"host", b"sro-office.ru.evil.example"), (b"origin", b"https://sro-office.ru")],
            [(b"host", b"sro-office.ru:444"), (b"origin", b"https://sro-office.ru")],
            [(b"host", b"sro-office.ru"), (b"origin", b"http://sro-office.ru")],
            [(b"host", b"sro-office.ru"), (b"origin", b"https://sro-office.ru.evil.example")],
            [(b"host", b"sro-office.ru"), (b"referer", b"http://sro-office.ru/tasks/1")],
            [(b"host", b"sro-office.ru"), (b"referer", b"https://sro-office.ru.evil.example/")],
            [(b"host", b"sro-office.ru"), (b"referer", b"https://user@sro-office.ru/")],
            [(b"host", b"sro-office.ru"), (b"origin", b"https://sro-office.ru"), (b"origin", b"https://sro-office.ru")],
        ):
            with self.subTest(headers=headers):
                self.assertFalse(_same_host_origin(headers, self.origin))
