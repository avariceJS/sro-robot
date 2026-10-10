"""Настройки веб-приложения. Секреты берутся из .env, как и у робота."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from sro_robot.config import ROOT, load_env

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
        upload_dir = Path(os.environ.get("SRO_UPLOAD_DIR", UPLOAD_DIR))
        upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            # Documents contain personal data; do not rely on the process umask.
            if upload_dir.stat().st_uid != os.geteuid():
                raise ValueError("SRO_UPLOAD_DIR must be owned by the service user")
            upload_dir.chmod(0o700)
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql+psycopg://sro:sro_dev@localhost:55432/sro"
            ),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:56379/0"),
            # The development fallback is never accepted with debug disabled.
            secret_key=secret_key or "dev-only-not-for-production",
            upload_dir=upload_dir,
            debug=debug,
            ocr_task_leases_enabled=os.environ.get("OCR_TASK_LEASES_ENABLED", "0") == "1",
        )


settings = Settings.load()
