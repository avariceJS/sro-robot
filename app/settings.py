"""Настройки веб-приложения. Секреты берутся из .env, как и у робота."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import make_url

from sro_robot.config import ROOT, _secure_private_directory, load_env

UPLOAD_DIR = ROOT / "storage" / "documents"


@dataclass
class Settings:
    database_url: str = field(repr=False)
    redis_url: str = field(repr=False)
    secret_key: str = field(repr=False)
    upload_dir: Path
    debug: bool
    migration_database_url: str | None = field(default=None, repr=False)
    ocr_task_leases_enabled: bool = False
    database_pool_size: int = 5
    database_max_overflow: int = 10
    database_pool_timeout_seconds: int = 5

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
        migration_database_url = os.environ.get("MIGRATION_DATABASE_URL")
        redis_url = os.environ.get("REDIS_URL")
        if not debug and not database_url:
            raise ValueError("DATABASE_URL must be explicitly configured when APP_DEBUG is disabled")
        if not debug and not migration_database_url:
            raise ValueError(
                "MIGRATION_DATABASE_URL must be explicitly configured when APP_DEBUG is disabled"
            )
        if not debug and not redis_url:
            raise ValueError("REDIS_URL must be explicitly configured when APP_DEBUG is disabled")
        if not debug and database_url and migration_database_url:
            try:
                runtime_url = make_url(database_url)
                migration_url = make_url(migration_database_url)
            except Exception as exc:
                raise ValueError("Database URLs are invalid") from exc
            runtime_target = (
                runtime_url.drivername.partition("+")[0],
                runtime_url.host,
                runtime_url.port,
                runtime_url.database,
            )
            migration_target = (
                migration_url.drivername.partition("+")[0],
                migration_url.host,
                migration_url.port,
                migration_url.database,
            )
            if runtime_target[0] != "postgresql":
                raise ValueError(
                    "Production DATABASE_URL and MIGRATION_DATABASE_URL must use PostgreSQL"
                )
            if runtime_target != migration_target:
                raise ValueError(
                    "MIGRATION_DATABASE_URL must target the same database as DATABASE_URL"
                )
            if (
                not migration_url.username
                or not runtime_url.username
                or migration_url.username == runtime_url.username
            ):
                raise ValueError(
                    "MIGRATION_DATABASE_URL must use a distinct explicit database role"
                )
        database_pool_size = _positive_int_setting("DB_POOL_SIZE", 5)
        database_max_overflow = _nonnegative_int_setting("DB_MAX_OVERFLOW", 10)
        database_pool_timeout_seconds = _positive_int_setting("DB_POOL_TIMEOUT_SECONDS", 5)
        upload_dir = Path(os.environ.get("SRO_UPLOAD_DIR", UPLOAD_DIR))
        _secure_private_directory(upload_dir)
        return cls(
            database_url=database_url or "postgresql+psycopg://sro:sro_dev@localhost:55432/sro",
            migration_database_url=(
                migration_database_url
                or database_url
                or "postgresql+psycopg://sro:sro_dev@localhost:55432/sro"
            ),
            redis_url=redis_url or "redis://localhost:56379/0",
            # The development fallback is never accepted with debug disabled.
            secret_key=secret_key or "dev-only-not-for-production",
            upload_dir=upload_dir,
            debug=debug,
            ocr_task_leases_enabled=os.environ.get("OCR_TASK_LEASES_ENABLED", "0") == "1",
            database_pool_size=database_pool_size,
            database_max_overflow=database_max_overflow,
            database_pool_timeout_seconds=database_pool_timeout_seconds,
        )


def _positive_int_setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int_setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


settings = Settings.load()
