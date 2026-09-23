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
        upload_dir = Path(os.environ.get("SRO_UPLOAD_DIR", UPLOAD_DIR))
        upload_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql+psycopg://sro:sro_dev@localhost:55432/sro"
            ),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:56379/0"),
            # Ключ подписи куки сессии. На сервере обязателен свой в .env.
            secret_key=os.environ.get("APP_SECRET_KEY", "dev-only-not-for-production"),
            upload_dir=upload_dir,
            debug=os.environ.get("APP_DEBUG", "1") == "1",
        )


settings = Settings.load()
