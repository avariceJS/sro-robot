from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd()))

from app.settings import Settings

SECRET = "synthetic-test-secret-with-at-least-32-chars"
RUNTIME_URL = "postgresql://runtime:synthetic@db.invalid/sro?sslmode=verify-full&gssencmode=disable"
MIGRATION_URL = "postgresql://migrator:synthetic@db.invalid/sro?sslmode=verify-full&gssencmode=disable"
REDIS_URL = "rediss://synthetic:synthetic@cache.invalid/0"
SCRATCH = Path(os.environ["PPV_SCRATCH"])


def load_settings(extra: dict[str, str] | None = None, *, debug: bool = False) -> Settings:
    environment = {
        "APP_DEBUG": "1" if debug else "0",
        "APP_SECRET_KEY": SECRET,
        "DATABASE_URL": RUNTIME_URL,
        "MIGRATION_DATABASE_URL": MIGRATION_URL,
        "REDIS_URL": REDIS_URL,
    }
    environment.update(extra or {})
    with tempfile.TemporaryDirectory(prefix="sro-ppv-ca-upload-", dir=SCRATCH) as upload_dir:
        environment["SRO_UPLOAD_DIR"] = upload_dir
        with patch("app.settings.load_env"), patch.dict(os.environ, environment, clear=True):
            return Settings.load()


def assert_rejected(extra: dict[str, str]) -> None:
    error = ""
    try:
        load_settings(extra)
    except ValueError as exc:
        error = str(exc)
    print("PPV_REACHED", flush=True)
    assert "libpq environment overrides" in error, f"unexpected acceptance/error: {error!r}"
    print("ambient CA override rejected")


def exploit() -> None:
    assert_rejected({"PGSSLROOTCERT": "/synthetic/untrusted-root.pem"})


def variant_runtime_tls() -> None:
    assert_rejected({"PGSSLROOTCERT": "/synthetic/runtime-root.pem"})


def variant_migration_tls() -> None:
    assert_rejected({"PGSSLROOTCERT": "/synthetic/migration-root.pem"})


def control() -> None:
    settings = load_settings()
    assert settings.debug is False
    print("explicit verified TLS settings accepted")


def behavior() -> None:
    settings = load_settings()
    print(f"debug={int(settings.debug)};redis_scheme={settings.redis_url.split(':', 1)[0]}")


def security_debug() -> None:
    settings = load_settings({"PGSSLROOTCERT": "/synthetic/debug-root.pem"}, debug=True)
    assert settings.debug is True
    print("debug settings remain available under synthetic libpq defaults")


MODES = {
    "exploit": exploit,
    "variant-runtime-tls": variant_runtime_tls,
    "variant-migration-tls": variant_migration_tls,
    "control": control,
    "behavior": behavior,
    "security-debug": security_debug,
}

if __name__ == "__main__":
    MODES[sys.argv[1]]()
