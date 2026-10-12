from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


checkout = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
sys.path.insert(0, str(checkout))

bootstrap_directory = tempfile.TemporaryDirectory(prefix="sro-ppv-debug-default-")
bootstrap_env = {
    "APP_DEBUG": "1",
    "DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:55432/sro",
    "REDIS_URL": "redis://synthetic:synthetic@127.0.0.1:56379/0",
    "SRO_UPLOAD_DIR": bootstrap_directory.name,
}
with patch.dict(os.environ, bootstrap_env, clear=True):
    import app.settings as settings_module


def configured_settings(overrides: dict[str, str]):
    env = {
        "DATABASE_URL": "postgresql+psycopg://runtime:synthetic@db.invalid/sro",
        "REDIS_URL": "redis://synthetic:synthetic@cache.invalid/0",
        "SRO_UPLOAD_DIR": bootstrap_directory.name,
        **overrides,
    }
    with patch.object(settings_module, "load_env"), patch.dict(os.environ, env, clear=True):
        return settings_module.Settings.load()


def production_environment() -> dict[str, str]:
    return {
        "APP_SECRET_KEY": "synthetic-session-secret-with-over-32-characters",
        "DATABASE_URL": (
            "postgresql+psycopg://runtime:synthetic@db.invalid/sro"
            "?sslmode=verify-full&gssencmode=disable"
        ),
        "MIGRATION_DATABASE_URL": (
            "postgresql+psycopg://migrator:synthetic@db.invalid/sro"
            "?sslmode=verify-full&gssencmode=disable"
        ),
        "REDIS_URL": "rediss://synthetic:synthetic@cache.invalid/0",
        "SRO_UPLOAD_DIR": bootstrap_directory.name,
    }


if mode == "missing-debug-rejects-incomplete-production":
    try:
        configured_settings({})
    except ValueError as exc:
        safe = "APP_SECRET_KEY" in str(exc)
    else:
        safe = False
    print("PPV_REACHED", flush=True)
    assert safe, "omitted APP_DEBUG silently enabled the development configuration"
    print("missing-debug-rejected")
elif mode == "missing-debug-disables-docs":
    env = production_environment()
    with patch.object(settings_module, "load_env"), patch.dict(os.environ, env, clear=True):
        importlib.reload(settings_module)
        import app.main as main_module

        main_module = importlib.reload(main_module)
        docs_disabled = main_module.app.docs_url is None
    print("PPV_REACHED", flush=True)
    assert docs_disabled, "omitted APP_DEBUG exposed interactive API documentation"
    print("missing-debug-docs-disabled")
elif mode == "explicit-development-opt-in":
    settings = configured_settings({"APP_DEBUG": "1"})
    assert settings.debug is True
    assert settings.secret_key == "dev-only-not-for-production"
    print("explicit-development-opt-in-preserved")
elif mode == "explicit-production-opt-in":
    env = production_environment()
    env["APP_DEBUG"] = "0"
    with patch.object(settings_module, "load_env"), patch.dict(os.environ, env, clear=True):
        settings = settings_module.Settings.load()
    assert settings.debug is False
    assert settings.secret_key == env["APP_SECRET_KEY"]
    assert settings.migration_database_url != settings.database_url
    print("explicit-production-configuration-preserved")
elif mode == "invalid-debug-value-rejected":
    try:
        configured_settings({"APP_DEBUG": "true"})
    except ValueError as exc:
        safe = "APP_DEBUG" in str(exc)
    else:
        safe = False
    assert safe
    print("invalid-debug-value-rejected")
elif mode in {"development-configuration-regression", "settings-regression-suite"}:
    env = {
        "APP_DEBUG": "1",
        "DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:55432/sro",
        "REDIS_URL": "redis://synthetic:synthetic@127.0.0.1:56379/0",
        "SRO_UPLOAD_DIR": bootstrap_directory.name,
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            *(
                ["tests.test_settings.SettingsSecurityTests.test_debug_mode_can_use_development_secret_fallback"]
                if mode == "development-configuration-regression"
                else ["tests.test_settings"]
            ),
        ],
        cwd=checkout,
        env=env,
        check=False,
    )
    raise SystemExit(result.returncode)
else:
    raise SystemExit(f"unknown PPV check mode: {mode}")
