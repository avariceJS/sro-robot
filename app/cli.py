"""Служебные команды платформы.

    .venv/bin/python -m app.cli init-db
    .venv/bin/python -m app.cli create-user ivan@example.ru
"""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import sys

from alembic import command as alembic_command
from alembic.autogenerate.api import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from redis import Redis
from rq import Queue, Worker
from sqlalchemy import Enum as SqlEnum
from sqlalchemy import Integer, func, inspect, select, text
from sqlalchemy.engine import Connection

from sro_robot.config import ROOT

from .db.base import Base, engine, session_scope
from .db import models as _models  # Register the application's tables in Base.metadata.
from .db.models import Task, TaskStatus
from .services.leases import reconcilable_ocr_task_ids
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
            enum_differences = compare_postgresql_enums(connection)
            object_differences = compare_postgresql_objects(connection)
    except Exception as exc:
        print(f"Ошибка read-only проверки схемы: {type(exc).__name__}")
        return 2

    print(f"Alembic revision: {revision or 'unversioned'}")
    print(f"Tables: {', '.join(tables) if tables else '(none)'}")
    if differences or enum_differences or object_differences:
        total_differences = len(differences) + len(enum_differences) + len(object_differences)
        print(f"Schema differs from ORM metadata ({total_differences} difference(s)):")
        for difference in differences:
            print(f"- {difference!r}")
        for difference in enum_differences:
            print(f"- PostgreSQL enum mismatch: {difference}")
        for difference in object_differences:
            print(f"- PostgreSQL object mismatch: {difference}")
        return 1

    print("Schema matches ORM metadata; no migration or stamp was applied")
    return 0


def compare_postgresql_enums(connection) -> list[str]:
    """Compare named native enum labels and order without mutating the database."""
    if connection.dialect.name != "postgresql":
        return []

    expected = expected_postgresql_enums()
    actual = {
        enum_info["name"]: list(enum_info["labels"])
        for enum_info in inspect(connection).get_enums(schema="public")
    }
    mismatches = []
    for name in sorted(expected.keys() | actual.keys()):
        expected_labels = expected.get(name)
        actual_labels = actual.get(name)
        if expected_labels != actual_labels:
            mismatches.append(
                f"{name}: expected={expected_labels!r}, actual={actual_labels!r}"
            )
    return mismatches


def expected_postgresql_enums() -> dict[str, list[str]]:
    """Collect every named native enum referenced by application metadata."""
    expected: dict[str, list[str]] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            column_type = column.type
            if isinstance(column_type, SqlEnum) and column_type.native_enum and column_type.name:
                expected[column_type.name] = list(column_type.enums)
    return expected


def compare_postgresql_objects(connection) -> list[str]:
    """Check public views, foreign tables and serial sequences against metadata."""
    if connection.dialect.name != "postgresql":
        return []

    expected_sequences = expected_postgresql_sequences()

    inspector = inspect(connection)
    actual_sequences = set(inspector.get_sequence_names(schema="public"))
    actual_views = set(inspector.get_view_names(schema="public"))
    get_materialized_views = getattr(inspector, "get_materialized_view_names", None)
    actual_materialized_views = (
        set(get_materialized_views(schema="public")) if get_materialized_views else set()
    )
    get_foreign_tables = getattr(inspector, "get_foreign_table_names", None)
    actual_foreign_tables = set(get_foreign_tables(schema="public")) if get_foreign_tables else set()
    trigger_names = connection.execute(
        text(
            "SELECT c.relname || '.' || t.tgname "
            "FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND NOT t.tgisinternal "
            "ORDER BY c.relname, t.tgname"
        )
    ).scalars().all()

    differences = []
    for sequence in sorted(expected_sequences - actual_sequences):
        differences.append(f"missing sequence {sequence}")
    for sequence in sorted(actual_sequences - expected_sequences):
        differences.append(f"unexpected sequence {sequence}")
    differences.extend(f"unexpected view {name}" for name in sorted(actual_views))
    differences.extend(
        f"unexpected materialized view {name}" for name in sorted(actual_materialized_views)
    )
    differences.extend(f"unexpected foreign table {name}" for name in sorted(actual_foreign_tables))
    differences.extend(f"unexpected trigger {name}" for name in trigger_names)
    return differences


def expected_postgresql_sequences() -> set[str]:
    """Derive default public serial sequence names for integer primary keys."""
    return {
        f"{table.name}_{column.name}_seq"
        for table in Base.metadata.tables.values()
        for column in table.primary_key.columns
        if column.autoincrement in (True, "auto")
        and not column.foreign_keys
        and isinstance(column.type, Integer)
    }


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


def cmd_ocr_reconcile(args) -> int:
    """Report or enqueue eligible local OCR tasks; database claims fence duplicates."""
    try:
        with session_scope() as db:
            task_ids = reconcilable_ocr_task_ids(db)
    except Exception as exc:
        print(f"Не удалось проверить задания OCR: {type(exc).__name__}")
        return 2

    if not args.apply:
        print(f"Eligible local OCR tasks: {len(task_ids)}; no jobs were enqueued")
        return 0
    if not settings.ocr_task_leases_enabled:
        print("Отказ: включите OCR_TASK_LEASES_ENABLED=1 только после применения миграции")
        return 2

    client = None
    enqueued = 0
    try:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        queue = Queue("ocr", connection=client, default_timeout=600)
        from .workers.jobs import recognize

        for task_id in task_ids:
            queue.enqueue(recognize, task_id)
            enqueued += 1
    except Exception as exc:
        print(f"Очередь OCR приняла {enqueued} из {len(task_ids)} заданий; ошибка {type(exc).__name__}")
        return 2
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                print(f"Закрытие соединения Redis не удалось: {type(exc).__name__}")

    print(f"Enqueued {enqueued} eligible local OCR task(s); claims remain database-fenced")
    return 0


def cmd_storage_check(args) -> int:
    """Audit local document permissions without opening document contents."""
    if os.name != "posix":
        print("Проверка POSIX-прав хранилища доступна только на POSIX-системах")
        return 2

    root = settings.upload_dir
    try:
        root_stat = root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode):
            print("Хранилище документов недоступно: каталог отсутствует или является ссылкой")
            return 2
        directory_mode = stat.S_IMODE(root_stat.st_mode)
        expected_owner = os.geteuid()
        directory_owner_matches = root_stat.st_uid == expected_owner
        regular_files = 0
        unsafe_files = 0
        files_with_other_owner = 0
        symlinks = 0
        unexpected_entries = 0
        for entry in root.iterdir():
            entry_stat = entry.lstat()
            if stat.S_ISLNK(entry_stat.st_mode):
                symlinks += 1
            elif stat.S_ISREG(entry_stat.st_mode):
                regular_files += 1
                if stat.S_IMODE(entry_stat.st_mode) & 0o077:
                    unsafe_files += 1
                if entry_stat.st_uid != expected_owner:
                    files_with_other_owner += 1
            else:
                unexpected_entries += 1
    except OSError as exc:
        print(f"Проверка прав хранилища не удалась: {type(exc).__name__}")
        return 2

    print(
        "Document storage permissions: "
        f"directory_mode={directory_mode:04o} directory_owner_matches={directory_owner_matches} "
        f"files={regular_files} "
        f"files_with_group_or_other_access={unsafe_files} symlinks={symlinks} "
        f"files_with_other_owner={files_with_other_owner} unexpected_entries={unexpected_entries}"
    )
    return 0 if (
        directory_mode == 0o700
        and directory_owner_matches
        and not (unsafe_files or files_with_other_owner or symlinks or unexpected_entries)
    ) else 1


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

    p_reconcile = sub.add_parser(
        "ocr-reconcile",
        help="показать или поставить в OCR-очередь задания с безопасной арендой",
    )
    p_reconcile.add_argument(
        "--apply",
        action="store_true",
        help="поставить найденные задания в очередь (требует включённых OCR leases)",
    )
    p_reconcile.set_defaults(func=cmd_ocr_reconcile)

    p_storage = sub.add_parser(
        "storage-check",
        help="проверить POSIX-права локального хранилища без чтения документов",
    )
    p_storage.set_defaults(func=cmd_storage_check)

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
