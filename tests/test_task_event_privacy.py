from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import tasks


class TaskEventPrivacyTests(unittest.TestCase):
    def test_upload_filename_is_not_copied_to_creation_event(self) -> None:
        sensitive_filename = "Synthetic Person - case A00-12345-2026.pdf"
        document = SimpleNamespace(id=17, filename=sensitive_filename)
        db = MagicMock()

        with patch.object(tasks, "log") as log_event:
            tasks.create_task(db, document, user_id=31)

        self.assertEqual(log_event.call_args.args[2], "created")
        self.assertEqual(log_event.call_args.args[3], "Document received")
        self.assertNotIn(sensitive_filename, repr(log_event.call_args))
        self.assertEqual(log_event.call_args.args[4], 31)


if __name__ == "__main__":
    unittest.main()
