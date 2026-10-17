"""Synthetic contracts for authenticated browser-acquired PDF import."""
from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.api.deps import current_user
from app.db.base import get_session
from app.db.models import IntakeOperation, IntakeSource, TaskStatus, User
from app.main import app
from app.services import kad_import, tasks
from app.workers.queues import QueueUnavailable


RUN_ID = "12345678-1234-1234-1234-123456789abc"
CARD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PDF_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
CASE_NUMBER = "А41-12345/2026"
CARD_URL = f"https://kad.arbitr.ru/Card/{CARD_ID}"
PDF_URL = f"https://kad.arbitr.ru/Kad/PdfDocument/{CARD_ID}/{PDF_ID}/act.pdf"
PDF_BYTES = b"%PDF-1.4\nsynthetic local fixture\n%%EOF\n"


def source_payload(**changes) -> dict:
    source = {
        "requestId": RUN_ID,
        "caseNumber": CASE_NUMBER,
        "cardUrl": CARD_URL,
        "candidateInventoryComplete": False,
        "selectedUrl": PDF_URL,
        "candidates": [{"url": PDF_URL, "label": "Synthetic court act"}],
        "selectionReason": "employee_selected_visible_document",
    }
    source.update(changes)
    return source


class KadImportProvenanceTests(unittest.TestCase):
    def test_source_accepts_only_one_selected_candidate_bound_to_card(self) -> None:
        result = kad_import.validate_source(source_payload())

        self.assertEqual(result["provider"], "kad.arbitr.ru")
        self.assertEqual(result["caseNumber"], CASE_NUMBER)
        self.assertEqual(result["cardUrl"], CARD_URL)
        self.assertEqual(result["selectedUrl"], PDF_URL)
        self.assertEqual(result["selectionReason"], "employee_selected_visible_document")
        self.assertEqual(result["candidates"], [{"url": PDF_URL, "label": "Synthetic court act"}])

    def test_source_rejects_multiple_candidates_or_selection_mismatch(self) -> None:
        inventory = kad_import.validate_source(source_payload(candidates=[
            {"url": PDF_URL, "label": "A"},
            {"url": PDF_URL.replace("act.pdf", "other.pdf"), "label": "B"},
        ]))
        self.assertEqual(len(inventory["candidates"]), 2)

        with self.assertRaisesRegex(ValueError, "invalid_selection"):
            kad_import.validate_source(
                source_payload(selectedUrl=PDF_URL.replace("act.pdf", "other.pdf"))
            )

    def test_source_rejects_pdf_outside_the_claimed_card_and_untrusted_hosts(self) -> None:
        wrong_card_pdf = PDF_URL.replace(CARD_ID, "cccccccc-cccc-cccc-cccc-cccccccccccc")
        with self.assertRaisesRegex(ValueError, "invalid_url"):
            kad_import.validate_source(
                source_payload(selectedUrl=wrong_card_pdf, candidates=[{"url": wrong_card_pdf, "label": "Act"}])
            )

        other_host = PDF_URL.replace("kad.arbitr.ru", "example.invalid")
        with self.assertRaisesRegex(ValueError, "invalid_url"):
            kad_import.validate_source(
                source_payload(selectedUrl=other_host, candidates=[{"url": other_host, "label": "Act"}])
            )

    def test_employee_must_explicitly_confirm_even_a_single_visible_candidate(self) -> None:
        source = kad_import.validate_source(source_payload(selectionReason="employee_selected_visible_document"))
        self.assertEqual(source["selectionReason"], "employee_selected_visible_document")

    def test_service_import_rejects_automatic_selection_reasons(self) -> None:
        for reason in ("only_visible_document", "unique_act_date"):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, "invalid_reason"):
                kad_import.validate_source(source_payload(selectionReason=reason))

    def test_employee_can_select_exactly_one_of_multiple_visible_pdfs(self) -> None:
        other_url = PDF_URL.replace("act.pdf", "other.pdf")
        source = kad_import.validate_source(source_payload(
            selectedUrl=other_url,
            selectionReason="employee_selected_visible_document",
            candidates=[
                {"url": PDF_URL, "label": "Synthetic decision", "actDate": "2024-02-29"},
                {"url": other_url, "label": "Synthetic definition", "actDate": "2024-03-01"},
            ],
        ))
        self.assertEqual(source["selectedUrl"], other_url)
        self.assertEqual(source["selectionReason"], "employee_selected_visible_document")
        self.assertEqual(len(source["candidates"]), 2)

    def test_manual_selection_respects_requested_date_and_can_resolve_missing_other_dates(self) -> None:
        other_url = PDF_URL.replace("act.pdf", "other.pdf")
        selected = kad_import.validate_source(source_payload(
            actDate="2024-02-29",
            selectedUrl=PDF_URL,
            selectionReason="employee_selected_visible_document",
            candidates=[
                {"url": PDF_URL, "label": "Synthetic matching act", "actDate": "2024-02-29"},
                {"url": other_url, "label": "Synthetic item with no date"},
            ],
        ))
        self.assertEqual(selected["selectedUrl"], PDF_URL)
        with self.assertRaisesRegex(ValueError, "invalid_selection"):
            kad_import.validate_source({**selected, "selectedUrl": other_url})

    def test_employee_date_selection_preserves_the_full_candidate_inventory(self) -> None:
        requested_date = "2024-02-29"
        other_url = PDF_URL.replace("act.pdf", "other.pdf")
        source = kad_import.validate_source(source_payload(
            actDate=requested_date,
            selectedUrl=PDF_URL,
            selectionReason="employee_selected_visible_document",
            candidates=[
                {"url": PDF_URL, "label": "Synthetic matching act", "actDate": requested_date},
                {"url": other_url, "label": "Synthetic other event", "actDate": "2024-03-01"},
            ],
        ))

        self.assertEqual(source["actDate"], requested_date)
        self.assertEqual(source["selectedUrl"], PDF_URL)
        self.assertEqual(source["selectionReason"], "employee_selected_visible_document")
        self.assertEqual(len(source["candidates"]), 2)
        self.assertEqual([item["actDate"] for item in source["candidates"]], [requested_date, "2024-03-01"])

    def test_check_case_rejects_missing_or_mismatched_parsed_case(self) -> None:
        source = kad_import.validate_source(source_payload())
        matching = SimpleNamespace(case_number=SimpleNamespace(value=CASE_NUMBER))
        missing = SimpleNamespace(case_number=SimpleNamespace(value=""))
        mismatched = SimpleNamespace(case_number=SimpleNamespace(value="А40-98765/2026"))

        self.assertIsNone(kad_import.check_case(source, matching))
        for card in (missing, mismatched, SimpleNamespace()):
            with self.subTest(card=card), self.assertRaises(kad_import.KadDocumentMismatch):
                kad_import.check_case(source, card)

    def test_check_case_rejects_parsed_document_date_mismatch(self) -> None:
        source = kad_import.validate_source(source_payload(actDate="2024-02-29", selectionReason="employee_selected_visible_document",
            candidates=[{"url": PDF_URL, "label": "Synthetic leap-day act", "actDate": "2024-02-29"}]))
        matching = SimpleNamespace(
            case_number=SimpleNamespace(value=CASE_NUMBER),
            doc_date=SimpleNamespace(value="29.02.2024"),
        )
        mismatch = SimpleNamespace(
            case_number=SimpleNamespace(value=CASE_NUMBER),
            doc_date=SimpleNamespace(value="01.03.2024"),
        )

        self.assertIsNone(kad_import.check_case(source, matching))
        with self.assertRaisesRegex(kad_import.KadDocumentMismatch, "act date"):
            kad_import.check_case(source, mismatch)

    def test_source_digest_is_stable_and_binds_selection_metadata(self) -> None:
        source = kad_import.validate_source(source_payload())
        reordered = dict(reversed(list(source.items())))
        altered = {**source, "selectionReason": "different"}

        self.assertEqual(kad_import.source_digest(source), kad_import.source_digest(reordered))
        self.assertNotEqual(kad_import.source_digest(source), kad_import.source_digest(altered))


class KadBrowserImportHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user = User(
            id=91,
            login="synthetic-kad-import-user",
            full_name="Synthetic KAD Import User",
            password_hash="unused-synthetic-hash",
            sro_login="",
            is_active=True,
        )
        self.db = MagicMock()
        self.operation = SimpleNamespace(id=801)
        self.document = SimpleNamespace(id=901, source_metadata=None)
        self.item = SimpleNamespace(id=1001)
        self.task = SimpleNamespace(id=1101, recognized={}, document=self.document)
        app.dependency_overrides[current_user] = lambda: self.user
        app.dependency_overrides[get_session] = lambda: self.db
        self.client = TestClient(app)
        sro_preview_patch = patch("app.workers.queues.enqueue_sro_form_preview")
        task_route_sro_preview_patch = patch("app.api.routes_tasks.enqueue_sro_form_preview")
        self.patches = [
            patch("app.api.routes_kad_import.tasks.begin_intake_operation", return_value=(self.operation, True)),
            patch("app.api.routes_kad_import.tasks.intake_manifest", return_value="a" * 64),
            patch("app.api.routes_kad_import.tasks.store_document", return_value=self.document),
            patch("app.api.routes_kad_import.tasks.create_intake_item", return_value=self.item),
            patch("app.api.routes_kad_import.tasks.create_task", return_value=self.task),
            patch("app.api.routes_kad_import.outbox.create_intake_intent", return_value=1201),
            patch("app.api.routes_kad_import.outbox.dispatch_required"),
            patch("app.api.routes_kad_import.tasks.operation_tasks", return_value=[self.task]),
            patch("app.api.routes_kad_import.tasks.log_intake_replay"),
            sro_preview_patch,
            task_route_sro_preview_patch,
        ]
        self.mocks = [item.start() for item in self.patches]
        (
            self.begin_intake,
            self.intake_manifest,
            self.store_document,
            self.create_item,
            self.create_task,
            self.create_intent,
            self.dispatch,
            self.operation_tasks,
            self.log_replay,
            self.sro_preview_enqueue,
            self.task_route_sro_preview_enqueue,
        ) = self.mocks

    def tearDown(self) -> None:
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        app.dependency_overrides.pop(current_user, None)
        app.dependency_overrides.pop(get_session, None)

    @staticmethod
    def form_data(source: dict | None = None, key: str = "1" * 48, filename: str = "ignored.pdf"):
        return {
            "idempotency_key": key,
            "source": json.dumps(source if source is not None else source_payload(), ensure_ascii=False),
            "file": (filename, PDF_BYTES, "application/pdf"),
        }

    def post(self, *, source: dict | None = None, key: str = "1" * 48, file: bytes = PDF_BYTES):
        return self.client.post(
            "/kad-helper/import",
            data={
                "idempotency_key": key,
                "source": json.dumps(source if source is not None else source_payload(), ensure_ascii=False),
            },
            files={"file": ("ignored.pdf", file, "application/pdf")},
            headers={"Origin": "http://testserver"},
        )

    def test_authenticated_import_creates_occurrence_task_and_ocr_intent_only(self) -> None:
        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"task_url": "/tasks/1101", "queued": True})
        self.begin_intake.assert_called_once()
        args = self.begin_intake.call_args.kwargs
        self.assertEqual(args["user_id"], self.user.id)
        self.assertEqual(args["idempotency_key"], "1" * 48)
        self.assertEqual(args["source_metadata_sha256"], kad_import.source_digest(kad_import.validate_source(source_payload())))
        self.store_document.assert_called_once_with(self.db, "kad-document.pdf", PDF_BYTES, IntakeSource.kad, self.user.id)
        self.create_item.assert_called_once()
        self.create_task.assert_called_once()
        self.assertEqual(self.task.recognized[kad_import.SOURCE_KEY]["sha256"], hashlib.sha256(PDF_BYTES).hexdigest())
        self.create_intent.assert_called_once_with(self.db, task_id=1101, operation_id=801, job_kind="ocr")
        self.dispatch.assert_called_once_with(self.db, 1201)
        self.assertTrue(self.db.commit.called)
        self.sro_preview_enqueue.assert_not_called()
        self.task_route_sro_preview_enqueue.assert_not_called()

    def test_employee_selected_pdf_import_keeps_the_full_case_candidate_inventory(self) -> None:
        other_url = PDF_URL.replace("act.pdf", "other.pdf")
        source = source_payload(
            selectedUrl=other_url,
            selectionReason="employee_selected_visible_document",
            candidates=[
                {"url": PDF_URL, "label": "Synthetic decision", "actDate": "2024-02-29"},
                {"url": other_url, "label": "Synthetic definition", "actDate": "2024-03-01"},
            ],
        )
        response = self.post(source=source)

        self.assertEqual(response.status_code, 200)
        retained = self.task.recognized[kad_import.SOURCE_KEY]
        self.assertEqual(retained["selectedUrl"], other_url)
        self.assertEqual(retained["selectionReason"], "employee_selected_visible_document")
        self.assertEqual(retained["candidates"], source["candidates"])
        self.assertEqual(retained["sha256"], hashlib.sha256(PDF_BYTES).hexdigest())
        self.assertEqual(self.begin_intake.call_args.kwargs["source_metadata_sha256"], kad_import.source_digest(kad_import.validate_source(source)))

    def test_duplicate_replay_returns_same_task_without_storing_new_blob(self) -> None:
        self.begin_intake.return_value = (self.operation, False)

        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"task_url": "/tasks/1101", "queued": True})
        self.operation_tasks.assert_called_once_with(self.db, self.operation.id)
        self.log_replay.assert_called_once_with(self.db, [self.task], self.user.id, unittest.mock.ANY)
        self.store_document.assert_not_called()
        self.create_item.assert_not_called()
        self.create_task.assert_not_called()
        self.create_intent.assert_called_once_with(self.db, task_id=1101, operation_id=801, job_kind="ocr")

    def test_idempotency_conflict_returns_409_before_document_or_queue_work(self) -> None:
        self.begin_intake.side_effect = tasks.IntakeOperationConflict("conflict")

        response = self.post()

        self.assertEqual(response.status_code, 409)
        self.store_document.assert_not_called()
        self.create_task.assert_not_called()
        self.create_intent.assert_not_called()
        self.dispatch.assert_not_called()

    def test_invalid_pdf_or_provenance_is_rejected_before_intake(self) -> None:
        for source, content, expected in (
            (source_payload(), b"not a PDF", 422),
            (source_payload(candidates=[]), PDF_BYTES, 422),
            (source_payload(selectionReason="only_visible_document"), PDF_BYTES, 422),
        ):
            with self.subTest(expected=expected, content=content[:8]):
                self.begin_intake.reset_mock()
                self.store_document.reset_mock()
                response = self.post(source=source, file=content)
                self.assertEqual(response.status_code, expected)
                self.begin_intake.assert_not_called()
                self.store_document.assert_not_called()

    def test_oversize_pdf_is_rejected_before_intake(self) -> None:
        oversized = b"%PDF-1.4\n" + b"x" * 40 + b"\n%%EOF"
        with patch("app.api.routes_kad_import.settings.upload_max_file_bytes", len(PDF_BYTES)):
            response = self.post(file=oversized)

        self.assertEqual(response.status_code, 413)
        self.begin_intake.assert_not_called()
        self.store_document.assert_not_called()

    def test_cross_origin_import_is_rejected_before_auth_or_intake(self) -> None:
        response = self.client.post(
            "/kad-helper/import",
            data={
                "idempotency_key": "1" * 48,
                "source": json.dumps(source_payload(), ensure_ascii=False),
            },
            files={"file": ("ignored.pdf", PDF_BYTES, "application/pdf")},
            headers={"Origin": "https://attacker.invalid"},
        )

        self.assertEqual(response.status_code, 403)
        self.begin_intake.assert_not_called()
        self.store_document.assert_not_called()

    def test_queue_unavailable_returns_durable_receipt_without_claiming_queued(self) -> None:
        self.dispatch.side_effect = QueueUnavailable("SyntheticQueueUnavailable")

        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"task_url": "/tasks/1101", "queued": False})
        self.db.commit.assert_called_once()
        self.dispatch.assert_called_once_with(self.db, 1201)
        self.sro_preview_enqueue.assert_not_called()

    def test_receipt_returns_task_for_owned_import_key(self) -> None:
        key = "2" * 48
        self.db.scalar.return_value = self.operation
        self.task.recognized = {kad_import.SOURCE_KEY: {"provider": "kad.arbitr.ru"}}

        response = self.client.get(f"/kad-helper/receipt/{key}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"task_url": "/tasks/1101"})
        self.db.scalar.assert_called_once()
        statement = self.db.scalar.call_args.args[0]
        compiled = statement.compile()
        self.assertIn("intake_operation.user_id", str(statement))
        self.assertIn("intake_operation.idempotency_key", str(statement))
        self.assertIn(self.user.id, compiled.params.values())
        self.assertIn(key, compiled.params.values())
        self.operation_tasks.assert_called_once_with(self.db, self.operation.id)
        self.dispatch.assert_not_called()
        self.create_intent.assert_not_called()
        self.db.commit.assert_not_called()
        self.store_document.assert_not_called()

    def test_receipt_rejects_invalid_key_before_database_lookup(self) -> None:
        response = self.client.get("/kad-helper/receipt/not-a-hex-key")

        self.assertEqual(response.status_code, 422)
        self.db.scalar.assert_not_called()
        self.operation_tasks.assert_not_called()
        self.dispatch.assert_not_called()
        self.db.commit.assert_not_called()
        self.store_document.assert_not_called()

    def test_receipt_returns_not_found_for_unknown_owned_key(self) -> None:
        self.db.scalar.return_value = None

        response = self.client.get(f"/kad-helper/receipt/{'3' * 48}")

        self.assertEqual(response.status_code, 404)
        self.db.scalar.assert_called_once()
        self.operation_tasks.assert_not_called()
        self.dispatch.assert_not_called()
        self.db.commit.assert_not_called()
        self.store_document.assert_not_called()

    def test_receipt_rejects_operation_without_kad_task_provenance(self) -> None:
        self.db.scalar.return_value = self.operation
        self.task.recognized = {"some_other_source": {"provider": "synthetic"}}

        response = self.client.get(f"/kad-helper/receipt/{'4' * 48}")

        self.assertEqual(response.status_code, 409)
        self.operation_tasks.assert_called_once_with(self.db, self.operation.id)
        self.dispatch.assert_not_called()
        self.create_intent.assert_not_called()
        self.db.commit.assert_not_called()
        self.store_document.assert_not_called()


class KadTaskProvenanceTests(unittest.TestCase):
    def test_recognition_preserves_import_provenance_on_task_not_shared_document(self) -> None:
        from sro_robot.core.models import DebtorCard, Field

        document = SimpleNamespace(source_metadata=None)
        first_source = {**kad_import.validate_source(source_payload()), "sha256": hashlib.sha256(PDF_BYTES).hexdigest()}
        second_source = {**kad_import.validate_source(source_payload(requestId="87654321-4321-4321-4321-cba987654321")), "sha256": hashlib.sha256(PDF_BYTES).hexdigest()}
        first = SimpleNamespace(id=201, status=TaskStatus.recognizing, recognized={kad_import.SOURCE_KEY: first_source}, document=document)
        second = SimpleNamespace(id=202, status=TaskStatus.recognizing, recognized={kad_import.SOURCE_KEY: second_source}, document=document)
        db = MagicMock()
        card = DebtorCard(case_number=Field(value=CASE_NUMBER, confidence=1.0, source="synthetic"))

        with patch.object(tasks, "lock_task_for_update"):
            self.assertTrue(tasks.save_recognized(db, first, card))
            self.assertTrue(tasks.save_recognized(db, second, card))

        self.assertEqual(first.recognized[kad_import.SOURCE_KEY], first_source)
        self.assertEqual(second.recognized[kad_import.SOURCE_KEY], second_source)
        self.assertIsNone(document.source_metadata)
        self.assertEqual(first.status, TaskStatus.needs_review)
        self.assertEqual(second.status, TaskStatus.needs_review)


if __name__ == "__main__":
    unittest.main()
