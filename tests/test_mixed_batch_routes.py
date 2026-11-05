"""Synthetic API admission checks for exact mixed-batch selections."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import hashlib
import unittest

from fastapi.testclient import TestClient

from app.api import routes_mixed_batches
from app.api import routes_assignments
from app.api import routes_tasks
from app.api.deps import current_user
from app.db.base import get_session
from app.db.models import Document, MixedBatchPrepareIntent, Task, TaskStatus
from app.main import app
from app.services import mixed_intake
from app.services import batch_actions
from sro_robot.core.extract import parse
from sro_robot.core.mixed_classification import classify_card
from sro_robot.core.models import card_to_fields


BATCH_ID = "a" * 32


def _state() -> dict:
    rows = [
        {"file_index": index, "task_id": 100 + index, "row_token": str(index) * 64,
         "registry": "persons", "status": "needs_review"}
        for index in range(3)
    ]
    return {"batch_id": BATCH_ID, "snapshot_token": "f" * 64,
            "groups": {"persons": rows}, "total": len(rows)}


class MixedBatchRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = MagicMock()
        self.user = SimpleNamespace(id=7)
        app.dependency_overrides[get_session] = lambda: self.db
        app.dependency_overrides[current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(current_user, None)

    def test_prepare_selects_only_named_occurrence_and_rejects_stale_manifest(self) -> None:
        task = SimpleNamespace(id=101)
        added = []
        self.db.add.side_effect = added.append
        self.db.get.side_effect = lambda model, key: (
            None if model is MixedBatchPrepareIntent else task if model is Task else None
        )
        def resolve(_db, _task):
            added[-1].status = "admitted"
            return task.id, 7
        with (patch.object(routes_mixed_batches, "_require_batch", return_value=SimpleNamespace(id=BATCH_ID)),
              patch.object(routes_mixed_batches, "mixed_batch_state", return_value=_state()),
              patch.object(routes_mixed_batches.batch_actions, "resolve_prepare_intent", side_effect=resolve),
              patch.object(routes_mixed_batches, "enqueue_sro_form_preview") as enqueue):
            response = self.client.post(
                f"/upload-batches/{BATCH_ID}/mixed/prepare",
                json={"file_indices": [1], "snapshot_token": "f" * 64,
                      "request_key": "b" * 48},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["results"], [
                {"file_index": 1, "status": "admitted", "task_id": 101}
            ])
            self.assertEqual([(item.batch_id, item.file_index, item.task_id) for item in added],
                             [(BATCH_ID, 1, 101)])
            enqueue.assert_called_once_with(101, 7)
            stale = self.client.post(
                f"/upload-batches/{BATCH_ID}/mixed/prepare",
                json={"file_indices": [1], "snapshot_token": "e" * 64,
                      "request_key": "c" * 48},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(stale.status_code, 409)
            enqueue.assert_called_once()

    def test_durable_v2_preview_is_visible_for_individual_save(self) -> None:
        task = Task(
            id=100, document_id=1, scenario="person_card", profile="court_ruling",
            status=TaskStatus.awaiting_preview, recognized={}, values={},
            screenshots=["/synthetic/preview.png"], browser_decision="",
            preview_mode="durable_closed", preview_purpose="new_record_review",
            preview_schema_version=2, preview_values_sha256=routes_mixed_batches.tasks.values_digest({}),
            preview_screenshot_sha256=["f" * 64], preview_destination={},
            local_finalization_state="not_applicable", owned_add_state="bound", dry_run=True,
        )
        self.db.get.side_effect = lambda model, key: (
            task if model is Task else None
        )
        grouped = {"batch_id": BATCH_ID, "groups": {"persons": [{
            "file_index": 0, "filename": "synthetic.pdf", "task_id": 100,
            "status": "awaiting_preview", "subject_kind": "person",
            "reason": "explicit_person_debtor",
        }]}, "total": 1, "counts": {"persons": 1}}
        with (patch.object(routes_mixed_batches, "_require_batch", return_value=SimpleNamespace(id=BATCH_ID)),
              patch.object(routes_mixed_batches.mixed_intake, "batch_registry_groups", return_value=grouped),
              patch.object(routes_mixed_batches, "_batch_snapshot", return_value="f" * 64),
              patch.object(routes_mixed_batches.tasks, "_owned_binding_matches", return_value=True),
              patch.object(routes_mixed_batches.tasks, "_owned_principal_matches", return_value=True),
              patch.object(routes_mixed_batches.tasks, "_save_readiness_errors", return_value=[]),
              patch.object(routes_mixed_batches.fields, "review_fields", return_value=[])):
            state = routes_mixed_batches.mixed_batch_state(self.db, BATCH_ID, 7)
        row = state["groups"]["persons"][0]
        self.assertTrue(row["can_save"])
        self.assertEqual(row["technical_card_state"], "preview_ready")
        self.assertTrue(row["preview_uses_owned_card"])
        self.assertEqual(row["preview_token"], routes_mixed_batches.tasks.preview_token(task))
        self.assertTrue(row["role_review_required"])
        with (patch.object(routes_mixed_batches, "_require_batch", return_value=SimpleNamespace(id=BATCH_ID)),
              patch.object(routes_mixed_batches.mixed_intake, "batch_registry_groups", return_value=grouped),
              patch.object(routes_mixed_batches, "_batch_snapshot", return_value="f" * 64),
              patch.object(routes_mixed_batches.tasks, "_owned_binding_matches", return_value=True),
              patch.object(routes_mixed_batches.tasks, "_owned_principal_matches", return_value=False),
              patch.object(routes_mixed_batches.tasks, "_save_readiness_errors", return_value=[]),
              patch.object(routes_mixed_batches.fields, "review_fields", return_value=[])):
            other_principal = routes_mixed_batches.mixed_batch_state(self.db, BATCH_ID, 8)
        self.assertFalse(other_principal["groups"]["persons"][0]["can_save"])
        task.preview_refresh_state = "requested"
        with (patch.object(routes_mixed_batches, "_require_batch", return_value=SimpleNamespace(id=BATCH_ID)),
              patch.object(routes_mixed_batches.mixed_intake, "batch_registry_groups", return_value=grouped),
              patch.object(routes_mixed_batches, "_batch_snapshot", return_value="f" * 64),
              patch.object(routes_mixed_batches.tasks, "_owned_binding_matches", return_value=True),
              patch.object(routes_mixed_batches.tasks, "_owned_principal_matches", return_value=True),
              patch.object(routes_mixed_batches.tasks, "_save_readiness_errors", return_value=[]),
              patch.object(routes_mixed_batches.fields, "review_fields", return_value=[])):
            refreshing = routes_mixed_batches.mixed_batch_state(self.db, BATCH_ID, 7)
        self.assertFalse(refreshing["groups"]["persons"][0]["can_save"])
        self.assertEqual(refreshing["groups"]["persons"][0]["technical_card_state"], "preparing")

    def test_partial_preview_is_shown_but_save_requires_refreshed_complete_values(self) -> None:
        task = Task(
            id=100, document_id=1, scenario="person_card", profile="court_ruling",
            status=TaskStatus.awaiting_preview, recognized={}, values={},
            screenshots=["/synthetic/preview.png"], browser_decision="",
            preview_mode="durable_closed", preview_purpose="new_record_review",
            preview_schema_version=2, preview_values_sha256=routes_mixed_batches.tasks.values_digest({}),
            preview_screenshot_sha256=["f" * 64], preview_destination={},
            local_finalization_state="not_applicable", owned_add_state="bound", dry_run=True,
        )
        self.db.get.side_effect = lambda model, key: task if model is Task else None
        grouped = {"batch_id": BATCH_ID, "groups": {"persons": [{
            "file_index": 0, "filename": "synthetic.pdf", "task_id": 100,
            "status": "awaiting_preview", "subject_kind": "person",
            "reason": "review_person_candidate",
        }]}, "total": 1, "counts": {"persons": 1}}
        with (patch.object(routes_mixed_batches, "_require_batch", return_value=SimpleNamespace(id=BATCH_ID)),
              patch.object(routes_mixed_batches.mixed_intake, "batch_registry_groups", return_value=grouped),
              patch.object(routes_mixed_batches, "_batch_snapshot", return_value="f" * 64),
              patch.object(routes_mixed_batches.tasks, "_owned_binding_matches", return_value=True),
              patch.object(routes_mixed_batches.tasks, "_owned_principal_matches", return_value=True),
              patch.object(routes_mixed_batches.fields, "review_fields", return_value=[])):
            row = routes_mixed_batches.mixed_batch_state(self.db, BATCH_ID, 7)["groups"]["persons"][0]
        self.assertEqual(row["technical_card_state"], "preview_ready")
        self.assertFalse(row["can_save"])
        self.assertIn("Поля карточки неполны", row["save_block_reason"])

    def test_task_refresh_commits_exact_values_before_same_guid_queue(self) -> None:
        task = SimpleNamespace(id=100, scenario="person_card")
        self.db.get.return_value = task
        order = []
        self.db.commit.side_effect = lambda: order.append("commit")
        with (patch.object(routes_tasks.tasks, "request_owned_preview_refresh", return_value=True) as request_refresh,
              patch.object(routes_tasks, "enqueue_sro_preview_refresh",
                           side_effect=lambda *_args: order.append("enqueue")) as enqueue):
            response = self.client.post(
                "/tasks/100/refresh-owned-preview",
                data={"preview_token": "a" * 64, "refresh_key": "b" * 48,
                      "last_name": "Синтетический", "first_name": "Тест"},
                headers={"Origin": "http://testserver"}, follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(order, ["commit", "enqueue"])
        self.assertEqual(request_refresh.call_args.args[1:4], (task, 7, "a" * 64))
        self.assertEqual(request_refresh.call_args.args[4]["last_name"], "Синтетический")
        self.assertEqual(enqueue.call_args.args[0:2], (100, 7))

    def test_task_refresh_retry_only_redelivers_unclaimed_same_request(self) -> None:
        task = SimpleNamespace(
            id=100, preview_mode="durable_closed", status=TaskStatus.awaiting_preview,
            preview_refresh_state="requested", preview_refresh_actor_id=7,
            preview_refresh_request_id="a" * 32,
            browser_decision="", preview_save_enabled=False,
        )
        self.db.get.return_value = task
        with patch.object(routes_tasks, "enqueue_sro_preview_refresh") as enqueue:
            response = self.client.post(
                "/tasks/100/retry-owned-preview-refresh",
                headers={"Origin": "http://testserver"}, follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303, response.text)
            enqueue.assert_called_once_with(100, 7, "a" * 32)
            task.preview_refresh_state = "started"
            blocked = self.client.post(
                "/tasks/100/retry-owned-preview-refresh",
                headers={"Origin": "http://testserver"}, follow_redirects=False,
            )
            self.assertEqual(blocked.status_code, 409)
            enqueue.assert_called_once()

    def test_legacy_decision_route_cannot_save_durable_card(self) -> None:
        self.db.get.return_value = SimpleNamespace(id=100, preview_mode="durable_closed")
        with patch.object(routes_tasks.tasks, "decide_preview") as legacy_decide:
            response = self.client.post(
                "/tasks/100/decision", data={"decision": "save", "preview_token": "a" * 64},
                headers={"Origin": "http://testserver"}, follow_redirects=False,
            )
        self.assertEqual(response.status_code, 409)
        legacy_decide.assert_not_called()

    def test_one_coherent_review_person_is_prepare_eligible_but_role_unconfirmed(self) -> None:
        task = Task(
            id=100, document_id=1, scenario="person_card", profile="court_ruling",
            status=TaskStatus.needs_review, recognized={}, values={},
            screenshots=[], browser_decision="", preview_mode="live_session",
            preview_purpose="legacy", owned_add_state="not_started",
            local_finalization_state="not_applicable",
        )
        self.db.get.side_effect = lambda model, key: task if model is Task else None
        grouped = {"batch_id": BATCH_ID, "groups": {"persons": [{
            "file_index": 0, "filename": "synthetic.jpg", "task_id": 100,
            "status": "needs_review", "subject_kind": "person",
            "reason": "review_person_candidate",
        }]}, "total": 1, "counts": {"persons": 1}}
        with (patch.object(routes_mixed_batches, "_require_batch", return_value=SimpleNamespace(id=BATCH_ID)),
              patch.object(routes_mixed_batches.mixed_intake, "batch_registry_groups", return_value=grouped),
              patch.object(routes_mixed_batches, "_batch_snapshot", return_value="f" * 64),
              patch.object(routes_mixed_batches.tasks, "mixed_preview_eligibility",
                           return_value={"eligible": True, "reason": "review_person_candidate"}),
              patch.object(routes_mixed_batches.fields, "review_fields", return_value=[])):
            state = routes_mixed_batches.mixed_batch_state(self.db, BATCH_ID, 7)
        row = state["groups"]["persons"][0]
        self.assertTrue(row["can_prepare"])
        self.assertTrue(row["role_review_required"])
        self.assertFalse(row["can_save"])
        self.assertEqual(row["technical_card_state"], "pending")

    def test_applicant_row_shows_both_missing_assignment_inputs_without_prepare(self) -> None:
        task = Task(
            id=100, document_id=1, scenario="person_card", profile="court_ruling",
            status=TaskStatus.needs_review, recognized={}, values={},
            screenshots=[], browser_decision="", preview_mode="live_session",
            preview_purpose="legacy", owned_add_state="not_started",
            local_finalization_state="not_applicable",
        )
        assignment = SimpleNamespace(
            id=19, status="awaiting_information", source_role="applicant",
            debtor_card_id=None, values={"practitioner": ""},
        )
        self.db.get.side_effect = lambda model, key: task if model is Task else None
        self.db.scalar.return_value = assignment
        grouped = {"batch_id": BATCH_ID, "groups": {"assignments": [{
            "file_index": 0, "filename": "synthetic.jpg", "task_id": 100,
            "status": "needs_review", "subject_kind": "person",
            "reason": "explicit_applicant",
        }]}, "total": 1, "counts": {"assignments": 1}}
        with (patch.object(routes_mixed_batches, "_require_batch", return_value=SimpleNamespace(id=BATCH_ID)),
              patch.object(routes_mixed_batches.mixed_intake, "batch_registry_groups", return_value=grouped),
              patch.object(routes_mixed_batches, "_batch_snapshot", return_value="f" * 64),
              patch.object(routes_mixed_batches.fields, "review_fields", return_value=[])):
            row = routes_mixed_batches.mixed_batch_state(self.db, BATCH_ID, 7)["groups"]["assignments"][0]
        self.assertEqual(row["assignment_url"], "/assignments/19")
        self.assertEqual(row["assignment_status"], "awaiting_information")
        self.assertTrue(row["assignment_needs_debtor"])
        self.assertTrue(row["assignment_needs_practitioner"])
        self.assertFalse(row["can_prepare"])
        self.assertFalse(row["prepare_retry"])

    def test_unknown_add_cannot_be_readmitted_or_requeued(self) -> None:
        task = SimpleNamespace(id=100, status=TaskStatus.needs_review,
                               owned_add_state="unknown")
        intent = SimpleNamespace(batch_id=BATCH_ID, file_index=0,
                                 task_id=100, actor_id=7, status="pending_ocr")
        self.db.scalar.return_value = intent
        grouped = {"groups": {"persons": [{"file_index": 0, "task_id": 100}]}}
        with (patch.object(batch_actions.mixed_intake, "batch_registry_groups", return_value=grouped),
              patch.object(batch_actions.tasks, "lock_task_for_update"),
              patch.object(batch_actions.tasks, "admit_mixed_preview") as admit):
            self.assertIsNone(batch_actions.resolve_prepare_intent(self.db, task))
        self.assertEqual(intent.status, "withheld")
        admit.assert_not_called()

    def test_unknown_add_is_held_in_read_model(self) -> None:
        task = Task(
            id=100, document_id=1, scenario="person_card", profile="court_ruling",
            status=TaskStatus.needs_review, recognized={}, values={},
            screenshots=[], browser_decision="", preview_mode="durable_closed",
            preview_purpose="new_record_review", owned_add_state="unknown",
            local_finalization_state="not_applicable",
        )
        self.db.get.side_effect = lambda model, key: task if model is Task else None
        grouped = {"batch_id": BATCH_ID, "groups": {"persons": [{
            "file_index": 0, "filename": "synthetic.jpg", "task_id": 100,
            "status": "needs_review", "subject_kind": "person",
            "reason": "explicit_person_debtor",
        }]}, "total": 1, "counts": {"persons": 1}}
        with (patch.object(routes_mixed_batches, "_require_batch", return_value=SimpleNamespace(id=BATCH_ID)),
              patch.object(routes_mixed_batches.mixed_intake, "batch_registry_groups", return_value=grouped),
              patch.object(routes_mixed_batches, "_batch_snapshot", return_value="f" * 64),
              patch.object(routes_mixed_batches.fields, "review_fields", return_value=[]),
              patch.object(routes_mixed_batches.tasks, "mixed_preview_eligibility") as eligibility):
            row = routes_mixed_batches.mixed_batch_state(self.db, BATCH_ID, 7)["groups"]["persons"][0]
        self.assertEqual(row["technical_card_state"], "add_unknown")
        self.assertFalse(row["can_prepare"])
        self.assertFalse(row["prepare_retry"])
        eligibility.assert_not_called()

    def test_save_requires_selected_preview_token_and_isolates_items(self) -> None:
        first = SimpleNamespace(id=100, preview_mode="durable_closed",
                                status=TaskStatus.awaiting_preview, browser_decision="")
        second = SimpleNamespace(id=101, preview_mode="durable_closed",
                                 status=TaskStatus.awaiting_preview, browser_decision="")
        self.db.get.side_effect = lambda model, key: {100: first, 101: second}.get(key)
        with (patch.object(routes_mixed_batches, "mixed_batch_state", return_value=_state()),
              patch.object(routes_mixed_batches.tasks, "confirm_durable_preview",
                           side_effect=lambda _db, task, *_args: task.id == 100),
              patch.object(routes_mixed_batches.tasks, "request_durable_sro_save", return_value=True),
              patch.object(routes_mixed_batches, "enqueue_sro_durable_save") as enqueue):
            response = self.client.post(
                f"/upload-batches/{BATCH_ID}/mixed/save",
                json={"items": [{"file_index": 0, "preview_token": "1" * 64},
                                {"file_index": 1, "preview_token": "2" * 64}],
                      "snapshot_token": "f" * 64, "request_key": "d" * 48},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([result["status"] for result in response.json()["results"]],
                             ["save_requested", "stale_or_unavailable"])
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.args[:2], (100, 7))

    def test_lost_save_response_returns_current_state_without_reenqueue(self) -> None:
        token = "1" * 64
        key = "d" * 48
        request_id = hashlib.sha256(
            f"sro-robot/mixed-save/v1:{BATCH_ID}:0:7:{key}".encode()
        ).hexdigest()[:32]
        decision = SimpleNamespace(user_id=7, payload={
            "preview_token": token, "request_id": request_id,
        })
        self.db.scalar.return_value = decision
        with (patch.object(routes_mixed_batches, "mixed_batch_state", return_value=_state()),
              patch.object(routes_mixed_batches.tasks, "confirm_durable_preview") as confirm,
              patch.object(routes_mixed_batches.tasks, "request_durable_sro_save") as request_save,
              patch.object(routes_mixed_batches, "enqueue_sro_durable_save") as enqueue):
            for state in ("save_started", "saved", "save_unknown"):
                with self.subTest(state=state):
                    task = SimpleNamespace(id=100, preview_mode="durable_closed",
                                           status=TaskStatus.awaiting_preview,
                                           browser_decision=state)
                    self.db.get.side_effect = lambda model, index: task
                    response = self.client.post(
                        f"/upload-batches/{BATCH_ID}/mixed/save",
                        json={"items": [{"file_index": 0, "preview_token": token}],
                              "snapshot_token": "f" * 64, "request_key": key},
                        headers={"Origin": "http://testserver"},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["results"][0]["status"], state)
            confirm.assert_not_called()
            request_save.assert_not_called()
            enqueue.assert_not_called()
            wrong_key = self.client.post(
                f"/upload-batches/{BATCH_ID}/mixed/save",
                json={"items": [{"file_index": 0, "preview_token": token}],
                      "snapshot_token": "f" * 64, "request_key": "e" * 48},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(wrong_key.json()["results"][0]["status"], "stale_or_unavailable")
            enqueue.assert_not_called()

    def test_local_reconcile_uses_verified_receipt_without_queue(self) -> None:
        task = SimpleNamespace(id=100, preview_purpose="new_record_review",
                               local_finalization_state="failed")
        self.db.get.return_value = task
        with (patch.object(routes_mixed_batches, "mixed_batch_state", return_value=_state()),
              patch.object(routes_mixed_batches.tasks, "saved_sro_record_url",
                           return_value="https://synthetic.invalid/register/person_card.aspx?id=1"),
              patch.object(routes_mixed_batches.tasks, "reconcile_saved_local_finalization",
                           return_value=True) as reconcile,
              patch.object(routes_mixed_batches, "enqueue_sro_durable_save") as enqueue):
            response = self.client.post(
                f"/upload-batches/{BATCH_ID}/mixed/reconcile-local",
                json={"file_index": 0, "snapshot_token": "f" * 64,
                      "request_key": "d" * 48},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "succeeded")
            reconcile.assert_called_once_with(self.db, task, 7)
            enqueue.assert_not_called()
        with (patch.object(routes_mixed_batches, "mixed_batch_state", return_value=_state()),
              patch.object(routes_mixed_batches.tasks, "saved_sro_record_url", return_value=None),
              patch.object(routes_mixed_batches.tasks, "reconcile_saved_local_finalization") as reconcile):
            blocked = self.client.post(
                f"/upload-batches/{BATCH_ID}/mixed/reconcile-local",
                json={"file_index": 0, "snapshot_token": "f" * 64,
                      "request_key": "d" * 48},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(blocked.status_code, 409)
            reconcile.assert_not_called()

    def test_unclaimed_save_delivery_replays_without_new_entitlement(self) -> None:
        token = "1" * 64
        task = SimpleNamespace(id=100, preview_mode="durable_closed",
                               status=TaskStatus.awaiting_preview,
                               browser_decision="save_requested", preview_save_enabled=True)
        self.db.get.return_value = task
        self.db.scalar.return_value = SimpleNamespace(user_id=7, payload={
            "preview_token": token, "request_id": "original-authorized-action",
        })
        with (patch.object(routes_mixed_batches, "mixed_batch_state", return_value=_state()),
              patch.object(routes_mixed_batches.tasks, "_owned_binding_matches", return_value=True),
              patch.object(routes_mixed_batches.tasks, "confirm_durable_preview") as confirm,
              patch.object(routes_mixed_batches.tasks, "request_durable_sro_save") as request_save,
              patch.object(routes_mixed_batches, "enqueue_sro_durable_save") as enqueue):
            response = self.client.post(
                f"/upload-batches/{BATCH_ID}/mixed/save",
                json={"items": [{"file_index": 0, "preview_token": token}],
                      "snapshot_token": "f" * 64, "request_key": "e" * 48},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(response.json()["results"][0]["status"], "save_requested")
            confirm.assert_not_called()
            request_save.assert_not_called()
            enqueue.assert_called_once_with(100, 7, token)

    def test_applicant_binding_route_uses_exact_reviewed_card_service(self) -> None:
        with patch.object(routes_assignments.assignments, "bind_applicant_assignment_debtor") as bind:
            response = self.client.post(
                "/assignments/17/bind-debtor",
                data={"debtor_card_id": "29", "version": "4"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303, response.text)
            bind.assert_called_once_with(self.db, 17, 29, 7, 4)
            self.assertEqual(response.headers["location"], "/assignments/17?saved=1")
            invalid = self.client.post(
                "/assignments/17/bind-debtor",
                data={"debtor_card_id": "0", "version": "4"},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(invalid.status_code, 422)
            bind.assert_called_once()

    def test_applicant_debtor_options_query_only_reviewed_cards(self) -> None:
        self.db.get.return_value = SimpleNamespace(source_role="applicant")
        self.db.scalars.return_value = [SimpleNamespace(
            id=29, display_name="Тестовый должник", entity_kind="person", inn="",
        )]
        response = self.client.get("/assignments/17/debtor-options?q=Тестовый")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["options"], [{
            "id": 29, "display_name": "Тестовый должник",
            "entity_kind": "person", "inn": "",
        }])
        statement = self.db.scalars.call_args.args[0]
        self.assertIn("preparation_state", str(statement))
        self.assertIn("lower", str(statement).lower())

    def test_assignment_search_keeps_cardless_applicant_drafts(self) -> None:
        captured = []
        def worklist(_request, _db, statement, *_args):
            captured.append(statement)
            return [], 0, 1, 1, "", ""
        with patch.object(routes_assignments, "_worklist_page", side_effect=worklist):
            response = self.client.get("/assignments?q=Заявитель")
        self.assertEqual(response.status_code, 200, response.text)
        sql = str(captured[0]).lower()
        self.assertIn("left outer join", sql)
        self.assertIn("applicant", captured[0].compile().params.values())

    def test_common_mixed_entry_is_upload_only(self) -> None:
        with patch.object(routes_tasks, "_dashboard_context") as dashboard:
            response = self.client.get("/debtors/intake?registry=mixed")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Загрузить смешанную партию", response.text)
        self.assertNotIn("Обработка документов", response.text)
        dashboard.assert_not_called()

    def test_batch_folder_get_keeps_only_exact_selected_registry(self) -> None:
        state = _state()
        state["groups"]["assignments"] = [{"file_index": 9, "task_id": 109}]
        state["counts"] = {"persons": 3, "assignments": 1}
        state["total"] = 4
        with patch.object(routes_mixed_batches, "mixed_batch_state", return_value=state):
            response = self.client.get(f"/upload-batches/{BATCH_ID}/mixed?registry=assignments")
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(set(body["groups"]), {"assignments"})
            self.assertEqual(body["groups"]["assignments"][0]["file_index"], 9)
            self.assertEqual(body["total"], 1)
            self.assertEqual(body["snapshot_token"], "f" * 64)
            invalid = self.client.get(f"/upload-batches/{BATCH_ID}/mixed?registry=other")
            self.assertEqual(invalid.status_code, 422)

    def test_applicant_folder_link_requires_manifest_receipt_join(self) -> None:
        self.db.execute.return_value.all.return_value = [(17, BATCH_ID)]
        links = routes_assignments._applicant_source_batch_work_urls(self.db, [17])
        self.assertEqual(links[17], f"/upload-batches/{BATCH_ID}/work?registry=assignments")
        statement = self.db.execute.call_args.args[0]
        sql = str(statement).lower()
        self.assertIn("upload_transport_chunk_receipt", sql)
        self.assertIn("upload_transport_batch_item", sql)
        self.assertIn("content_sha256", sql)
        self.assertIn("file_index", sql)
        self.assertIn("chunk_index", sql)

    def test_task_folder_link_uses_exact_accepted_manifest_occurrence(self) -> None:
        task = SimpleNamespace(
            id=100, intake_item_id=3, intake_operation_id=8,
            document_id=5, recognized={}, scenario="person_card",
        )
        self.db.scalar.return_value = BATCH_ID
        with patch.object(routes_tasks.mixed_intake, "route_for_task",
                          return_value=SimpleNamespace(registry="persons")):
            url = routes_tasks._task_source_batch_work_url(self.db, task)
        self.assertEqual(url, f"/upload-batches/{BATCH_ID}/work?registry=persons")
        statement = self.db.scalar.call_args.args[0]
        sql = str(statement).lower()
        self.assertIn("upload_transport_chunk_receipt", sql)
        self.assertIn("upload_transport_batch_item", sql)
        self.assertIn("content_hash", sql)
        self.assertIn("file_index", sql)
        self.assertIn("filename", sql)

    def test_retry_child_supplies_read_view_while_original_remains_action_target(self) -> None:
        original = Task(id=100, document_id=1, scenario="person_card", profile="court_ruling",
                        status=TaskStatus.failed, recognized={}, values={}, screenshots=[])
        child = Task(id=200, document_id=1, scenario="person_card", profile="court_ruling",
                     status=TaskStatus.needs_review, recognized={}, values={}, screenshots=[])
        child.document = Document(id=1, stored_path="/synthetic/photo.jpg")
        self.db.get.side_effect = lambda model, key: {100: original, 200: child}.get(key) if model is Task else None
        self.db.scalar.return_value = None
        grouped = {"batch_id": BATCH_ID, "groups": {"persons": [{
            "file_index": 0, "filename": "synthetic.jpg", "task_id": 100,
            "display_task_id": 200, "status": "needs_review",
            "subject_kind": "person", "reason": "review_person_candidate",
        }]}, "total": 1, "counts": {"persons": 1}}
        with (patch.object(routes_mixed_batches, "_require_batch", return_value=SimpleNamespace(id=BATCH_ID)),
              patch.object(routes_mixed_batches.mixed_intake, "batch_registry_groups", return_value=grouped),
              patch.object(routes_mixed_batches, "_batch_snapshot", return_value="f" * 64),
              patch.object(routes_mixed_batches, "_row_snapshot", return_value="r" * 64) as row_snapshot,
              patch.object(routes_mixed_batches.fields, "review_fields",
                           side_effect=lambda task: [SimpleNamespace(title="Состояние", value="Дочерний OCR")]
                           if task.id == 200 else [])):
            row = routes_mixed_batches.mixed_batch_state(self.db, BATCH_ID, 7)["groups"]["persons"][0]
        self.assertEqual(row["task_id"], 100)
        self.assertEqual(row["display_task_id"], 200)
        self.assertEqual(row["task_url"], "/tasks/200?registry=persons")
        self.assertEqual(row["source_preview_url"], "/tasks/200/document/preview")
        self.assertEqual(row["data_summary"], "Состояние: Дочерний OCR")
        self.assertEqual(row["status"], "needs_review")
        self.assertIs(row_snapshot.call_args.args[2], original)
        self.assertEqual(row_snapshot.call_args.args[1]["status"], "failed")
        self.assertEqual(row_snapshot.call_args.args[1]["registry"], "review")

    def test_person_parser_applicant_routes_assignment_without_preview(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Заявитель: ПАО «Учебный заявитель»\n"
        )
        card = parse([text, text])
        self.assertEqual(type(card).__name__, "DebtorCard")
        route = classify_card(card, applicant_evidence=card.applicant_evidence)
        self.assertEqual((route.registry, route.reason), ("assignments", "explicit_applicant"))
        recognized = card_to_fields(card)
        recognized[routes_mixed_batches.tasks.MIXED_ROUTE_KEY] = route.as_dict()
        recognized[routes_mixed_batches.tasks.APPLICANT_EVIDENCE_KEY] = card.applicant_evidence
        task = Task(id=100, document_id=1, scenario="person_card", profile="court_ruling",
                    status=TaskStatus.needs_review, recognized=recognized, values={},
                    browser_decision="", screenshots=[], approved_by_id=None)
        self.assertEqual(mixed_intake.route_for_task(task).registry, "assignments")
        with patch.object(routes_mixed_batches.tasks, "lock_task_for_update"):
            self.assertFalse(routes_mixed_batches.tasks.admit_mixed_preview(self.db, task))


if __name__ == "__main__":
    unittest.main()
