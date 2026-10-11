from __future__ import annotations

import unittest
from types import SimpleNamespace

from redis.exceptions import ConnectionError as RedisConnectionError

from app.services.outbox import create_intake_intent
from app.workers.queues import QueueUnavailable, _enqueue


class QueueAdmissionTests(unittest.TestCase):
    def test_redis_failure_becomes_safe_queue_unavailable_error(self) -> None:
        queue = SimpleNamespace(
            enqueue=lambda *_args: (_ for _ in ()).throw(
                RedisConnectionError("redis://synthetic-secret@invalid")
            )
        )

        with self.assertRaises(QueueUnavailable) as raised:
            _enqueue(queue, lambda: None, 17)

        self.assertEqual(str(raised.exception), "Background queue admission is unavailable")
        self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_success_returns_the_rq_job_identifier(self) -> None:
        queue = SimpleNamespace(enqueue=lambda *_args: SimpleNamespace(id="synthetic-job-id"))

        self.assertEqual(_enqueue(queue, lambda: None, 17), "synthetic-job-id")

    def test_outbox_rejects_external_write_job_kinds_before_database_access(self) -> None:
        db = SimpleNamespace()

        with self.assertRaisesRegex(ValueError, "Unsupported outbox job kind"):
            create_intake_intent(
                db,
                task_id=17,
                operation_id=23,
                job_kind="sro_write",
            )


if __name__ == "__main__":
    unittest.main()
