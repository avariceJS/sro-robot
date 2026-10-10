"""Служебные команды платформы.

    .venv/bin/python -m app.cli init-db
    .venv/bin/python -m app.cli create-user ivan@example.ru
"""

from __future__ import annotations

import argparse
import getpass
import sys

from alembic import command as alembic_command
from alembic.autogenerate.api import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from redis import Redis
from rq import Queue, Worker
from sqlalchemy import func, inspect, select
from sqlalchemy.engine import Connection

from sro_robot.config import ROOT

from .db.base import Base, engine, session_scope
from .db import models as _models  # Register the application's tables in Base.metadata.
from .db.models import Task, TaskStatus
from .services import auth
from .settings import settings


def has_unversioned_tables(connection: Connection) -> bool:
    """Detect a populated schema that Alembic does not yet own."""
    revision = MigrationContext.configure(connection).get_current_revision()
    tables = set(inspect(connection).get_table_names())
    return revision is None and bool(tables - {"alembic_version"})


def cmd_init_db(args) -> int:
    if args.reset:
        print("Отказ: init-db больше не удаляет таблицы; управляйте схемой через Alembic")
        return 2
    try:
        with engine.connect() as connection:
            if has_unversioned_tables(connection):
                print(
                    "Отказ: найдены существующие таблицы без записанной ревизии Alembic. "
                    "Перед применением миграций нужны проверенная резервная копия и "
                    "точное сравнение схемы; автоматическое проставление baseline запрещено."
                )
                return 2
    except Exception as exc:
        print(f"Отказ: не удалось безопасно проверить состояние схемы: {type(exc).__name__}")
        return 2
    alembic_command.upgrade(AlembicConfig(str(ROOT / "alembic.ini")), "head")
    print("Схема базы данных обновлена до текущей ревизии")
    return 0


def cmd_schema_check(args) -> int:
    """Compare the live schema to ORM metadata without changing the database."""
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": True},
            )
            revision = context.get_current_revision()
            differences = compare_metadata(context, Base.metadata)
            tables = sorted(inspect(connection).get_table_names())
    except Exception as exc:
        print(f"Ошибка read-only проверки схемы: {type(exc).__name__}")
        return 2

    print(f"Alembic revision: {revision or 'unversioned'}")
    print(f"Tables: {', '.join(tables) if tables else '(none)'}")
    if differences:
        print(f"Schema differs from ORM metadata ({len(differences)} difference(s)):")
        for difference in differences:
            print(f"- {difference!r}")
        return 1

    print("Schema matches ORM metadata; no migration or stamp was applied")
    return 0


def cmd_ops_status(args) -> int:
    """Report queue and task health using read-only database/Redis operations."""
    client = None
    try:
        with engine.connect() as connection:
            counts = connection.execute(
                select(Task.status, func.count()).group_by(Task.status)
            ).all()
            oldest_recognizing = connection.execute(
                select(func.min(Task.updated_at)).where(Task.status == TaskStatus.recognizing)
            ).scalar_one()
    except Exception as exc:
        print(f"Проверка задач недоступна: {type(exc).__name__}")
        return 2

    try:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        ocr_count = Queue("ocr", connection=client).count
        sro_count = Queue("sro", connection=client).count
        worker_count = len(Worker.all(connection=client))
    except Exception as exc:
        print(f"Проверка очередей недоступна: {type(exc).__name__}")
        return 2
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                print(f"Закрытие соединения Redis не удалось: {type(exc).__name__}")

    print("Task counts:")
    for status, count in sorted(counts, key=lambda row: str(getattr(row[0], "value", row[0]))):
        name = getattr(status, "value", status)
        print(f"- {name}: {count}")
    print(f"Oldest recognizing updated_at: {oldest_recognizing or 'none'}")
    print(f"Queue depth: ocr={ocr_count} sro={sro_count}")
    print(f"Registered RQ workers: {worker_count}")
    print("Read-only check; no task was changed or re-enqueued")
    return 0


def cmd_create_user(args) -> int:
    password = args.password or getpass.getpass("Пароль для входа в приложение: ")
    if not password:
        print("Пустой пароль не годится")
        return 1
    with session_scope() as db:
        user = auth.create_user(
            db, args.login, password, full_name=args.name, sro_login=args.sro_login
        )
        print(f"Создан пользователь {user.login}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-db", help="применить Alembic migrations до текущей ревизии")
    p_init.add_argument(
        "--reset",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p_init.set_defaults(func=cmd_init_db)

    p_schema = sub.add_parser(
        "schema-check",
        help="сравнить схему БД с моделями без изменений в базе",
    )
    p_schema.set_defaults(func=cmd_schema_check)

    p_ops = sub.add_parser(
        "ops-status",
        help="показать read-only состояние заданий, очередей и воркеров",
    )
    p_ops.set_defaults(func=cmd_ops_status)

    p_user = sub.add_parser("create-user", help="завести сотрудника")
    p_user.add_argument("login", help="как в СРО, например «Иванова М.И.»")
    p_user.add_argument("--name", default="")
    p_user.add_argument("--sro-login", default="", help="если отличается от логина у нас")
    p_user.add_argument("--password", default="", help="лучше не указывать: спросим скрытым вводом")
    p_user.set_defaults(func=cmd_create_user)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
