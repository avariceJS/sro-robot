from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd()))

from app.settings import Settings


SECRET = "synthetic-test-secret-with-at-least-32-chars"
MIGRATION_URL = "postgresql://migrator:synthetic@localhost/sro"
REDIS_URL = "redis://localhost/0"
SCRATCH = Path(os.environ["PPV_SCRATCH"])


def load_settings(database_url: str, extra: dict[str, str] | None = None) -> Settings:
    environment = {
        "APP_DEBUG": "0",
        "APP_SECRET_KEY": SECRET,
        "DATABASE_URL": database_url,
        "MIGRATION_DATABASE_URL": MIGRATION_URL,
        "REDIS_URL": REDIS_URL,
    }
    environment.update(extra or {})
    with tempfile.TemporaryDirectory(prefix="sro-ppv-upload-", dir=SCRATCH) as upload_dir:
        environment["SRO_UPLOAD_DIR"] = upload_dir
        with patch("app.settings.load_env"), patch.dict(os.environ, environment, clear=True):
            return Settings.load()


def assert_rejected(database_url: str, extra: dict[str, str], expected: str) -> None:
    error = ""
    try:
        load_settings(database_url, extra)
    except ValueError as exc:
        error = str(exc)
    print("PPV_REACHED", flush=True)
    assert expected in error, f"expected {expected!r} rejection, observed {error!r}"
    print("target override rejected")


def control() -> None:
    settings = load_settings(
        "postgresql://runtime:synthetic@db.invalid/sro?"
        "sslmode=verify-full&gssencmode=disable",
        {
            "MIGRATION_DATABASE_URL": (
                "postgresql://migrator:synthetic@db.invalid/sro?"
                "sslmode=verify-full&gssencmode=disable"
            ),
            "REDIS_URL": "rediss://synthetic:synthetic@cache.invalid/0",
        },
    )
    assert settings.debug is False
    print("valid verified-transport production settings accepted")


def behavior() -> None:
    settings = load_settings(
        "postgresql://runtime:synthetic@db.invalid/sro?"
        "sslmode=verify-full&gssencmode=disable",
        {
            "MIGRATION_DATABASE_URL": (
                "postgresql://migrator:synthetic@db.invalid/sro?"
                "sslmode=verify-full&gssencmode=disable"
            ),
            "REDIS_URL": "rediss://synthetic:synthetic@cache.invalid/0",
        },
    )
    print(f"debug={int(settings.debug)};redis_scheme={settings.redis_url.split(':', 1)[0]}")


def security() -> None:
    environment = {
        "APP_DEBUG": "1",
        "PGHOSTADDR": "198.51.100.7",
    }
    with tempfile.TemporaryDirectory(prefix="sro-ppv-debug-upload-", dir=SCRATCH) as upload_dir:
        environment["SRO_UPLOAD_DIR"] = upload_dir
        with patch("app.settings.load_env"), patch.dict(os.environ, environment, clear=True):
            settings = Settings.load()
    assert settings.debug is True
    print("debug-mode configuration remains available")


def exploit_hostaddr() -> None:
    assert_rejected(
        "postgresql://runtime:synthetic@localhost/sro?hostaddr=198.51.100.7",
        {},
        "PostgreSQL hostaddr",
    )


def variant_environment_hostaddr() -> None:
    assert_rejected(
        "postgresql://runtime:synthetic@localhost/sro",
        {"PGHOSTADDR": "198.51.100.7"},
        "libpq environment overrides",
    )


def variant_service() -> None:
    assert_rejected(
        "postgresql://runtime:synthetic@localhost/sro?service=synthetic",
        {},
        "PostgreSQL service",
    )


def variant_user() -> None:
    assert_rejected(
        "postgresql://runtime:synthetic@localhost/sro?user=migrator",
        {},
        "PostgreSQL user",
    )


def variant_database() -> None:
    assert_rejected(
        "postgresql://runtime:synthetic@localhost/sro?dbname=other_database",
        {},
        "PostgreSQL dbname",
    )


MODES = {
    "control": control,
    "behavior": behavior,
    "security": security,
    "exploit-hostaddr": exploit_hostaddr,
    "variant-environment-hostaddr": variant_environment_hostaddr,
    "variant-service": variant_service,
    "variant-user": variant_user,
    "variant-database": variant_database,
}


if __name__ == "__main__":
    MODES[sys.argv[1]]()
