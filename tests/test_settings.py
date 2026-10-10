"""Environment configuration must not expose production sessions to a known key."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.settings import Settings


class SettingsSecurityTests(unittest.TestCase):
    def load_settings(self, **environment: str) -> Settings:
        with tempfile.TemporaryDirectory() as upload_dir:
            isolated_environment = {
                "SRO_UPLOAD_DIR": upload_dir,
                **environment,
            }
            with patch("app.settings.load_env"), patch.dict(
                os.environ, isolated_environment, clear=True
            ):
                return Settings.load()

    def test_debug_mode_can_use_development_secret_fallback(self) -> None:
        settings = self.load_settings(APP_DEBUG="1")

        self.assertTrue(settings.debug)
        self.assertEqual(settings.secret_key, "dev-only-not-for-production")
        self.assertFalse(settings.ocr_task_leases_enabled)

    def test_ocr_task_leases_require_explicit_enablement(self) -> None:
        disabled = self.load_settings(APP_DEBUG="1")
        enabled = self.load_settings(APP_DEBUG="1", OCR_TASK_LEASES_ENABLED="1")

        self.assertFalse(disabled.ocr_task_leases_enabled)
        self.assertTrue(enabled.ocr_task_leases_enabled)

    def test_database_pool_defaults_preserve_existing_bounded_capacity(self) -> None:
        settings = self.load_settings(APP_DEBUG="1")

        self.assertEqual(settings.database_pool_size, 5)
        self.assertEqual(settings.database_max_overflow, 10)
        self.assertEqual(settings.database_pool_timeout_seconds, 5)

    def test_database_pool_values_are_configurable_and_validated(self) -> None:
        settings = self.load_settings(
            APP_DEBUG="1",
            DB_POOL_SIZE="8",
            DB_MAX_OVERFLOW="2",
            DB_POOL_TIMEOUT_SECONDS="9",
        )

        self.assertEqual(settings.database_pool_size, 8)
        self.assertEqual(settings.database_max_overflow, 2)
        self.assertEqual(settings.database_pool_timeout_seconds, 9)

    def test_invalid_database_pool_values_fail_closed(self) -> None:
        for name, value in (
            ("DB_POOL_SIZE", "0"),
            ("DB_POOL_SIZE", "unbounded"),
            ("DB_MAX_OVERFLOW", "-1"),
            ("DB_MAX_OVERFLOW", "many"),
            ("DB_POOL_TIMEOUT_SECONDS", "0"),
            ("DB_POOL_TIMEOUT_SECONDS", "fast"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ValueError, name):
                    self.load_settings(APP_DEBUG="1", **{name: value})

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions are required")
    def test_existing_upload_directory_is_restricted_to_the_service_user(self) -> None:
        with tempfile.TemporaryDirectory() as upload_dir:
            path = Path(upload_dir)
            path.chmod(0o755)
            with patch("app.settings.load_env"), patch.dict(
                os.environ,
                {
                    "SRO_UPLOAD_DIR": upload_dir,
                    "APP_DEBUG": "1",
                },
                clear=True,
            ):
                Settings.load()

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    @unittest.skipUnless(os.name == "posix", "POSIX file ownership is required")
    def test_settings_refuse_storage_owned_by_another_account(self) -> None:
        with tempfile.TemporaryDirectory() as upload_dir:
            path = Path(upload_dir)
            path.chmod(0o755)
            service_uid = os.geteuid() + 1
            with (
                patch("app.settings.load_env"),
                patch.dict(
                    os.environ,
                    {"SRO_UPLOAD_DIR": upload_dir, "APP_DEBUG": "1"},
                    clear=True,
                ),
                patch("app.settings.os.geteuid", return_value=service_uid),
                self.assertRaisesRegex(ValueError, "owned by the service user"),
            ):
                Settings.load()

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_settings_repr_does_not_disclose_credentials_or_session_secret(self) -> None:
        settings = Settings(
            database_url="postgresql://synthetic-user:db-password@db.invalid/app",
            redis_url="redis://:redis-password@cache.invalid/0",
            secret_key="synthetic-session-secret",
            upload_dir=Path("/tmp/synthetic-upload"),
            debug=False,
        )

        representation = repr(settings)
        for secret in ("db-password", "redis-password", "synthetic-session-secret"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, representation)
        self.assertIn("debug=False", representation)

    def test_production_mode_rejects_missing_or_short_secret(self) -> None:
        for secret in (None, "short"):
            with self.subTest(secret="missing" if secret is None else "short"):
                environment = {"APP_DEBUG": "0"}
                if secret is not None:
                    environment["APP_SECRET_KEY"] = secret
                with self.assertRaisesRegex(ValueError, "APP_SECRET_KEY"):
                    self.load_settings(**environment)

    def test_production_mode_accepts_configured_secret(self) -> None:
        secret = "synthetic-test-secret-with-at-least-32-chars"
        database_url = "postgresql+psycopg://synthetic:synthetic@db.invalid/sro"
        redis_url = "redis://synthetic:synthetic@cache.invalid/0"

        settings = self.load_settings(
            APP_DEBUG="0",
            APP_SECRET_KEY=secret,
            DATABASE_URL=database_url,
            REDIS_URL=redis_url,
        )

        self.assertFalse(settings.debug)
        self.assertEqual(settings.secret_key, secret)
        self.assertEqual(settings.database_url, database_url)
        self.assertEqual(settings.redis_url, redis_url)

    def test_production_mode_requires_explicit_database_and_redis_urls(self) -> None:
        common = {
            "APP_DEBUG": "0",
            "APP_SECRET_KEY": "synthetic-test-secret-with-at-least-32-chars",
        }
        for missing, configured in (
            ("DATABASE_URL", {"REDIS_URL": "redis://synthetic.invalid/0"}),
            ("REDIS_URL", {"DATABASE_URL": "postgresql://synthetic.invalid/sro"}),
        ):
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(ValueError, missing):
                    self.load_settings(**common, **configured)


if __name__ == "__main__":
    unittest.main()
