from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch
from starlette.requests import Request

from app.db.models import TaskStatus
from app.api import routes_tasks
from app.services.fields import review_fields
from app.services import tasks
from sro_robot.core.extract import parse
from sro_robot.core.models import card_to_fields


class TaskLifecycleTransitionTests(unittest.TestCase):
    def test_task_page_receives_persisted_review_suggestions_separately(self) -> None:
        suggestions = [{"kind": "application_person", "warning": "review", "fields": {}}]
        task = SimpleNamespace(
            id=501, screenshots=[], values={}, approved_by_id=None,
            status=TaskStatus.needs_review,
            recognized={tasks.REVIEW_SUGGESTIONS_KEY: suggestions},
            document=None,
            scenario="person_card",
            preview_mode="live_session",
            intake_item_id=None,
            intake_operation_id=None,
        )
        request = Request({
            "type": "http", "method": "GET", "path": "/tasks/501",
            "headers": [], "query_string": b"",
        })
        db = MagicMock()
        db.query.return_value.filter_by.return_value.one_or_none.return_value = None
        rendered = object()

        with (
            patch.object(routes_tasks, "_require_task", return_value=task),
            patch.object(routes_tasks, "_review_operation_scope", return_value=[]),
            patch.object(routes_tasks, "_ocr_retry_review_scope", return_value=None),
            patch.object(routes_tasks.case_packages, "active_case_packages_for_task", return_value=[]),
            patch.object(routes_tasks.fields, "review_fields", return_value=[]),
            patch.object(routes_tasks.tasks, "status_label", return_value="На проверке"),
            patch.object(routes_tasks.tasks, "approval_matches", return_value=False),
            patch.object(routes_tasks.tasks, "review_token", return_value="review-token"),
            patch.object(routes_tasks.ocr_retries, "context_for_task", return_value={}),
            patch.object(routes_tasks.templates, "TemplateResponse", return_value=rendered) as render,
        ):
            response = routes_tasks.task_page(request, 501, SimpleNamespace(id=7), db)

        self.assertIs(response, rendered)
        self.assertEqual(render.call_args.args[2]["review_suggestions"], suggestions)

    def test_unbound_ocr_candidates_are_saved_separately_from_card_fields(self) -> None:
        card = parse([
            "Рассмотрев заявление гражданки Макетовой Анны Петровны, "
            "ИНН 772881234508, суд установил обстоятельства дела."
        ])
        db = MagicMock()
        task = SimpleNamespace(
            status=TaskStatus.recognizing,
            recognized={},
            scenario="person_card",
            case_number="",
            values={},
        )

        with (
            patch.object(tasks, "lock_task_for_update"),
            patch("app.services.kad_import.check_case"),
            patch.object(tasks, "transition_task_status"),
            patch.object(tasks, "log"),
            patch("app.services.assignments.prepare_debtor_card_from_ocr"),
        ):
            self.assertTrue(tasks.save_recognized(db, task, card))

        self.assertEqual(
            task.recognized[tasks.REVIEW_SUGGESTIONS_KEY], card.review_suggestions,
        )
        self.assertTrue(task.recognized[tasks.REVIEW_SUGGESTIONS_COMPLETE_KEY])
        self.assertIsNone(task.recognized["last_name"]["value"])
        self.assertIsNone(task.recognized["inn"]["value"])
        editable = {field.attr: field.value for field in review_fields(task)}
        self.assertTrue(editable["last_name"])
        self.assertTrue(editable["first_name"])
        self.assertTrue(editable["middle_name"])
        self.assertTrue(editable["inn"])

    def test_unbound_candidate_prefills_best_reading_and_respects_operator_value(self) -> None:
        def suggestion(value: str) -> dict:
            return {
                "kind": "application_person",
                "copyable_fields": ["first_name"],
                "fields": {"first_name": {"value": value, "confidence": 0.65}},
            }

        def first_name(suggestions: list[dict], *, complete: bool | None = True, values=None) -> str:
            recognized = {tasks.REVIEW_SUGGESTIONS_KEY: suggestions}
            if complete is not None:
                recognized[tasks.REVIEW_SUGGESTIONS_COMPLETE_KEY] = complete
            task = SimpleNamespace(
                scenario="person_card",
                recognized=recognized,
                values=values or {},
            )
            return {field.attr: field.value for field in review_fields(task)}["first_name"]

        self.assertEqual(first_name([suggestion("Анна"), suggestion("Анна")]), "Анна")
        self.assertEqual(first_name([suggestion("Анна"), suggestion("Мария")]), "Анна")
        self.assertEqual(first_name([suggestion("Анна")], complete=False), "Анна")
        self.assertEqual(first_name([suggestion("Анна")], complete=None), "Анна")
        self.assertEqual(first_name([suggestion("Анна")], values={"first_name": ""}), "")

    def test_candidate_overflow_suppresses_prefill(self) -> None:
        text = " ".join(
            "Рассмотрев заявление гражданки Макетовой Анны Петровны, "
            f"ИНН 772881234508, суд установил обстоятельства номер {index}."
            for index in range(9)
        )
        card = parse([text])

        self.assertEqual(len(card.review_suggestions), 8)
        self.assertFalse(card.review_suggestions_complete)
        task = SimpleNamespace(
            scenario="person_card",
            recognized={
                **card_to_fields(card),
                tasks.REVIEW_SUGGESTIONS_KEY: card.review_suggestions,
                tasks.REVIEW_SUGGESTIONS_COMPLETE_KEY: card.review_suggestions_complete,
            },
            values={},
        )
        editable = {field.attr: field.value for field in review_fields(task)}
        self.assertEqual(editable["first_name"], "Анна")

    def test_skipped_fifth_ocr_variant_suppresses_prefill(self) -> None:
        first_four = (
            "Рассмотрев заявление гражданки Макетовой Анны Петровны, "
            "ИНН 772881234508, суд установил обстоятельства дела."
        )
        fifth = (
            "Рассмотрев заявление гражданки Ивановой Марии Петровны, "
            "ИНН 500100001146, суд установил обстоятельства дела."
        )
        card = parse([first_four, first_four, first_four, first_four, fifth])

        self.assertTrue(card.review_suggestions)
        self.assertFalse(card.review_suggestions_complete)
        task = SimpleNamespace(
            scenario="person_card",
            recognized={
                **card_to_fields(card),
                tasks.REVIEW_SUGGESTIONS_KEY: card.review_suggestions,
                tasks.REVIEW_SUGGESTIONS_COMPLETE_KEY: card.review_suggestions_complete,
            },
            values={},
        )
        editable = {field.attr: field.value for field in review_fields(task)}
        self.assertEqual(editable["first_name"], "Анна")

    def test_transition_graph_covers_all_states_and_keeps_terminals_closed(self) -> None:
        expected = {
            TaskStatus.new: {TaskStatus.recognizing, TaskStatus.failed, TaskStatus.rejected},
            TaskStatus.recognizing: {
                TaskStatus.recognizing,
                TaskStatus.needs_review,
                TaskStatus.failed,
                TaskStatus.duplicate,
            },
            TaskStatus.needs_review: {TaskStatus.approved, TaskStatus.rejected},
            TaskStatus.approved: {
                TaskStatus.needs_review,
                TaskStatus.approved,
                TaskStatus.rejected,
                TaskStatus.writing,
                TaskStatus.done,
                TaskStatus.failed,
            },
            TaskStatus.writing: {TaskStatus.done, TaskStatus.awaiting_preview},
            TaskStatus.awaiting_preview: set(),
            TaskStatus.done: set(),
            TaskStatus.failed: set(),
            TaskStatus.duplicate: set(),
            TaskStatus.rejected: set(),
        }
        self.assertEqual(set(tasks.TASK_STATUS_TRANSITIONS), set(TaskStatus))
        self.assertEqual(
            tasks.TASK_STATUS_TRANSITIONS,
            {source: frozenset(targets) for source, targets in expected.items()},
        )
        for terminal in (
            TaskStatus.done,
            TaskStatus.failed,
            TaskStatus.duplicate,
            TaskStatus.rejected,
        ):
            with self.subTest(terminal=terminal):
                self.assertEqual(tasks.TASK_STATUS_TRANSITIONS[terminal], frozenset())

    def test_transition_helper_applies_declared_edges_and_rejects_undeclared_edges(self) -> None:
        task = SimpleNamespace(status=TaskStatus.approved)
        tasks.transition_task_status(task, TaskStatus.needs_review)
        self.assertEqual(task.status, TaskStatus.needs_review)

        task.status = TaskStatus.done
        with self.assertRaisesRegex(tasks.InvalidTaskTransition, "done -> failed"):
            tasks.transition_task_status(task, TaskStatus.failed)
        self.assertEqual(task.status, TaskStatus.done)

    def test_writing_and_preview_transitions_append_minimal_task_events(self) -> None:
        db = MagicMock()
        task = SimpleNamespace(
            id=18,
            status=TaskStatus.approved,
            values={"debtor_name": "Synthetic Debtor"},
            approved_by_id=7,
            dry_run=False,
            error="previous safe error",
            screenshots=[],
            browser_decision="old decision",
        )

        with patch.object(tasks, "approval_matches", return_value=True):
            tasks.mark_writing(db, task, dry_run=True)

        self.assertEqual(task.status, TaskStatus.writing)
        writing_event = db.add.call_args.args[0]
        self.assertEqual(writing_event.kind, "writing_started")
        self.assertIsNone(writing_event.user_id)
        self.assertEqual(writing_event.payload, {"dry_run": True})
        self.assertNotIn("Synthetic Debtor", repr(writing_event))

        db.reset_mock()
        screenshots = ["synthetic-one.png", "synthetic-two.png"]
        tasks.begin_preview(db, task, screenshots)

        self.assertEqual(task.status, TaskStatus.awaiting_preview)
        self.assertEqual(task.screenshots, screenshots)
        self.assertEqual(task.browser_decision, "")
        preview_event = db.add.call_args.args[0]
        self.assertEqual(preview_event.kind, "preview_started")
        self.assertEqual(preview_event.payload, {"screenshot_count": 2, "preview_token": tasks.preview_token(task)})
        self.assertNotIn("synthetic-one.png", repr(preview_event))

    def test_sro_preview_retry_requires_reconciliation_state_and_current_approval(self) -> None:
        task = SimpleNamespace(
            id=29,
            status=TaskStatus.writing,
            dry_run=True,
            error="safe failure summary",
            screenshots=[],
            browser_decision="",
            values={"debtor_name": "Synthetic Debtor"},
        )
        db = MagicMock()
        with (
            patch.object(tasks, "lock_task_for_update"),
            patch.object(tasks, "approval_matches", return_value=True),
        ):
            self.assertTrue(tasks.retry_sro_preview_after_reconciliation(db, task))

        self.assertEqual(task.error, "")
        self.assertEqual(task.browser_decision, "retry")
        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "writing_started")
        self.assertEqual(event.payload, {"dry_run": True})
        self.assertNotIn("Synthetic Debtor", repr(event))

        for changes in (
            {"status": TaskStatus.approved},
            {"dry_run": False},
            {"error": ""},
            {"screenshots": ["private.png"]},
            {"browser_decision": "close"},
        ):
            candidate = SimpleNamespace(
                id=30,
                status=TaskStatus.writing,
                dry_run=True,
                error="safe failure summary",
                screenshots=[],
                browser_decision="",
                values={},
                **{},
            )
            for key, value in changes.items():
                setattr(candidate, key, value)
            with (
                patch.object(tasks, "lock_task_for_update"),
                patch.object(tasks, "approval_matches", return_value=True),
            ):
                with self.subTest(changes=changes):
                    self.assertFalse(tasks.retry_sro_preview_after_reconciliation(db, candidate))

        with (
            patch.object(tasks, "lock_task_for_update"),
            patch.object(tasks, "approval_matches", return_value=False),
        ):
            self.assertFalse(tasks.retry_sro_preview_after_reconciliation(db, task))

    def test_finish_event_uses_fixed_message_instead_of_caller_content(self) -> None:
        task = SimpleNamespace(
            id=19,
            status=TaskStatus.writing,
            record_url="",
        )
        db = MagicMock()

        tasks.finish(db, task, record_url="https://registry.invalid/synthetic")

        self.assertEqual(task.status, TaskStatus.done)
        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "done")
        self.assertEqual(event.message, "Task processing completed")
        self.assertEqual(event.payload, {})

    def test_rejected_operator_action_is_attributed_without_submitted_values(self) -> None:
        task = SimpleNamespace(id=18, status=TaskStatus.writing)
        db = MagicMock()

        tasks.log_operator_action_not_applied(
            db,
            task,
            action="save_fields",
            user_id=12,
            request_id="e" * 32,
        )

        event = db.add.call_args.args[0]
        self.assertEqual(event.task_id, task.id)
        self.assertEqual(event.user_id, 12)
        self.assertEqual(event.kind, "operator_action_not_applied")
        self.assertEqual(
            event.payload,
            {
                "action": "save_fields",
                "reason": "precondition_not_met",
                "request_id": "e" * 32,
            },
        )
        self.assertNotIn("Synthetic Private Name", repr(event.payload))
        db.flush.assert_called_once_with()

    def test_rejected_operator_action_omits_invalid_request_id_and_rejects_unknown_action(self) -> None:
        task = SimpleNamespace(id=18, status=TaskStatus.writing)
        db = MagicMock()

        tasks.log_operator_action_not_applied(
            db,
            task,
            action="reject",
            user_id=12,
            request_id="caller-controlled-value",
        )
        event = db.add.call_args.args[0]
        self.assertEqual(
            event.payload,
            {"action": "reject", "reason": "precondition_not_met"},
        )

        with self.assertRaisesRegex(ValueError, "Unsupported operator audit action"):
            tasks.log_operator_action_not_applied(
                db,
                task,
                action="arbitrary",
                user_id=12,
            )
    def test_rejection_accepts_only_queued_or_operator_review_states(self) -> None:
        for status in (TaskStatus.new, TaskStatus.needs_review, TaskStatus.approved):
            with self.subTest(status=status):
                task = SimpleNamespace(
                    id=17,
                    status=status,
                    approved_by_id=9 if status == TaskStatus.approved else None,
                )
                db = MagicMock()

                tasks.reject(db, task, user_id=7, request_id="a" * 32)

                self.assertEqual(task.status, TaskStatus.rejected)
                self.assertIsNone(task.approved_by_id)
                event = db.add.call_args.args[0]
                self.assertEqual(event.kind, "rejected")
                self.assertEqual(event.user_id, 7)
                self.assertEqual(event.payload, {"request_id": "a" * 32})

    def test_rejection_refuses_active_recognition(self) -> None:
        task = SimpleNamespace(id=17, status=TaskStatus.recognizing, approved_by_id=None)
        db = MagicMock()

        with self.assertRaises(tasks.InvalidTaskTransition):
            tasks.reject(db, task, user_id=7)

        self.assertEqual(task.status, TaskStatus.recognizing)
        db.add.assert_not_called()

    def test_local_validation_admission_requires_current_approval(self) -> None:
        task = SimpleNamespace(id=17, status=TaskStatus.approved, values={})
        db = MagicMock()
        approval_event = SimpleNamespace(id=41)
        db.scalar.side_effect = [approval_event, None]
        with patch.object(tasks, "approval_matches", return_value=True), patch(
            "app.services.outbox.create_local_validation_intent", return_value=41
        ) as create_intent:
            self.assertTrue(tasks.request_local_validation(db, task, 7, "d" * 32))
        create_intent.assert_called_once_with(
            db, task_id=task.id, actor_id=7, approval_event_id=approval_event.id,
        )
        event = db.add.call_args.args[0]
        self.assertEqual(event.kind, "local_validation_requested")
        self.assertEqual(
            event.payload,
            {"execution_mode": "local_validation", "saved": False, "request_id": "d" * 32},
        )

        task.status = TaskStatus.rejected
        db.reset_mock()
        with patch.object(tasks, "approval_matches") as matches:
            self.assertFalse(tasks.request_local_validation(db, task, 7, "d" * 32))
        matches.assert_not_called()
        db.add.assert_not_called()

    def test_expired_preview_is_recorded_only_while_still_waiting(self) -> None:
        task = SimpleNamespace(
            id=17,
            status=TaskStatus.awaiting_preview,
            browser_decision="",
            error="",
        )
        db = MagicMock()

        self.assertTrue(tasks.expire_preview(db, task))
        self.assertEqual(task.error, "Время ожидания истекло; запрошено закрытие формы без записи")
        self.assertEqual(task.browser_decision, "expired")
        self.assertEqual(db.add.call_args.args[0].kind, "preview_timeout")

        db.reset_mock()
        self.assertFalse(tasks.expire_preview(db, task))
        db.add.assert_not_called()

    def test_preview_decision_is_recorded_once(self) -> None:
        task = SimpleNamespace(
            id=23,
            status=TaskStatus.awaiting_preview,
            browser_decision="",
        )
        db = MagicMock()

        with patch.object(tasks, "lock_task_for_update"):
            self.assertTrue(tasks.decide_preview(db, task, "close", 7, "a" * 32))
            self.assertFalse(tasks.decide_preview(db, task, "close", 7, "b" * 32))

        self.assertEqual(task.browser_decision, "close")
        self.assertEqual(db.add.call_count, 1)
        self.assertEqual(db.add.call_args.args[0].kind, "decision_close")

    def test_terminal_tasks_reject_unguarded_service_transitions(self) -> None:
        attempts = (
            lambda db, task: tasks.reject(db, task, user_id=7),
            lambda db, task: tasks.mark_writing(db, task, dry_run=False),
            lambda db, task: tasks.finish(db, task, record_url="/synthetic"),
            lambda db, task: tasks.mark_duplicate(db, task, ["synthetic.pdf"]),
            lambda db, task: tasks.finish_local_validation(db, task, user_id=7),
        )
        for transition in attempts:
            with self.subTest(transition=transition):
                task = SimpleNamespace(
                    id=19,
                    status=TaskStatus.done,
                    dry_run=True,
                    record_url="",
                    error="",
                    values={},
                )
                db = MagicMock()

                with self.assertRaises(tasks.InvalidTaskTransition):
                    transition(db, task)

                self.assertEqual(task.status, TaskStatus.done)
                db.add.assert_not_called()



class ScreenshotSaveServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = MagicMock()
        self.lock = patch.object(tasks, "lock_task_for_update").start()
        self.approval = patch.object(tasks, "approval_matches", return_value=True).start()
        self.addCleanup(patch.stopall)
        self.task = SimpleNamespace(
            id=701, status=TaskStatus.awaiting_preview, scenario="person_card",
            dry_run=True, browser_decision="", screenshots=["synthetic-filled.png"],
            recognized={"last_name": {"value": "Synthetic"}}, values={"first_name": "Example"},
            approved_by_id=7, record_url="", error="",
        )
        self.token = tasks.preview_token(self.task)
        self.started = SimpleNamespace(
            id=20, user_id=7, payload={"preview_token": self.token},
            created_at=datetime.now(timezone.utc),
        )
        self.request = SimpleNamespace(user_id=7, payload={"preview_token": self.token})
        self.now = datetime.now(timezone.utc)

        def scalar(statement):
            sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
            if "clock_timestamp" in sql:
                return self.now
            return self.request if "decision_save" in sql else self.started

        self.db.scalar.side_effect = scalar

    def test_preview_token_binds_snapshot_but_not_lifecycle_timestamps(self) -> None:
        self.task.browser_decision = "save_requested"
        self.task.updated_at = datetime.now(timezone.utc)
        self.assertEqual(tasks.preview_token(self.task), self.token)
        for attr, value in (
            ("screenshots", ["different.png"]), ("values", {"first_name": "Changed"}),
            ("recognized", {}), ("approved_by_id", 8),
        ):
            original = getattr(self.task, attr)
            setattr(self.task, attr, value)
            with self.subTest(attr=attr):
                self.assertNotEqual(tasks.preview_token(self.task), self.token)
            setattr(self.task, attr, original)

    def test_preparation_claim_is_single_use_and_does_not_recover_dead_worker(self) -> None:
        self.task.status = TaskStatus.writing
        self.task.screenshots = []
        self.assertTrue(tasks.claim_sro_preview(self.db, self.task, 7))
        self.assertEqual(self.task.browser_decision, "preparing")
        self.assertFalse(tasks.claim_sro_preview(self.db, self.task, 7))
        self.assertFalse(tasks.retry_sro_preview_after_reconciliation(self.db, self.task))
        self.assertEqual(self.db.add.call_count, 1)
        self.assertEqual(self.lock.call_count, 3)

    def test_preparation_claim_checks_refreshed_scenario(self) -> None:
        self.task.status = TaskStatus.writing
        self.task.screenshots = []
        self.lock.side_effect = lambda _db, task: setattr(task, "scenario", "assignment")
        self.assertFalse(tasks.claim_sro_preview(self.db, self.task, 7))
        self.db.add.assert_not_called()

    def test_current_actor_can_request_exact_snapshot_once(self) -> None:
        self.assertTrue(tasks.can_save_preview(self.db, self.task, 7))
        self.assertTrue(tasks.decide_preview(self.db, self.task, "save", 7, "a" * 32, self.token))
        self.assertEqual(self.task.browser_decision, "save_requested")
        self.assertFalse(tasks.decide_preview(self.db, self.task, "save", 7, "a" * 32, self.token))
        self.assertFalse(tasks.decide_preview(self.db, self.task, "close", 7))
        event = self.db.add.call_args.args[0]
        self.assertEqual(event.kind, "decision_save")
        self.assertEqual(event.payload, {"preview_token": self.token, "request_id": "a" * 32})
        self.assertNotIn("Synthetic", repr(event))

    def test_save_refuses_missing_stale_or_other_actor_snapshot(self) -> None:
        for token, actor in (("", 7), ("f" * 64, 7), (self.token, 8)):
            with self.subTest(token=token, actor=actor):
                self.assertFalse(tasks.decide_preview(self.db, self.task, "save", actor, expected_token=token))
        self.started.created_at -= timedelta(seconds=601)
        self.assertFalse(tasks.can_save_preview(self.db, self.task, 7))
        self.assertFalse(tasks.decide_preview(self.db, self.task, "save", 7, expected_token=self.token))
        self.db.add.assert_not_called()

    def test_freshness_uses_event_database_clock_with_exact_expiry_boundary(self) -> None:
        # Deliberately far from the worker clock: all event age comparisons
        # must use the database clock which created the event timestamp.
        self.started.created_at = datetime.now(timezone.utc) - timedelta(days=3)
        self.now = self.started.created_at + timedelta(seconds=600)
        self.assertTrue(tasks.can_save_preview(self.db, self.task, 7))
        self.now += timedelta(microseconds=1)
        self.assertFalse(tasks.can_save_preview(self.db, self.task, 7))
        self.now = self.started.created_at - timedelta(microseconds=1)
        self.assertFalse(tasks.can_save_preview(self.db, self.task, 7))

    def test_changed_fields_or_approval_cannot_authorize_save(self) -> None:
        self.task.values = {"first_name": "Changed"}
        self.assertFalse(tasks.decide_preview(self.db, self.task, "save", 7, expected_token=self.token))
        self.assertFalse(tasks.can_save_preview(self.db, self.task, 7))
        self.task.values = {"first_name": "Example"}
        self.approval.return_value = False
        self.assertFalse(tasks.decide_preview(self.db, self.task, "save", 7, expected_token=self.token))

    def test_save_checks_locked_current_state_not_stale_request_object(self) -> None:
        self.lock.side_effect = lambda _db, task: setattr(task, "browser_decision", "expired")
        self.assertFalse(tasks.decide_preview(self.db, self.task, "save", 7, expected_token=self.token))
        self.db.add.assert_not_called()

    def test_save_consume_is_single_use_and_binds_request_actor(self) -> None:
        self.task.browser_decision = "save_requested"
        self.db.scalar.side_effect = [self.started, self.now, self.request]
        self.assertTrue(tasks.consume_preview_save(self.db, self.task, 7, self.token))
        self.assertEqual(self.task.browser_decision, "save_started")
        self.assertFalse(tasks.consume_preview_save(self.db, self.task, 7, self.token))
        self.assertFalse(tasks.expire_preview(self.db, self.task))
        self.assertEqual(self.db.add.call_args.args[0].kind, "preview_save_started")

    def test_save_consume_refuses_request_actor_mismatch_and_snapshot_drift(self) -> None:
        self.task.browser_decision = "save_requested"
        self.request.user_id = 8
        self.db.scalar.side_effect = [self.started, self.now, self.request]
        self.assertFalse(tasks.consume_preview_save(self.db, self.task, 7, self.token))
        self.task.screenshots = ["replacement.png"]
        self.assertFalse(tasks.consume_preview_save(self.db, self.task, 7, self.token))
        self.db.add.assert_not_called()

    def test_verified_saved_receipt_is_terminal_without_status_transition(self) -> None:
        self.task.browser_decision = "save_started"
        result = SimpleNamespace(ok=True, saved=True,
            record_url="https://sroarbitr.sro-service.ru/register/person_card.aspx?id=synthetic-id")
        self.assertTrue(tasks.complete_sro_preview(self.db, self.task, result, 7))
        self.assertEqual(self.task.status, TaskStatus.awaiting_preview)
        self.assertEqual(self.task.browser_decision, "saved")
        self.assertFalse(self.task.dry_run)
        self.assertEqual(self.task.record_url, result.record_url)
        self.assertFalse(tasks.complete_sro_preview(self.db, self.task, result, 7))
        self.assertFalse(tasks.fail_sro_preview(self.db, self.task, 7))
        self.assertEqual(self.db.add.call_count, 1)

    def test_unverified_save_result_preserves_unknown_nonretryable_outcome(self) -> None:
        for url in ("", "https://example.invalid/register/person_card.aspx?id=x",
                    "https://sroarbitr.sro-service.ru/register/person_card.aspx",
                    "https://sroarbitr.sro-service.ru/register/person_card.aspx?id=",
                    "https://sroarbitr.sro-service.ru/register/person_card.aspx?id=x&id=y",
                    "https://sroarbitr.sro-service.ru/register/person_card.aspx?id=x&action=edit"):
            with self.subTest(url=url):
                self.task.browser_decision = "save_started"
                self.task.dry_run = True
                self.assertTrue(tasks.complete_sro_preview(self.db, self.task,
                    SimpleNamespace(ok=True, saved=True, record_url=url), 7))
                self.assertEqual(self.task.browser_decision, "save_unknown")
                self.assertFalse(self.task.dry_run)
                self.assertEqual(self.task.record_url, "")
                self.assertFalse(tasks.retry_sro_preview_after_reconciliation(self.db, self.task))

    def test_failure_after_save_marker_is_unknown_and_cannot_be_retried(self) -> None:
        self.task.browser_decision = "save_started"
        self.assertTrue(tasks.fail_sro_preview(self.db, self.task, 7))
        self.assertEqual(self.task.browser_decision, "save_unknown")
        self.assertFalse(self.task.dry_run)
        self.assertFalse(tasks.consume_preview_save(self.db, self.task, 7, self.token))
        self.assertFalse(tasks.claim_sro_preview(self.db, self.task, 7))
        self.assertIn("Повтор запрещён", self.task.error)

    def test_pre_save_failure_does_not_claim_successful_close(self) -> None:
        self.assertTrue(tasks.fail_sro_preview(self.db, self.task, 7))
        self.assertEqual(self.task.browser_decision, "preview_failed")
        self.assertTrue(self.task.dry_run)
        self.assertNotIn("форма закрыта", self.task.error)

    def test_known_form_failure_retains_actionable_reason_and_existing_audit_contract(self) -> None:
        self.assertTrue(tasks.fail_sro_preview(
            self.db, self.task, 7, failure_code="new_person_identity_unbound",
        ))
        self.assertIn("с добавлением новой карточки", self.task.error)
        self.assertEqual(self.task.browser_decision, "preview_failed")
        self.assertEqual(self.db.add.call_args.args[0].payload, {})

    def test_form_failure_reason_never_overwrites_unknown_save_outcome(self) -> None:
        self.task.browser_decision = "save_started"
        self.assertTrue(tasks.fail_sro_preview(
            self.db, self.task, 7, failure_code="person_readback_mismatch",
        ))
        self.assertEqual(self.task.browser_decision, "save_unknown")
        self.assertIn("Результат сохранения СРО неизвестен", self.task.error)
        self.assertFalse(self.task.dry_run)

    def test_close_completion_requires_worker_result(self) -> None:
        self.task.browser_decision = "close"
        self.assertTrue(tasks.complete_sro_preview(self.db, self.task,
            SimpleNamespace(ok=True, saved=False), 7))
        self.assertEqual(self.task.browser_decision, "closed")
        self.assertTrue(self.task.dry_run)
        self.assertEqual(self.db.add.call_args.args[0].kind, "preview_closed")
