"""Подключение к базе и сессии SQLAlchemy."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..settings import settings

def _create_database_engine(database_url: str):
    return create_engine(
        database_url,
        connect_args={"connect_timeout": 5},
        pool_pre_ping=True,
        pool_timeout=5,
        future=True,
    )


engine = _create_database_engine(settings.database_url)
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
