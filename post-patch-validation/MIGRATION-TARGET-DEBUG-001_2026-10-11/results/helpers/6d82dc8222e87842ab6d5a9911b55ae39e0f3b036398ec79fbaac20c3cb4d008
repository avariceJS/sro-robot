from __future__ import annotations

import os
import sys
from unittest.mock import patch

from app.settings import Settings


mode = sys.argv[1]
scratch_uploads = os.environ["SRO_UPLOAD_DIR"]


def load_with(values: dict[str, str]) -> Settings:
    environment = {
        "APP_DEBUG": "1",
        "SRO_UPLOAD_DIR": scratch_uploads,
        **values,
    }
    with patch("app.settings.load_env"), patch.dict(os.environ, environment, clear=True):
        return Settings.load()


if mode == "mismatch-debug":
    refused = False
    try:
        load_with({
            "DATABASE_URL": "postgresql://runtime:synthetic@runtime.invalid/sro",
            "MIGRATION_DATABASE_URL": "postgresql://migrator:synthetic@other.invalid/sro",
        })
    except ValueError as exc:
        refused = "same database" in str(exc)
    print("PPV_REACHED", flush=True)
    assert refused, "debug configuration accepted a migration target different from runtime"
    print("mismatched_target=refused")
elif mode == "missing-runtime":
    refused = False
    try:
        load_with({
            "MIGRATION_DATABASE_URL": "postgresql://migrator:synthetic@other.invalid/sro",
        })
    except ValueError as exc:
        refused = "same database" in str(exc)
    print("PPV_REACHED", flush=True)
    assert refused, "explicit migration target bypassed the effective runtime fallback"
    print("fallback_target_mismatch=refused")
elif mode == "same-target":
    settings = load_with({
        "DATABASE_URL": "postgresql://runtime:synthetic@db.invalid/sro",
        "MIGRATION_DATABASE_URL": "postgresql://migrator:synthetic@db.invalid/sro",
    })
    assert settings.database_url.endswith("@db.invalid/sro")
    assert settings.migration_database_url.endswith("@db.invalid/sro")
    print("same_target=accepted")
elif mode == "production":
    with patch("app.settings.load_env"), patch.dict(
        os.environ,
        {
            "APP_DEBUG": "0",
            "APP_SECRET_KEY": "synthetic-production-secret-long-enough",
            "DATABASE_URL": "postgresql://runtime:synthetic@db.invalid/sro",
            "MIGRATION_DATABASE_URL": "postgresql://migrator:synthetic@db.invalid/sro",
            "REDIS_URL": "redis://synthetic:synthetic@cache.invalid/0",
            "SRO_UPLOAD_DIR": scratch_uploads,
        },
        clear=True,
    ):
        settings = Settings.load()
    assert not settings.debug
    print("production_roles=accepted")
else:
    raise SystemExit(f"unknown mode: {mode}")
