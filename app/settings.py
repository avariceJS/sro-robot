"""Настройки веб-приложения. Секреты берутся из .env, как и у робота."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from sro_robot.config import ROOT, _secure_private_directory, load_env

UPLOAD_DIR = ROOT / "storage" / "documents"


@dataclass
class Settings:
    database_url: str = field(repr=False)
    redis_url: str = field(repr=False)
    secret_key: str = field(repr=False)
    upload_dir: Path
    debug: bool
    ocr_task_leases_enabled: bool = False

    @classmethod
    def load(cls) -> "Settings":
        load_env()
        debug = os.environ.get("APP_DEBUG", "1") == "1"
        secret_key = os.environ.get("APP_SECRET_KEY", "")
        if not debug and len(secret_key) < 32:
            raise ValueError(
                "APP_SECRET_KEY must contain at least 32 characters when APP_DEBUG is disabled"
            )
        database_url = os.environ.get("DATABASE_URL")
        redis_url = os.environ.get("REDIS_URL")
        if not debug and not database_url:
            raise ValueError("DATABASE_URL must be explicitly configured when APP_DEBUG is disabled")
        if not debug and not redis_url:
            raise ValueError("REDIS_URL must be explicitly configured when APP_DEBUG is disabled")
        upload_dir = Path(os.environ.get("SRO_UPLOAD_DIR", UPLOAD_DIR))
        _secure_private_directory(upload_dir)
        return cls(
            database_url=database_url or "postgresql+psycopg://sro:sro_dev@localhost:55432/sro",
            redis_url=redis_url or "redis://localhost:56379/0",
            # The development fallback is never accepted with debug disabled.
            secret_key=secret_key or "dev-only-not-for-production",
            upload_dir=upload_dir,
            debug=debug,
            ocr_task_leases_enabled=os.environ.get("OCR_TASK_LEASES_ENABLED", "0") == "1",
        )


settings = Settings.load()
