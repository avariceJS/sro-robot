#!/usr/bin/env python3
"""Start an RQ worker using the private application Redis URL."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, Worker
from rq.worker_pool import WorkerPool

from app.db.base import engine
from app.db.schema import SchemaRevisionUnavailable, require_current_schema_revision
from app.settings import settings


class WorkerSchemaUnavailable(RuntimeError):
    """The worker cannot safely process jobs against this database schema."""


class WorkerPoolCleanupError(RuntimeError):
    """An RQ startup failure left a child that could not be reaped safely."""


class WorkerPoolStartError(RuntimeError):
    """RQ pool startup failed outside the expected Redis/I/O error classes."""


def validate_worker_configuration(
    workers: int, queues: Sequence[str], leases_enabled: bool
) -> None:
    """Admit bounded pools only for queues with a durable claim boundary."""
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    if workers > 1:
        queue_names = list(queues)
        if queue_names == ["ocr"] and leases_enabled:
            return
        if queue_names == ["sro_preview"]:
            # Each preview opens a new browser context. Task claims serialize
            # duplicate deliveries and durable Save requests before I/O.
            return
        raise ValueError(
            "parallel workers require only leased OCR or isolated SRO preview jobs"
        )


def stop_and_reap_partial_pool(pool, timeout: float = 10.0) -> bool:
    """Stop and reap only process objects recorded by this RQ pool instance."""
    worker_data = tuple(pool.worker_dict.values())
    # Do not call RQ's request_stop here: RQ 2.12 sends signals by stored PID,
    # while Process methods track/reap the owned child and avoid signaling a
    # recycled PID. Normal pool shutdown remains under RQ's own handlers.
    remaining = [data.process for data in worker_data if _process_alive(data.process)]
    for process in remaining:
        try:
            process.terminate()
        except BaseException:
            pass

    deadline = time.monotonic() + max(0.0, timeout * 0.7)
    for process in remaining:
        try:
            process.join(max(0.0, deadline - time.monotonic()))
        except BaseException:
            pass

    remaining = [process for process in remaining if _process_alive(process)]
    for process in remaining:
        try:
            # Second TERM asks RQ's worker to stop its local OCR horse.
            process.terminate()
        except BaseException:
            pass
    terminate_deadline = time.monotonic() + max(0.0, timeout * 0.2)
    for process in remaining:
        try:
            process.join(max(0.0, terminate_deadline - time.monotonic()))
        except BaseException:
            pass

    remaining = [process for process in remaining if _process_alive(process)]
    for process in remaining:
        try:
            process.kill()
        except BaseException:
            pass
    final_deadline = time.monotonic() + max(0.0, timeout * 0.1)
    for process in remaining:
        try:
            process.join(max(0.0, final_deadline - time.monotonic()))
        except BaseException:
            pass
    return not any(_process_alive(data.process) for data in worker_data)


def _process_alive(process) -> bool:
    try:
        return bool(process.is_alive())
    except BaseException:
        return True


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
    parser.add_argument(
        "--workers", type=int, default=1,
        help="worker processes (1-4; parallel OCR needs leases, SRO preview uses isolated contexts)",
    )
    args = parser.parse_args(argv)

    try:
        validate_worker_configuration(
            args.workers, args.queues, settings.ocr_task_leases_enabled
        )
    except ValueError as exc:
        print(f"Отказ запуска worker: {exc}.", file=sys.stderr)
        return 2

    try:
        require_current_schema()
    except WorkerSchemaUnavailable:
        print(
            "Отказ запуска worker: схема базы недоступна или не соответствует текущей ревизии.",
            file=sys.stderr,
        )
        return 2

    if args.workers > 1:
        # The schema preflight opened and closed a QueuePool connection. Ensure
        # forked RQ workers cannot inherit its live DBAPI socket.
        engine.dispose()

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
        try:
            if args.workers == 1:
                # Keep the established single-worker path unchanged by default.
                Worker(queues, connection=connection).work()
            else:
                pool = WorkerPool(
                    queues, connection=connection, num_workers=args.workers
                )
                try:
                    pool.start()
                except BaseException as original_error:
                    if not stop_and_reap_partial_pool(pool):
                        raise WorkerPoolCleanupError(
                            "RQ worker pool child could not be reaped"
                        ) from original_error
                    if isinstance(original_error, Exception) and not isinstance(
                        original_error, (RedisError, OSError, TimeoutError)
                    ):
                        raise WorkerPoolStartError(
                            "RQ worker pool failed during startup"
                        ) from original_error
                    raise
        except (
            RedisError,
            OSError,
            TimeoutError,
            WorkerPoolCleanupError,
            WorkerPoolStartError,
        ) as exc:
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
