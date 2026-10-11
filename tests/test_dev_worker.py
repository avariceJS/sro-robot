from __future__ import annotations

import socket
import time
import unittest
from contextlib import nullcontext
from io import StringIO
from unittest.mock import MagicMock, call, patch

from app.db.schema import (
    EXPECTED_SCHEMA_REVISION,
    SchemaRevisionUnavailable,
    require_current_schema_revision,
)
from scripts.dev import run_worker


class DevelopmentWorkerCommandTests(unittest.TestCase):
    def test_worker_uses_the_private_configured_redis_url(self) -> None:
        events = []
        connection = MagicMock()
        queues = [MagicMock(name="ocr_queue"), MagicMock(name="sro_queue")]
        worker = MagicMock()

        with (
            patch.object(run_worker, "require_current_schema", side_effect=lambda: events.append("schema")),
            patch.object(
                run_worker.Redis,
                "from_url",
                side_effect=lambda *_args, **_kwargs: (events.append("redis"), connection)[1],
            ) as connect,
            patch.object(run_worker, "Queue", side_effect=queues) as queue_factory,
            patch.object(
                run_worker,
                "Worker",
                side_effect=lambda *_args, **_kwargs: (events.append("worker"), worker)[1],
            ) as worker_factory,
        ):
            result = run_worker.main(["ocr", "sro"])

        self.assertEqual(result, 0)
        connect.assert_called_once_with(
            run_worker.settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=5,
        )
        self.assertEqual(
            queue_factory.call_args_list,
            [
                call("ocr", connection=connection),
                call("sro", connection=connection),
            ],
        )
        worker_factory.assert_called_once_with(queues, connection=connection)
        worker.work.assert_called_once_with()
        connection.close.assert_called_once_with()
        self.assertEqual(events, ["schema", "redis", "worker"])

    def test_worker_bounds_and_redacts_redis_startup_failure(self) -> None:
        connection = MagicMock()
        connection.ping.side_effect = run_worker.RedisError(
            "redis://synthetic-user:synthetic-password@private-host.invalid"
        )
        stderr = StringIO()
        with (
            patch.object(run_worker, "require_current_schema"),
            patch.object(run_worker.Redis, "from_url", return_value=connection) as connect,
            patch.object(run_worker, "Queue") as queue_factory,
            patch.object(run_worker, "Worker") as worker_factory,
            patch("sys.stderr", stderr),
        ):
            result = run_worker.main(["ocr"])

        self.assertEqual(result, 2)
        self.assertIn("очередь недоступна", stderr.getvalue())
        self.assertIn("error_type=RedisError", stderr.getvalue())
        self.assertNotIn("synthetic-password", stderr.getvalue())
        self.assertNotIn("private-host", stderr.getvalue())
        connect.assert_called_once_with(
            run_worker.settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=5,
        )
        queue_factory.assert_not_called()
        worker_factory.assert_not_called()
        connection.close.assert_called_once_with()

    def test_worker_fails_fast_against_a_closed_loopback_redis_port(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        stderr = StringIO()
        with (
            patch.object(run_worker, "require_current_schema"),
            patch.object(
                run_worker.settings,
                "redis_url",
                f"redis://synthetic:synthetic-secret@127.0.0.1:{port}/0",
            ),
            patch("sys.stderr", stderr),
        ):
            started = time.monotonic()
            result = run_worker.main(["ocr"])
            elapsed = time.monotonic() - started

        self.assertEqual(result, 2)
        self.assertLess(elapsed, 3)
        self.assertIn("очередь недоступна", stderr.getvalue())
        self.assertNotIn("synthetic-secret", stderr.getvalue())
        self.assertNotIn(f"127.0.0.1:{port}", stderr.getvalue())

    def test_worker_refuses_stale_or_unversioned_schema_before_redis(self) -> None:
        for revision in ("stale-revision", None):
            with self.subTest(revision=revision):
                stderr = StringIO()
                with (
                    patch.object(
                        run_worker,
                        "require_current_schema",
                        side_effect=run_worker.WorkerSchemaUnavailable,
                    ),
                    patch.object(run_worker.Redis, "from_url") as connect,
                    patch.object(run_worker, "Worker") as worker_factory,
                    patch("sys.stderr", stderr),
                ):
                    result = run_worker.main(["ocr"])

                self.assertEqual(result, 2)
                self.assertIn("не соответствует текущей ревизии", stderr.getvalue())
                connect.assert_not_called()
                worker_factory.assert_not_called()

    def test_schema_preflight_accepts_only_exact_current_revision(self) -> None:
        connection = MagicMock()
        for revision in (EXPECTED_SCHEMA_REVISION, "stale-revision", None):
            with self.subTest(revision=revision):
                with (
                    patch.object(run_worker.engine, "connect", return_value=nullcontext(connection)),
                    patch("app.db.schema.MigrationContext.configure") as configure,
                ):
                    configure.return_value.get_current_revision.return_value = revision
                    if revision == EXPECTED_SCHEMA_REVISION:
                        run_worker.require_current_schema()
                    else:
                        with self.assertRaises(run_worker.WorkerSchemaUnavailable):
                            run_worker.require_current_schema()

    def test_shared_schema_gate_accepts_only_exact_current_revision(self) -> None:
        connection = MagicMock()
        for revision in (EXPECTED_SCHEMA_REVISION, "stale-revision", None):
            with self.subTest(revision=revision):
                with patch("app.db.schema.MigrationContext.configure") as configure:
                    configure.return_value.get_current_revision.return_value = revision
                    if revision == EXPECTED_SCHEMA_REVISION:
                        require_current_schema_revision(connection)
                    else:
                        with self.assertRaises(SchemaRevisionUnavailable):
                            require_current_schema_revision(connection)

    def test_schema_preflight_hides_database_connection_details(self) -> None:
        with patch.object(
            run_worker.engine,
            "connect",
            side_effect=RuntimeError("synthetic database credential detail"),
        ):
            with self.assertRaises(run_worker.WorkerSchemaUnavailable) as raised:
                run_worker.require_current_schema()

        self.assertEqual(str(raised.exception), "")

    def test_worker_closes_redis_connection_after_worker_failure(self) -> None:
        connection = MagicMock()
        worker = MagicMock()
        worker.work.side_effect = RuntimeError("synthetic worker failure")

        with (
            patch.object(run_worker, "require_current_schema"),
            patch.object(run_worker.Redis, "from_url", return_value=connection),
            patch.object(run_worker, "Queue", return_value=MagicMock()),
            patch.object(run_worker, "Worker", return_value=worker),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic worker failure"):
                run_worker.main(["ocr"])

        connection.close.assert_called_once_with()

    def test_worker_redacts_redis_runtime_failure_and_exits_nonzero(self) -> None:
        connection = MagicMock()
        worker = MagicMock()
        worker.work.side_effect = run_worker.RedisError(
            "redis://synthetic-user:synthetic-password@private-host.invalid"
        )
        stderr = StringIO()
        with (
            patch.object(run_worker, "require_current_schema"),
            patch.object(run_worker.Redis, "from_url", return_value=connection),
            patch.object(run_worker, "Queue", return_value=MagicMock()),
            patch.object(run_worker, "Worker", return_value=worker),
            patch("sys.stderr", stderr),
        ):
            result = run_worker.main(["ocr"])

        self.assertEqual(result, 2)
        self.assertIn("Worker остановлен: очередь недоступна", stderr.getvalue())
        self.assertIn("error_type=RedisError", stderr.getvalue())
        self.assertNotIn("synthetic-password", stderr.getvalue())
        self.assertNotIn("private-host", stderr.getvalue())
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
