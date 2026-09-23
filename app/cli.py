"""Служебные команды платформы.

    .venv/bin/python -m app.cli init-db
    .venv/bin/python -m app.cli create-user ivan@example.ru
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .db.base import Base, engine, session_scope
from .db import models  # noqa: F401  — регистрирует таблицы в метаданных
from .services import auth


def cmd_init_db(args) -> int:
    if args.reset:
        Base.metadata.drop_all(engine)
        print("Старые таблицы удалены")
    Base.metadata.create_all(engine)
    print("Таблицы созданы")
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

    p_init = sub.add_parser("init-db", help="создать таблицы")
    p_init.add_argument("--reset", action="store_true", help="удалить таблицы перед созданием")
    p_init.set_defaults(func=cmd_init_db)

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
