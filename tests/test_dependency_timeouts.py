from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.db import base
from app.workers import queues


class DependencyTimeoutTests(unittest.TestCase):
    def test_postgres_engine_uses_bounded_connection_and_pool_waits(self) -> None:
        with (
            patch.object(base, "create_engine") as create_engine,
            patch.object(base.event, "listen"),
        ):
            base._create_database_engine("postgresql+psycopg://synthetic/db")

        create_engine.assert_called_once_with(
            "postgresql+psycopg://synthetic/db",
            connect_args={"connect_timeout": 5},
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            pool_timeout=5,
            future=True,
        )

    def test_postgres_engine_accepts_explicit_bounded_pool_configuration(self) -> None:
        with (
            patch.object(base, "create_engine") as create_engine,
            patch.object(base.event, "listen"),
        ):
            base._create_database_engine(
                "postgresql+psycopg://synthetic/db",
                pool_size=8,
                max_overflow=2,
                pool_timeout=9,
            )

        self.assertEqual(create_engine.call_args.kwargs["pool_size"], 8)
        self.assertEqual(create_engine.call_args.kwargs["max_overflow"], 2)
        self.assertEqual(create_engine.call_args.kwargs["pool_timeout"], 9)

    def test_postgres_engine_anchors_connections_to_public_schema(self) -> None:
        engine = object()
        with (
            patch.object(base, "create_engine", return_value=engine),
            patch.object(base.event, "listen") as listen,
        ):
            created = base._create_database_engine("postgresql+psycopg://synthetic/db")

        self.assertIs(created, engine)
        listen.assert_called_once_with(engine, "connect", base._set_public_search_path)

    def test_search_path_hook_uses_session_setting_and_closes_cursor(self) -> None:
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value = cursor

        base._set_public_search_path(connection, None)

        cursor.execute.assert_called_once_with("SET SESSION search_path TO public")
        cursor.close.assert_called_once_with()

    def test_redis_queue_client_uses_bounded_socket_waits(self) -> None:
        with patch.object(queues.Redis, "from_url") as from_url:
            queues._create_redis_client()

        from_url.assert_called_once_with(
            queues.settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=5,
        )


if __name__ == "__main__":
    unittest.main()
