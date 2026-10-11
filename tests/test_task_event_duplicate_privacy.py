from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.models import TaskStatus
from app.services.tasks import log, mark_duplicate


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

    def test_dynamic_failure_text_is_replaced_by_fixed_audit_message(self) -> None:
        private_detail = "Synthetic Person 12345.pdf OCR text"
        db = MagicMock()

        log(db, None, "failed", private_detail, payload={})

        event = db.add.call_args.args[0]
        self.assertEqual(event.message, "Обработка завершилась ошибкой; требуется проверка")
        self.assertNotIn(private_detail, repr(event))

    def test_audit_contract_rejects_unreviewed_kinds_and_payload_fields(self) -> None:
        for kind, payload in (
            ("new_unreviewed_event", {}),
            ("fields_saved", {"submitted_values": {"name": "Synthetic Person"}}),
            ("session_started", {"request_id": "not-a-server-request-id"}),
            ("ocr_claimed", {"attempt": True, "generation": 1}),
            ("operator_action_not_applied", {"action": ["approve"]}),
        ):
            with self.subTest(kind=kind, payload=payload):
                db = MagicMock()
                with self.assertRaises(ValueError):
                    log(db, None, kind, "untrusted message", payload=payload)
                db.add.assert_not_called()

        with self.assertRaises(ValueError):
            log(MagicMock(), None, "recognized", "ignored", payload=[])


if __name__ == "__main__":
    unittest.main()
