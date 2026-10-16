from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from redis.exceptions import RedisError

from scripts.dev import run_worker


class RunWorkerAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.connection.ping.return_value = True

    def invoke(self, argv, leases_enabled=True, events=None):
        error = io.StringIO()
        events = events if events is not None else []
        def redis_client(*_args, **_kwargs):
            events.append("redis")
            return self.connection
        with (
            patch.object(run_worker, "require_current_schema", side_effect=lambda: events.append("schema")) as schema,
            patch.object(run_worker.engine, "dispose", side_effect=lambda: events.append("dispose")) as dispose,
            patch.object(run_worker.settings, "ocr_task_leases_enabled", leases_enabled),
            patch.object(run_worker.Redis, "from_url", side_effect=redis_client) as redis_from_url,
            redirect_stderr(error),
        ):
            result = run_worker.main(argv)
        return result, error.getvalue(), schema, redis_from_url, dispose, events

    def test_default_single_worker_preserves_worker_path(self):
        with patch("scripts.dev.run_worker.Worker") as worker:
            result, error, schema, redis_from_url, dispose, events = self.invoke(["ocr"])

        self.assertEqual(result, 0)
        self.assertEqual(error, "")
        schema.assert_called_once_with()
        dispose.assert_not_called()
        redis_from_url.assert_called_once_with(
            run_worker.settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=5,
        )
        worker.assert_called_once()
        worker.return_value.work.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_four_workers_uses_rq_pool_for_ocr_only(self):
        events = []
        with patch("scripts.dev.run_worker.WorkerPool") as pool:
            pool.return_value.start.side_effect = lambda: events.append("pool")
            result, error, schema, redis_from_url, dispose, events = self.invoke(
                ["--workers", "4", "ocr"], events=events
            )

        self.assertEqual(result, 0)
        self.assertEqual(error, "")
        self.assertEqual(pool.call_args.kwargs["num_workers"], 4)
        self.assertEqual([queue.name for queue in pool.call_args.args[0]], ["ocr"])
        self.assertIs(pool.call_args.kwargs["connection"], self.connection)
        pool.return_value.start.assert_called_once_with()
        self.assertEqual(events, ["schema", "dispose", "redis", "pool"])
        dispose.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_parallel_admission_rejects_mixed_and_sro_queues_without_redis(self):
        for queues in (["ocr", "sro"], ["sro"]):
            with self.subTest(queues=queues):
                result, error, schema, redis_from_url, _dispose, events = self.invoke(
                    ["--workers", "2", *queues]
                )
                self.assertEqual(result, 2)
                self.assertIn("ocr", error)
                schema.assert_not_called()
                redis_from_url.assert_not_called()

    def test_parallel_admission_requires_enabled_durable_ocr_leases_without_redis(self):
        result, error, schema, redis_from_url, _dispose, events = self.invoke(
            ["--workers", "2", "ocr"], leases_enabled=False
        )
        self.assertEqual(result, 2)
        self.assertIn("leases", error)
        schema.assert_not_called()
        redis_from_url.assert_not_called()

    def test_worker_count_outside_one_through_four_is_rejected_before_redis(self):
        for worker_count in ("0", "5", "-1"):
            with self.subTest(worker_count=worker_count):
                result, error, schema, redis_from_url, _dispose, events = self.invoke(
                    ["--workers", worker_count, "ocr"]
                )
                self.assertEqual(result, 2)
                self.assertIn("between 1 and 4", error)
                schema.assert_not_called()
                redis_from_url.assert_not_called()

    def test_pool_start_failure_still_closes_redis_connection(self):
        with patch("scripts.dev.run_worker.WorkerPool") as pool:
            process = FakeProcess()
            pool.return_value.worker_dict = {
                "owned-worker": SimpleNamespace(name="owned-worker", pid=123, process=process)
            }
            pool.return_value.start.side_effect = RedisError("synthetic")
            result, error, _schema, _redis_from_url, _dispose, _events = self.invoke(
                ["--workers", "2", "ocr"]
            )

        self.assertEqual(result, 2)
        self.assertIn("error_type=RedisError", error)
        pool.return_value.request_stop.assert_not_called()
        self.assertTrue(process.terminated)
        self.assertEqual(process.terminate_calls, 1)
        self.assertGreaterEqual(process.join_calls, 1)
        self.connection.close.assert_called_once_with()

    def test_unexpected_pool_start_failure_is_redacted_after_child_reap(self):
        with patch("scripts.dev.run_worker.WorkerPool") as pool:
            process = FakeProcess()
            pool.return_value.worker_dict = {
                "owned-worker": SimpleNamespace(name="owned-worker", pid=123, process=process)
            }
            pool.return_value.start.side_effect = RuntimeError("synthetic sensitive detail")
            result, error, _schema, _redis_from_url, _dispose, _events = self.invoke(
                ["--workers", "2", "ocr"]
            )

        self.assertEqual(result, 2)
        self.assertIn("WorkerPoolStartError", error)
        self.assertNotIn("synthetic sensitive detail", error)
        self.assertTrue(process.terminated)
        self.assertEqual(process.terminate_calls, 1)
        self.assertFalse(process.is_alive())
        self.connection.close.assert_called_once_with()

    def test_unreaped_pool_child_fails_closed_without_exposing_original_error(self):
        with patch("scripts.dev.run_worker.WorkerPool") as pool:
            process = FakeProcess(stubborn=True, unkillable=True)
            pool.return_value.worker_dict = {
                "owned-worker": SimpleNamespace(name="owned-worker", pid=123, process=process)
            }
            pool.return_value.start.side_effect = RuntimeError("synthetic sensitive detail")
            result, error, _schema, _redis_from_url, _dispose, _events = self.invoke(
                ["--workers", "2", "ocr"]
            )

        self.assertEqual(result, 2)
        self.assertIn("WorkerPoolCleanupError", error)
        self.assertNotIn("synthetic sensitive detail", error)
        pool.return_value.request_stop.assert_not_called()
        self.assertTrue(process.killed)
        self.assertEqual(process.terminate_calls, 2)
        self.connection.close.assert_called_once_with()


class FakeProcess:
    def __init__(self, stubborn=False, unkillable=False):
        self.stubborn = stubborn
        self.unkillable = unkillable
        self.alive = True
        self.terminated = False
        self.terminate_calls = 0
        self.killed = False
        self.join_calls = 0

    def is_alive(self):
        return self.alive

    def join(self, _timeout=None):
        self.join_calls += 1

    def terminate(self):
        self.terminated = True
        self.terminate_calls += 1
        if not self.stubborn:
            self.killed = True
            self.alive = False

    def kill(self):
        self.killed = True
        if not self.unkillable:
            self.alive = False


if __name__ == "__main__":
    unittest.main()
