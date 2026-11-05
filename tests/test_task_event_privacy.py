from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import tasks
from app.services.tasks import (
    _AUDIT_EVENT_ACTOR_REQUIRED,
    _AUDIT_EVENT_PAYLOAD_KEYS,
    _AUDIT_EVENT_REQUIRED_PAYLOAD_KEYS,
    _AUDIT_EVENT_TASK_REQUIRED,
    log,
)


class TaskEventPrivacyTests(unittest.TestCase):
    def test_upload_filename_is_not_copied_to_creation_event(self) -> None:
        sensitive_filename = "Synthetic Person - case A00-12345-2026.pdf"
        document = SimpleNamespace(id=17, filename=sensitive_filename)
        db = MagicMock()

        with patch.object(tasks, "log") as log_event:
            tasks.create_task(
                db,
                document,
                user_id=31,
                request_id="a" * 32,
            )

        self.assertEqual(log_event.call_args.args[2], "created")
        self.assertEqual(log_event.call_args.args[3], "Document received")
        self.assertNotIn(sensitive_filename, repr(log_event.call_args))
        self.assertEqual(log_event.call_args.args[4], 31)
        self.assertEqual(log_event.call_args.kwargs["payload"], {"request_id": "a" * 32})

    def test_dynamic_failure_text_is_replaced_by_fixed_audit_message(self) -> None:
        private_detail = "Synthetic Person 12345.pdf OCR text"
        db = MagicMock()

        log(db, SimpleNamespace(id=9), "failed", private_detail, payload={})

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

    def test_preview_save_audit_requires_actor_snapshot_and_fixed_text(self) -> None:
        db = MagicMock()
        task = SimpleNamespace(id=9)
        for kind in ("decision_save", "preview_save_started"):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    log(db, task, kind, user_id=7, payload={"preview_token": "invalid"})
                log(db, task, kind, "private synthetic document text", user_id=7,
                    payload={"preview_token": "a" * 64})
                event = db.add.call_args.args[0]
                self.assertNotIn("private synthetic", event.message)
                self.assertEqual(event.payload, {"preview_token": "a" * 64})

    def test_audit_contract_requires_evidence_fields(self) -> None:
        self.assertTrue(
            all(required <= _AUDIT_EVENT_PAYLOAD_KEYS[kind]
                for kind, required in _AUDIT_EVENT_REQUIRED_PAYLOAD_KEYS.items())
        )
        for kind, required in _AUDIT_EVENT_REQUIRED_PAYLOAD_KEYS.items():
            with self.subTest(kind=kind):
                db = MagicMock()
                task = SimpleNamespace(id=4) if kind in _AUDIT_EVENT_TASK_REQUIRED else None
                user_id = 5 if kind in _AUDIT_EVENT_ACTOR_REQUIRED else None
                with self.assertRaisesRegex(ValueError, "missing required fields"):
                    log(db, task, kind, user_id=user_id, payload={})
                db.add.assert_not_called()

    def test_employee_audit_events_require_actor_and_task_context(self) -> None:
        self.assertEqual(
            _AUDIT_EVENT_TASK_REQUIRED,
            frozenset(_AUDIT_EVENT_PAYLOAD_KEYS)
            - {"session_started", "session_login_failed", "session_ended"},
        )
        for kind in _AUDIT_EVENT_ACTOR_REQUIRED:
            with self.subTest(kind=kind):
                db = MagicMock()
                task = None if kind in {"session_started", "session_ended"} else SimpleNamespace(id=4)
                with self.assertRaisesRegex(ValueError, "attributable user"):
                    log(db, task, kind, payload={})
                db.add.assert_not_called()

        for kind in _AUDIT_EVENT_TASK_REQUIRED:
            with self.subTest(kind=kind, condition="task"):
                db = MagicMock()
                with self.assertRaisesRegex(ValueError, "task context"):
                    log(db, None, kind, user_id=5, payload={})
                db.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
