"""Environment configuration must not expose production sessions to a known key."""

from __future__ import annotations

import os
import tempfile
import unittest
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

        settings = self.load_settings(APP_DEBUG="0", APP_SECRET_KEY=secret)

        self.assertFalse(settings.debug)
        self.assertEqual(settings.secret_key, secret)


if __name__ == "__main__":
    unittest.main()
