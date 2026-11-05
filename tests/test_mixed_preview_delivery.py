"""OCR-to-preview durable intent delivery checks without external services."""

from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.workers import jobs


class MixedPreviewDeliveryTests(unittest.TestCase):
    def test_intent_commits_before_queue_delivery(self):
        task = SimpleNamespace(id=34)
        db = MagicMock()
        db.get.return_value = task
        order = []

        @contextmanager
        def session():
            yield db
            order.append("committed")

        def resolve(_db, _task):
            order.append("admitted")
            return 34, 7

        def enqueue(task_id, actor_id):
            order.append("enqueued")
            self.assertEqual((task_id, actor_id), (34, 7))

        with (
            patch.object(jobs, "session_scope", side_effect=session),
            patch("app.services.batch_actions.resolve_prepare_intent", side_effect=resolve),
            patch("app.workers.queues.enqueue_sro_form_preview", side_effect=enqueue),
        ):
            jobs._resolve_and_enqueue_mixed_preview(34)
        self.assertEqual(order, ["admitted", "committed", "enqueued"])

    def test_unadmitted_intent_never_queues_external_form(self):
        db = MagicMock()
        db.get.return_value = SimpleNamespace(id=34)

        @contextmanager
        def session():
            yield db

        with (
            patch.object(jobs, "session_scope", side_effect=session),
            patch("app.services.batch_actions.resolve_prepare_intent", return_value=None),
            patch("app.workers.queues.enqueue_sro_form_preview") as enqueue,
        ):
            jobs._resolve_and_enqueue_mixed_preview(34)
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
