from __future__ import annotations

import unittest
from contextlib import nullcontext
from io import StringIO
from unittest.mock import MagicMock, call, patch

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
        connect.assert_called_once_with(run_worker.settings.redis_url)
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
        for revision in (run_worker.EXPECTED_SCHEMA_REVISION, "stale-revision", None):
            with self.subTest(revision=revision):
                with (
                    patch.object(run_worker.engine, "connect", return_value=nullcontext(connection)),
                    patch.object(
                        run_worker.MigrationContext,
                        "configure",
                        return_value=MagicMock(get_current_revision=MagicMock(return_value=revision)),
                    ),
                ):
                    if revision == run_worker.EXPECTED_SCHEMA_REVISION:
                        run_worker.require_current_schema()
                    else:
                        with self.assertRaises(run_worker.WorkerSchemaUnavailable):
                            run_worker.require_current_schema()

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


if __name__ == "__main__":
    unittest.main()
