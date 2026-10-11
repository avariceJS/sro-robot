from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.models import TaskStatus
from app.services.tasks import mark_duplicate


class DuplicateEventPrivacyTests(unittest.TestCase):
    def test_duplicate_event_records_count_without_document_names(self) -> None:
        sensitive_name = "Synthetic Debtor - A47-12345-2026.pdf"
        task = SimpleNamespace(id=17, status=TaskStatus.recognizing)
        db = MagicMock()

        mark_duplicate(db, task, [sensitive_name, "another synthetic.pdf"])

        event = db.add.call_args.args[0]
        self.assertEqual(task.status, TaskStatus.duplicate)
        self.assertEqual(event.kind, "duplicate")
        self.assertEqual(event.message, "Duplicate document detected")
        self.assertEqual(event.payload, {"count": 2})
        self.assertNotIn(sensitive_name, repr(event))
        db.flush.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
