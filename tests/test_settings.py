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
        self.assertEqual(settings.migration_database_url, settings.database_url)
        self.assertFalse(settings.ocr_task_leases_enabled)

    def test_debug_mode_rejects_ambiguous_values(self) -> None:
        for value in ("true", "false", "yes", "no", "", "2"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "APP_DEBUG"):
                self.load_settings(APP_DEBUG=value)

    def test_debug_mode_rejects_migration_url_for_another_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "same database"):
            self.load_settings(
                APP_DEBUG="1",
                DATABASE_URL="postgresql://runtime:synthetic@runtime.invalid/sro",
                MIGRATION_DATABASE_URL="postgresql://migrator:synthetic@other.invalid/sro",
            )

    def test_debug_mode_compares_explicit_migration_url_with_runtime_fallback(self) -> None:
        with self.assertRaisesRegex(ValueError, "same database"):
            self.load_settings(
                APP_DEBUG="1",
                MIGRATION_DATABASE_URL="postgresql://migrator:synthetic@other.invalid/sro",
            )

    def test_debug_mode_allows_distinct_roles_for_the_same_database(self) -> None:
        runtime_url = "postgresql://runtime:synthetic@db.invalid/sro"
        migration_url = "postgresql://migrator:synthetic@db.invalid/sro"
        settings = self.load_settings(
            APP_DEBUG="1",
            DATABASE_URL=runtime_url,
            MIGRATION_DATABASE_URL=migration_url,
        )

        self.assertEqual(settings.database_url, runtime_url)
        self.assertEqual(settings.migration_database_url, migration_url)

    def test_ocr_task_leases_require_explicit_enablement(self) -> None:
        disabled = self.load_settings(APP_DEBUG="1")
        enabled = self.load_settings(APP_DEBUG="1", OCR_TASK_LEASES_ENABLED="1")

        self.assertFalse(disabled.ocr_task_leases_enabled)
        self.assertTrue(enabled.ocr_task_leases_enabled)

    def test_ocr_task_leases_reject_ambiguous_values(self) -> None:
        for value in ("true", "false", "yes", "no", "", "2"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "OCR_TASK_LEASES_ENABLED"
            ):
                self.load_settings(APP_DEBUG="1", OCR_TASK_LEASES_ENABLED=value)

    def test_database_pool_defaults_preserve_existing_bounded_capacity(self) -> None:
        settings = self.load_settings(APP_DEBUG="1")

        self.assertEqual(settings.database_pool_size, 5)
        self.assertEqual(settings.database_max_overflow, 10)
        self.assertEqual(settings.database_pool_timeout_seconds, 5)
        self.assertEqual(settings.database_lock_timeout_ms, 5000)

    def test_database_pool_values_are_configurable_and_validated(self) -> None:
        settings = self.load_settings(
            APP_DEBUG="1",
            DB_POOL_SIZE="8",
            DB_MAX_OVERFLOW="2",
            DB_POOL_TIMEOUT_SECONDS="9",
            DB_LOCK_TIMEOUT_MS="750",
        )

        self.assertEqual(settings.database_pool_size, 8)
        self.assertEqual(settings.database_max_overflow, 2)
        self.assertEqual(settings.database_pool_timeout_seconds, 9)
        self.assertEqual(settings.database_lock_timeout_ms, 750)

    def test_invalid_database_pool_values_fail_closed(self) -> None:
        for name, value in (
            ("DB_POOL_SIZE", "0"),
            ("DB_POOL_SIZE", "unbounded"),
            ("DB_MAX_OVERFLOW", "-1"),
            ("DB_MAX_OVERFLOW", "many"),
            ("DB_POOL_TIMEOUT_SECONDS", "0"),
            ("DB_POOL_TIMEOUT_SECONDS", "fast"),
            ("DB_LOCK_TIMEOUT_MS", "0"),
            ("DB_LOCK_TIMEOUT_MS", "unbounded"),
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
            migration_database_url="postgresql://migrator:migration-password@db.invalid/app",
            secret_key="synthetic-session-secret",
            upload_dir=Path("/tmp/synthetic-upload"),
            debug=False,
        )

        representation = repr(settings)
        for secret in (
            "db-password",
            "redis-password",
            "migration-password",
            "synthetic-session-secret",
        ):
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
        database_url = (
            "postgresql+psycopg://synthetic:synthetic@db.invalid/sro"
            "?sslmode=verify-full&gssencmode=disable"
        )
        redis_url = "rediss://synthetic:synthetic@cache.invalid/0"
        migration_database_url = (
            "postgresql+psycopg://migrator:synthetic@db.invalid/sro"
            "?sslmode=verify-full&gssencmode=disable"
        )

        settings = self.load_settings(
            APP_DEBUG="0",
            APP_SECRET_KEY=secret,
            DATABASE_URL=database_url,
            MIGRATION_DATABASE_URL=migration_database_url,
            REDIS_URL=redis_url,
        )

        self.assertFalse(settings.debug)
        self.assertEqual(settings.secret_key, secret)
        self.assertEqual(settings.database_url, database_url)
        self.assertEqual(settings.migration_database_url, migration_database_url)
        self.assertEqual(settings.redis_url, redis_url)

    def test_production_mode_requires_explicit_database_and_redis_urls(self) -> None:
        common = {
            "APP_DEBUG": "0",
            "APP_SECRET_KEY": "synthetic-test-secret-with-at-least-32-chars",
        }
        for missing, configured in (
            (
                "DATABASE_URL",
                {
                    "MIGRATION_DATABASE_URL": "postgresql://migrator:synthetic@db.invalid/sro",
                    "REDIS_URL": "redis://synthetic.invalid/0",
                },
            ),
            (
                "MIGRATION_DATABASE_URL",
                {
                    "DATABASE_URL": "postgresql://runtime:synthetic@db.invalid/sro",
                    "REDIS_URL": "redis://synthetic.invalid/0",
                },
            ),
            (
                "REDIS_URL",
                {
                    "DATABASE_URL": "postgresql://runtime:synthetic@db.invalid/sro",
                    "MIGRATION_DATABASE_URL": "postgresql://migrator:synthetic@db.invalid/sro",
                },
            ),
        ):
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(ValueError, missing):
                    self.load_settings(**common, **configured)

    def test_production_mode_rejects_malformed_migration_database_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "Database URLs are invalid"):
            self.load_settings(
                APP_DEBUG="0",
                APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                DATABASE_URL="postgresql://runtime:synthetic@db.invalid/sro",
                MIGRATION_DATABASE_URL="not a database url",
                REDIS_URL="redis://synthetic:synthetic@cache.invalid/0",
            )

    def test_production_migration_url_must_target_the_runtime_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "same database"):
            self.load_settings(
                APP_DEBUG="0",
                APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                DATABASE_URL="postgresql://runtime:synthetic@db.invalid/sro",
                MIGRATION_DATABASE_URL="postgresql://migrator:synthetic@other.invalid/sro",
                REDIS_URL="redis://synthetic:synthetic@cache.invalid/0",
            )

    def test_production_database_urls_must_use_postgresql(self) -> None:
        with self.assertRaisesRegex(ValueError, "must use PostgreSQL"):
            self.load_settings(
                APP_DEBUG="0",
                APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                DATABASE_URL="sqlite:///runtime.db",
                MIGRATION_DATABASE_URL="sqlite:///runtime.db",
                REDIS_URL="redis://synthetic:synthetic@cache.invalid/0",
            )

    def test_production_mode_requires_a_distinct_migration_role(self) -> None:
        with self.assertRaisesRegex(ValueError, "distinct explicit database role"):
            self.load_settings(
                APP_DEBUG="0",
                APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                DATABASE_URL="postgresql://runtime:synthetic@db.invalid/sro",
                MIGRATION_DATABASE_URL="postgresql://runtime:synthetic@db.invalid/sro",
                REDIS_URL="redis://synthetic:synthetic@cache.invalid/0",
            )

    def test_production_remote_postgresql_requires_full_tls_verification(self) -> None:
        for sslmode in (None, "require", "verify-ca", "prefer", "disable"):
            with self.subTest(sslmode=sslmode):
                query = "gssencmode=disable"
                if sslmode is not None:
                    query = f"sslmode={sslmode}&{query}"
                database_url = f"postgresql://runtime:synthetic@db.invalid/sro?{query}"
                migration_url = (
                    "postgresql://migrator:synthetic@db.invalid/sro?"
                    "sslmode=verify-full&gssencmode=disable"
                )
                with self.assertRaisesRegex(ValueError, "sslmode=verify-full"):
                    self.load_settings(
                        APP_DEBUG="0",
                        APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                        DATABASE_URL=database_url,
                        MIGRATION_DATABASE_URL=migration_url,
                        REDIS_URL="rediss://synthetic:synthetic@cache.invalid/0",
                    )

    def test_production_remote_postgresql_disables_gssapi_downgrade(self) -> None:
        with self.assertRaisesRegex(ValueError, "gssencmode=disable"):
            self.load_settings(
                APP_DEBUG="0",
                APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                DATABASE_URL=(
                    "postgresql://runtime:synthetic@db.invalid/sro?sslmode=verify-full"
                ),
                MIGRATION_DATABASE_URL=(
                    "postgresql://migrator:synthetic@db.invalid/sro?"
                    "sslmode=verify-full&gssencmode=disable"
                ),
                REDIS_URL="rediss://synthetic:synthetic@cache.invalid/0",
            )

    def test_production_remote_redis_requires_tls_and_certificate_verification(self) -> None:
        database_url = (
            "postgresql://runtime:synthetic@db.invalid/sro?"
            "sslmode=verify-full&gssencmode=disable"
        )
        migration_url = (
            "postgresql://migrator:synthetic@db.invalid/sro?"
            "sslmode=verify-full&gssencmode=disable"
        )
        for redis_url, message in (
            ("redis://synthetic:synthetic@cache.invalid/0", "rediss://"),
            ("rediss://synthetic:synthetic@cache.invalid/0?ssl_cert_reqs=none", "certificate verification"),
            ("rediss://synthetic:synthetic@cache.invalid/0?ssl_check_hostname=false", "server hostname"),
            ("rediss://synthetic:synthetic@cache.invalid/0?ssl_check_hostname=", "server hostname"),
        ):
            with self.subTest(redis_url=redis_url), self.assertRaisesRegex(ValueError, message):
                self.load_settings(
                    APP_DEBUG="0",
                    APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                    DATABASE_URL=database_url,
                    MIGRATION_DATABASE_URL=migration_url,
                    REDIS_URL=redis_url,
                )

    def test_production_localhost_services_can_use_local_transport(self) -> None:
        settings = self.load_settings(
            APP_DEBUG="0",
            APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
            DATABASE_URL="postgresql://runtime:synthetic@localhost/sro",
            MIGRATION_DATABASE_URL="postgresql://migrator:synthetic@localhost/sro",
            REDIS_URL="redis://localhost/0",
        )

        self.assertFalse(settings.debug)
        self.assertEqual(settings.redis_url, "redis://localhost/0")

    def test_postgresql_host_query_cannot_bypass_transport_or_target_checks(self) -> None:
        with self.assertRaisesRegex(ValueError, "same database"):
            self.load_settings(
                APP_DEBUG="0",
                APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                DATABASE_URL="postgresql://runtime:synthetic@/sro?host=runtime.invalid",
                MIGRATION_DATABASE_URL="postgresql://migrator:synthetic@/sro?host=migrator.invalid",
                REDIS_URL="rediss://synthetic:synthetic@cache.invalid/0",
            )

    def test_production_rejects_libpq_query_parameters_that_hide_target(self) -> None:
        for parameter, value in (
            ("hostaddr", "198.51.100.7"),
            ("service", "synthetic"),
            ("servicefile", "/synthetic/pg_service.conf"),
            ("user", "migrator"),
            ("dbname", "other_database"),
            ("options", "-c role=synthetic_admin"),
        ):
            for overridden_setting in ("DATABASE_URL", "MIGRATION_DATABASE_URL"):
                with self.subTest(parameter=parameter, setting=overridden_setting):
                    database_url = "postgresql://runtime:synthetic@localhost/sro"
                    migration_url = "postgresql://migrator:synthetic@localhost/sro"
                    query = f"{parameter}={value}"
                    if overridden_setting == "DATABASE_URL":
                        database_url = f"{database_url}?{query}"
                    else:
                        migration_url = f"{migration_url}?{query}"
                    with self.assertRaisesRegex(ValueError, f"PostgreSQL {parameter}"):
                        self.load_settings(
                            APP_DEBUG="0",
                            APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                            DATABASE_URL=database_url,
                            MIGRATION_DATABASE_URL=migration_url,
                            REDIS_URL="redis://localhost/0",
                        )

    def test_production_requires_database_name_in_each_url_path(self) -> None:
        for missing_setting in ("DATABASE_URL", "MIGRATION_DATABASE_URL"):
            with self.subTest(setting=missing_setting), self.assertRaisesRegex(
                ValueError, "name its database in the URL path"
            ):
                self.load_settings(
                    APP_DEBUG="0",
                    APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                    DATABASE_URL=(
                        "postgresql://runtime:synthetic@localhost/"
                        if missing_setting == "DATABASE_URL"
                        else "postgresql://runtime:synthetic@localhost/sro"
                    ),
                    MIGRATION_DATABASE_URL=(
                        "postgresql://migrator:synthetic@localhost/"
                        if missing_setting == "MIGRATION_DATABASE_URL"
                        else "postgresql://migrator:synthetic@localhost/sro"
                    ),
                    REDIS_URL="redis://localhost/0",
                )

    def test_production_rejects_ambient_libpq_target_overrides(self) -> None:
        for variable in (
            "PGHOST",
            "PGHOSTADDR",
            "PGPORT",
            "PGDATABASE",
            "PGUSER",
            "PGSERVICE",
            "PGSERVICEFILE",
            "PGSYSCONFDIR",
            "PGOPTIONS",
            "PGSSLROOTCERT",
        ):
            with self.subTest(variable=variable), self.assertRaisesRegex(
                ValueError, "libpq environment overrides"
            ):
                self.load_settings(
                    APP_DEBUG="0",
                    APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                    DATABASE_URL="postgresql://runtime:synthetic@localhost/sro",
                    MIGRATION_DATABASE_URL=(
                        "postgresql://migrator:synthetic@localhost/sro"
                    ),
                    REDIS_URL="redis://localhost/0",
                    **{variable: "synthetic-override"},
                )

        with self.assertRaisesRegex(ValueError, "sslmode=verify-full"):
            self.load_settings(
                APP_DEBUG="0",
                APP_SECRET_KEY="synthetic-test-secret-with-at-least-32-chars",
                DATABASE_URL="postgresql://runtime:synthetic@/sro?host=db.invalid",
                MIGRATION_DATABASE_URL=(
                    "postgresql://migrator:synthetic@/sro?host=db.invalid&"
                    "sslmode=verify-full&gssencmode=disable"
                ),
                REDIS_URL="rediss://synthetic:synthetic@cache.invalid/0",
            )


if __name__ == "__main__":
    unittest.main()
