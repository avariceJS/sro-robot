#!/usr/bin/env python3
"""Start an RQ worker using the private application Redis URL."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, Worker

from app.db.base import engine
from app.db.schema import SchemaRevisionUnavailable, require_current_schema_revision
from app.settings import settings


class WorkerSchemaUnavailable(RuntimeError):
    """The worker cannot safely process jobs against this database schema."""


def require_current_schema() -> None:
    """Fail closed unless PostgreSQL is at the application's exact Alembic head."""
    try:
        with engine.connect() as connection:
            require_current_schema_revision(connection)
    except SchemaRevisionUnavailable:
        raise WorkerSchemaUnavailable from None
    except Exception:
        raise WorkerSchemaUnavailable from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queues", nargs="+", help="RQ queue names to serve")
    args = parser.parse_args(argv)

    try:
        require_current_schema()
    except WorkerSchemaUnavailable:
        print(
            "Отказ запуска worker: схема базы недоступна или не соответствует текущей ревизии.",
            file=sys.stderr,
        )
        return 2

    connection = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=5,
    )
    try:
        try:
            connection.ping()
        except (RedisError, OSError, TimeoutError) as exc:
            print(
                "Отказ запуска worker: очередь недоступна "
                f"error_type={type(exc).__name__}.",
                file=sys.stderr,
            )
            return 2
        queues = [Queue(name, connection=connection) for name in args.queues]
        worker = Worker(queues, connection=connection)
        try:
            worker.work()
        except (RedisError, OSError, TimeoutError) as exc:
            print(
                "Worker остановлен: очередь недоступна "
                f"error_type={type(exc).__name__}.",
                file=sys.stderr,
            )
            return 2
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
