from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from redis.exceptions import ConnectionError as RedisConnectionError

from app.services.outbox import create_intake_intent
from app.workers import queues
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

    def test_active_ocr_task_lookup_matches_only_recognize_jobs(self) -> None:
        jobs = [
            SimpleNamespace(func_name="app.workers.jobs.recognize", args=(17,)),
            SimpleNamespace(func_name="app.workers.jobs.recognize", args=(99,)),
            SimpleNamespace(func_name="app.workers.jobs.write_to_sro", args=(17,)),
            None,
        ]
        with (
            patch.object(queues.ocr_queue, "get_job_ids", return_value=["queued"]),
            patch.object(queues.StartedJobRegistry, "get_job_ids", return_value=["started"]),
            patch.object(queues.ScheduledJobRegistry, "get_job_ids", return_value=[]),
            patch.object(queues.DeferredJobRegistry, "get_job_ids", return_value=[]),
            patch.object(queues.Job, "fetch_many", return_value=jobs),
        ):
            self.assertEqual(queues.active_ocr_task_ids({17}), {17})

    def test_active_ocr_task_lookup_fails_closed_when_redis_is_unavailable(self) -> None:
        with patch.object(
            queues.ocr_queue,
            "get_job_ids",
            side_effect=RedisConnectionError("redis://synthetic-secret@invalid"),
        ):
            with self.assertRaises(QueueUnavailable) as raised:
                queues.active_ocr_task_ids({17})
        self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_outbox_rejects_external_write_job_kinds_before_database_access(self) -> None:
        db = SimpleNamespace()

        for job_kind in ("sro_write", "kad"):
            with self.subTest(job_kind=job_kind):
                with self.assertRaisesRegex(ValueError, "Unsupported outbox job kind"):
                    create_intake_intent(
                        db,
                        task_id=17,
                        operation_id=23,
                        job_kind=job_kind,
                    )


if __name__ == "__main__":
    unittest.main()
