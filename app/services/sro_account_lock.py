"""Cross-worker SRO account gate for isolated form preparation and Save.

Shared preparation locks permit parallel browser contexts. An exclusive Save
lock waits for those contexts and for any other Save under the same SRO login.
The lock is process-scoped coordination, not an external write receipt.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import time
from typing import Iterator

from sqlalchemy import text

from ..db.base import engine, session_scope
from ..db.models import User


class SroAccountBusy(RuntimeError):
    """No account lock was acquired within the bounded wait."""


def _lock_key(login: str) -> int:
    normalized = login.strip().casefold()
    if not normalized:
        raise ValueError("SRO account identity is missing")
    digest = hashlib.blake2b(
        ("sro-account:\0" + normalized).encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def sro_account_lease(user_id: int, *, shared: bool, wait_s: float = 45.0) -> Iterator[None]:
    """Hold an advisory lock from before task claim until the browser closes.

    The dedicated autocommit connection is explicitly unlocked before pool
    return. If unlock fails, invalidate it so a pooled connection cannot keep
    an orphaned account lock. Waiting expires before the RQ job timeout and
    leaves the task unclaimed.
    """
    if type(user_id) is not int or user_id < 1 or not 0 < wait_s <= 120:
        raise ValueError("Invalid SRO account lease request")
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("SRO account identity is missing")
        key = _lock_key(user.sro_login)

    acquire = text(
        "SELECT pg_try_advisory_lock_shared(:key)"
        if shared else "SELECT pg_try_advisory_lock(:key)"
    )
    release = text(
        "SELECT pg_advisory_unlock_shared(:key)"
        if shared else "SELECT pg_advisory_unlock(:key)"
    )
    connection = engine.connect()
    try:
        # The engine's raw DBAPI checkout hook executes SET SESSION search_path.
        # psycopg enters INTRANS before SQLAlchemy tracks any transaction, so
        # Connection.commit() alone cannot clear it. Commit the DBAPI state,
        # then clear SQLAlchemy's autobegin before changing isolation level.
        connection.exec_driver_sql("COMMIT")
        connection.commit()
        connection.execution_options(isolation_level="AUTOCOMMIT")
    except Exception:
        connection.invalidate()
        connection.close()
        raise
    acquired = False
    try:
        deadline = time.monotonic() + wait_s
        while True:
            if connection.scalar(acquire, {"key": key}) is True:
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise SroAccountBusy("SRO account is busy")
            time.sleep(0.2)
        yield
    finally:
        try:
            if acquired:
                try:
                    if connection.scalar(release, {"key": key}) is not True:
                        connection.invalidate()
                        raise RuntimeError("SRO account lease was lost")
                except Exception:
                    connection.invalidate()
                    raise
        finally:
            connection.close()
