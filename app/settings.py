"""Настройки веб-приложения. Секреты берутся из .env, как и у робота."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sro_robot.config import ROOT, load_env

UPLOAD_DIR = ROOT / "storage" / "documents"


@dataclass
class Settings:
    database_url: str
    redis_url: str
    secret_key: str
    upload_dir: Path
    debug: bool

    @classmethod
    def load(cls) -> "Settings":
        load_env()
        debug = os.environ.get("APP_DEBUG", "1") == "1"
        secret_key = os.environ.get("APP_SECRET_KEY", "")
        if not debug and len(secret_key) < 32:
            raise ValueError(
                "APP_SECRET_KEY must contain at least 32 characters when APP_DEBUG is disabled"
            )
        upload_dir = Path(os.environ.get("SRO_UPLOAD_DIR", UPLOAD_DIR))
        upload_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql+psycopg://sro:sro_dev@localhost:55432/sro"
            ),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:56379/0"),
            # The development fallback is never accepted with debug disabled.
            secret_key=secret_key or "dev-only-not-for-production",
            upload_dir=upload_dir,
            debug=debug,
        )


settings = Settings.load()
