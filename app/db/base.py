"""Подключение к базе и сессии SQLAlchemy."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..settings import settings
from .schema import configure_postgresql_transaction_timeouts
from .role_policy import enforce_runtime_privilege_ceiling


def _configure_runtime_connection(
    dbapi_connection,
    _connection_record,
    *checkout_args,
    enforce_role_policy: bool,
) -> None:
    """Pin schema resolution and recheck the production role before each checkout."""
    cursor = dbapi_connection.cursor()
    try:
        # Listing pg_temp after public prevents session-local temporary tables
        # from shadowing unqualified application relations on pooled sessions.
        cursor.execute("SET SESSION search_path TO public, pg_temp")
        if enforce_role_policy:
            enforce_runtime_privilege_ceiling(cursor)
    finally:
        cursor.close()


def _set_public_search_path(dbapi_connection, connection_record, *checkout_args) -> None:
    """Keep migration-engine connections on the modeled public schema."""
    _configure_runtime_connection(
        dbapi_connection,
        connection_record,
        *checkout_args,
        enforce_role_policy=False,
    )


def _create_database_engine(
    database_url: str,
    *,
    pool_size: int = settings.database_pool_size,
    max_overflow: int = settings.database_max_overflow,
    pool_timeout: int = settings.database_pool_timeout_seconds,
    enforce_role_policy: bool = False,
):
    engine = create_engine(
        database_url,
        connect_args={"connect_timeout": 5},
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        future=True,
    )
    if make_url(database_url).get_backend_name() == "postgresql":
        def configure_connection(connection, record, *args):
            _configure_runtime_connection(
                connection,
                record,
                *args,
                enforce_role_policy=enforce_role_policy,
            )

        event.listen(engine, "connect", configure_connection)
        event.listen(engine, "checkout", configure_connection)
    return engine


engine = _create_database_engine(
    settings.database_url,
    enforce_role_policy=not settings.debug,
)
if settings.migration_database_url:
    migration_engine = _create_database_engine(
        settings.migration_database_url,
        pool_size=1,
        max_overflow=0,
    )
elif settings.debug:
    migration_engine = engine
else:
    # Runtime-only web/worker processes must never inherit a migration URL or
    # silently reuse their restricted runtime engine for schema changes.
    migration_engine = None
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@event.listens_for(SessionLocal, "after_begin")
def _set_session_transaction_timeouts(_session, _transaction, connection) -> None:
    """Bound every PostgreSQL ORM transaction independently of checked-out connection state."""
    configure_postgresql_transaction_timeouts(connection)


class Base(DeclarativeBase):
    pass


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """Зависимость FastAPI."""
    with session_scope() as session:
        yield session
