from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch

from scripts.dev import run_worker


class DevelopmentWorkerCommandTests(unittest.TestCase):
    def test_worker_uses_the_private_configured_redis_url(self) -> None:
        connection = MagicMock()
        queues = [MagicMock(name="ocr_queue"), MagicMock(name="sro_queue")]
        worker = MagicMock()

        with (
            patch.object(run_worker.Redis, "from_url", return_value=connection) as connect,
            patch.object(run_worker, "Queue", side_effect=queues) as queue_factory,
            patch.object(run_worker, "Worker", return_value=worker) as worker_factory,
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

    def test_worker_closes_redis_connection_after_worker_failure(self) -> None:
        connection = MagicMock()
        worker = MagicMock()
        worker.work.side_effect = RuntimeError("synthetic worker failure")

        with (
            patch.object(run_worker.Redis, "from_url", return_value=connection),
            patch.object(run_worker, "Queue", return_value=MagicMock()),
            patch.object(run_worker, "Worker", return_value=worker),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic worker failure"):
                run_worker.main(["ocr"])

        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
