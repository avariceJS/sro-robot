"""Подключение к базе и сессии SQLAlchemy."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..settings import settings


def _set_public_search_path(dbapi_connection, _connection_record, *_checkout_args) -> None:
    """Keep unqualified ORM and migration names anchored to the modeled schema."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET SESSION search_path TO public")
    finally:
        cursor.close()


def _create_database_engine(
    database_url: str,
    *,
    pool_size: int = settings.database_pool_size,
    max_overflow: int = settings.database_max_overflow,
    pool_timeout: int = settings.database_pool_timeout_seconds,
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
        event.listen(engine, "connect", _set_public_search_path)
        event.listen(engine, "checkout", _set_public_search_path)
    return engine


engine = _create_database_engine(settings.database_url)
if settings.migration_database_url:
    migration_engine = _create_database_engine(
        settings.migration_database_url,
        pool_size=1,
        max_overflow=0,
    )
else:
    migration_engine = engine
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


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
