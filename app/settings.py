"""Настройки веб-приложения. Секреты берутся из .env, как и у робота."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
    database_lock_timeout_ms: int = 5000

    @classmethod
    def load(cls) -> "Settings":
        load_env()
        debug = _binary_setting("APP_DEBUG", "1")
        ocr_task_leases_enabled = _binary_setting("OCR_TASK_LEASES_ENABLED", "0")
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

        development_database_url = "postgresql+psycopg://sro:sro_dev@localhost:55432/sro"
        effective_database_url = database_url or development_database_url
        effective_migration_database_url = (
            migration_database_url or database_url or development_database_url
        )
        try:
            runtime_url = make_url(effective_database_url)
            migration_url = make_url(effective_migration_database_url)
        except Exception as exc:
            raise ValueError("Database URLs are invalid") from exc
        if not debug:
            _require_explicit_postgresql_connection_inputs(
                runtime_url,
                "DATABASE_URL",
            )
            _require_explicit_postgresql_connection_inputs(
                migration_url,
                "MIGRATION_DATABASE_URL",
            )
            _reject_libpq_environment_overrides()
        runtime_host = _postgresql_host_parameter(runtime_url)
        migration_host = _postgresql_host_parameter(migration_url)
        runtime_port = _postgresql_port_parameter(runtime_url)
        migration_port = _postgresql_port_parameter(migration_url)
        runtime_target = (
            runtime_url.drivername.partition("+")[0],
            runtime_host,
            runtime_port,
            runtime_url.database,
        )
        migration_target = (
            migration_url.drivername.partition("+")[0],
            migration_host,
            migration_port,
            migration_url.database,
        )
        if runtime_target != migration_target:
            raise ValueError(
                "MIGRATION_DATABASE_URL must target the same database as DATABASE_URL"
            )

        if not debug:
            if runtime_target[0] != "postgresql":
                raise ValueError(
                    "Production DATABASE_URL and MIGRATION_DATABASE_URL must use PostgreSQL"
                )
            if (
                not migration_url.username
                or not runtime_url.username
                or migration_url.username == runtime_url.username
            ):
                raise ValueError(
                    "MIGRATION_DATABASE_URL must use a distinct explicit database role"
                )
            _require_postgresql_transport_security(runtime_url, "DATABASE_URL")
            _require_postgresql_transport_security(migration_url, "MIGRATION_DATABASE_URL")
            _require_redis_transport_security(redis_url)
        database_pool_size = _positive_int_setting("DB_POOL_SIZE", 5)
        database_max_overflow = _nonnegative_int_setting("DB_MAX_OVERFLOW", 10)
        database_pool_timeout_seconds = _positive_int_setting("DB_POOL_TIMEOUT_SECONDS", 5)
        database_lock_timeout_ms = _positive_int_setting("DB_LOCK_TIMEOUT_MS", 5000)
        upload_dir = Path(os.environ.get("SRO_UPLOAD_DIR", UPLOAD_DIR))
        _secure_private_directory(upload_dir)
        return cls(
            database_url=effective_database_url,
            migration_database_url=effective_migration_database_url,
            redis_url=redis_url or "redis://localhost:56379/0",
            # The development fallback is never accepted with debug disabled.
            secret_key=secret_key or "dev-only-not-for-production",
            upload_dir=upload_dir,
            debug=debug,
            ocr_task_leases_enabled=ocr_task_leases_enabled,
            database_pool_size=database_pool_size,
            database_max_overflow=database_max_overflow,
            database_pool_timeout_seconds=database_pool_timeout_seconds,
            database_lock_timeout_ms=database_lock_timeout_ms,
        )


def _binary_setting(name: str, default: str) -> bool:
    value = os.environ.get(name, default)
    if value not in {"0", "1"}:
        raise ValueError(f"{name} must be set to 0 or 1")
    return value == "1"


def _single_postgresql_parameter(url, name: str) -> str | None:
    value = url.query.get(name)
    if value is None:
        return None
    if isinstance(value, tuple):
        if len(value) != 1:
            raise ValueError(f"PostgreSQL connection parameter {name} must occur once")
        value = value[0]
    return str(value)


def _require_explicit_postgresql_connection_inputs(url, setting_name: str) -> None:
    if not url.database:
        raise ValueError(f"{setting_name} must name its database in the URL path")
    hidden_parameters = ("hostaddr", "service", "servicefile", "user", "dbname", "options")
    for name in hidden_parameters:
        if _single_postgresql_parameter(url, name) is not None:
            raise ValueError(
                f"{setting_name} must not use PostgreSQL {name} query parameters"
            )


def _reject_libpq_environment_overrides() -> None:
    override_names = (
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
    )
    configured = [name for name in override_names if name in os.environ]
    if configured:
        raise ValueError(
            "Production PostgreSQL connection must not depend on libpq environment overrides"
        )


def _postgresql_host_values(url) -> tuple[str, ...]:
    query_host = _single_postgresql_parameter(url, "host")
    host = query_host if query_host is not None else url.host
    if not host:
        # libpq uses a local Unix-domain socket when no host is configured.
        return ()
    hosts = tuple(part.strip().strip("[]") for part in host.split(","))
    if any(not host_part for host_part in hosts):
        raise ValueError("PostgreSQL host must not be empty")
    return hosts


def _postgresql_host_parameter(url) -> str | None:
    hosts = _postgresql_host_values(url)
    return ",".join(hosts) if hosts else None


def _postgresql_port_parameter(url) -> int | None:
    query_port = _single_postgresql_parameter(url, "port")
    if query_port is None:
        return url.port
    try:
        port = int(query_port)
    except ValueError as exc:
        raise ValueError("PostgreSQL port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PostgreSQL port must be between 1 and 65535")
    return port


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return True
    normalized = host.strip().strip("[]").rstrip(".").casefold()
    if normalized == "localhost":
        return True
    if normalized.startswith("/"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped.is_loopback if mapped is not None else address.is_loopback


def _require_postgresql_transport_security(url, setting_name: str) -> None:
    if all(_is_loopback_host(host) for host in _postgresql_host_values(url)):
        return
    sslmode = _single_postgresql_parameter(url, "sslmode")
    if sslmode != "verify-full":
        raise ValueError(f"{setting_name} must use sslmode=verify-full for network hosts")
    gssencmode = _single_postgresql_parameter(url, "gssencmode")
    if gssencmode != "disable":
        raise ValueError(f"{setting_name} must set gssencmode=disable to require TLS")


def _require_redis_transport_security(redis_url: str) -> None:
    try:
        parsed = urlsplit(redis_url)
        hostname = parsed.hostname
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError as exc:
        raise ValueError("REDIS_URL is invalid") from exc

    if parsed.scheme == "unix":
        if not parsed.path.startswith("/") or not parsed.path:
            raise ValueError("REDIS_URL unix socket path must be absolute")
        return
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError("REDIS_URL must use redis://, rediss://, or unix://")

    host = hostname or "localhost"
    if parsed.scheme == "redis":
        if not _is_loopback_host(host):
            raise ValueError("REDIS_URL must use rediss:// for network hosts")
        return

    cert_reqs = query.get("ssl_cert_reqs")
    if cert_reqs is not None and (len(cert_reqs) != 1 or cert_reqs[0].casefold() != "required"):
        raise ValueError("REDIS_URL must require certificate verification")
    hostname_check = query.get("ssl_check_hostname")
    if hostname_check is not None and (
        len(hostname_check) != 1
        or hostname_check[0].strip().upper() not in {"1", "TRUE", "YES", "ON"}
    ):
        raise ValueError("REDIS_URL must verify the server hostname")


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
