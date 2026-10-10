from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool, text
from sqlalchemy.engine import make_url

from app.db import models  # noqa: F401 - register tables
from app.db.base import Base, _set_public_search_path
from app.settings import settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
MIGRATION_ADVISORY_LOCK_ID = 74173021533708100


def run_migrations_offline() -> None:
    context.configure(
        url=settings.migration_database_url or settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Escape percent signs because ConfigParser treats them as interpolation.
    migration_database_url = settings.migration_database_url or settings.database_url
    config.set_main_option("sqlalchemy.url", migration_database_url.replace("%", "%%"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if make_url(migration_database_url).get_backend_name() == "postgresql":
        event.listen(connectable, "connect", _set_public_search_path)
    with connectable.connect() as connection:
        if connection.dialect.name == "postgresql":
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": MIGRATION_ADVISORY_LOCK_ID},
            ).scalar_one()
            # The advisory lock is session-scoped. End the implicit transaction
            # used to acquire it before Alembic starts its migration transaction.
            connection.commit()
            if not acquired:
                raise RuntimeError("Another SRO Robot schema migration is already running")

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
