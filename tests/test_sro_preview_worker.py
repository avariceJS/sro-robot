"""Synthetic worker boundaries: claim once, persist result, never retry Save."""

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.db.models import TaskStatus
from app.workers import jobs
from sro_robot.platform.errors import SroError
from sro_robot.scenarios.base import ScenarioResult


class SroPreviewWorkerTests(unittest.TestCase):
    @contextmanager
    def worker(self, *, claim=True, result=None, error=None):
        task = SimpleNamespace(
            id=304, status=TaskStatus.writing, dry_run=True,
            browser_decision="", scenario="person_card", error="",
        )
        db = MagicMock()
        db.get.return_value = task
        scenario = MagicMock()
        scenario.validate.return_value = []
        scenario.run.return_value = result or ScenarioResult(ok=True)
        scenario.run.side_effect = error
        client = MagicMock()
        client.__enter__.return_value = client
        client.is_logged_in.return_value = True

        @contextmanager
        def session():
            yield db

        with ExitStack() as stack:
            def mocked(target, name, **kwargs):
                return stack.enter_context(patch.object(target, name, **kwargs))
            mocked(jobs, "_require_current_worker_schema")
            mocked(jobs, "session_scope", side_effect=session)
            mocked(jobs, "get_scenario", return_value=scenario)
            card_of = mocked(jobs, "card_of", side_effect=["old-input", "locked-input"])
            mocked(jobs.task_service, "approval_matches", return_value=True)
            claim_mock = mocked(jobs.task_service, "claim_sro_preview", return_value=claim)
            complete = mocked(jobs.task_service, "complete_sro_preview", return_value=True)
            fail = mocked(jobs.task_service, "fail_sro_preview", return_value=True)
            mocked(jobs.auth_service, "active_external_state", return_value=(708, b"synthetic-session"))
            revoke = mocked(jobs.auth_service, "expire_external_login_session")
            factory = mocked(jobs, "SroClient", return_value=client)
            gate_factory = mocked(jobs, "DbReviewGate")
            yield SimpleNamespace(
                task=task, db=db, scenario=scenario, client=client,
                claim=claim_mock, complete=complete, fail=fail, factory=factory,
                gate_factory=gate_factory, revoke=revoke, card_of=card_of,
            )

    def test_claim_loser_never_opens_browser_or_marks_owner_failure(self):
        with self.worker(claim=False) as w:
            jobs.prepare_sro_form(w.task.id, 17)
            w.factory.assert_not_called()
            w.complete.assert_not_called()
            w.fail.assert_not_called()

    def test_close_completes_using_only_locked_input_and_guarded_client(self):
        result = ScenarioResult(ok=True, saved=False)
        with self.worker(result=result) as w:
            jobs.prepare_sro_form(w.task.id, 17)
            w.claim.assert_called_once_with(w.db, w.task, 17)
            w.factory.assert_called_once_with(
                dry_run=True, prepare_only=True, session_state=b"synthetic-session",
                operator_save_enabled=True,
            )
            w.gate_factory.assert_called_once_with(w.task.id, user_id=17, operator_save=True)
            self.assertEqual(w.scenario.run.call_args.args, (w.client, "locked-input"))
            w.complete.assert_called_once_with(w.db, w.task, result, 17)
            w.fail.assert_not_called()

    def test_saved_result_is_recorded_without_another_scenario_run(self):
        result = ScenarioResult(ok=True, saved=True,
                                record_url="https://synthetic.invalid/register/person_card.aspx?id=123")
        with self.worker(result=result) as w:
            jobs.prepare_sro_form(w.task.id, 17)
            w.complete.assert_called_once_with(w.db, w.task, result, 17)
            w.scenario.run.assert_called_once()
            w.fail.assert_not_called()

    def test_exception_after_save_started_is_reconciled_without_retry(self):
        with self.worker(error=RuntimeError("sensitive synthetic sentinel")) as w:
            def uncertain_save(*args, **kwargs):
                w.task.status = TaskStatus.awaiting_preview
                w.task.browser_decision = "save_started"
                raise RuntimeError("sensitive synthetic sentinel")
            w.scenario.run.side_effect = uncertain_save
            with self.assertLogs(jobs.logger, level="ERROR") as logs:
                with self.assertRaises(jobs.SroJobExecutionError) as raised:
                    jobs.prepare_sro_form(w.task.id, 17)
            self.assertNotIn("sensitive synthetic sentinel", str(raised.exception))
            self.assertNotIn("sensitive synthetic sentinel", " ".join(logs.output))
            w.fail.assert_called_once_with(w.db, w.task, 17)
            w.complete.assert_not_called()
            w.scenario.run.assert_called_once()
            self.assertEqual(w.task.browser_decision, "save_started")

    def test_form_failure_reports_known_reason_without_revoking_valid_login(self):
        error = SroError("sensitive synthetic sentinel", code="new_person_identity_unbound")
        with self.worker(error=error) as w:
            with self.assertLogs(jobs.logger, level="ERROR") as logs:
                with self.assertRaises(jobs.SroJobExecutionError) as raised:
                    jobs.prepare_sro_form(w.task.id, 17)
            w.fail.assert_called_once_with(
                w.db, w.task, 17, failure_code="new_person_identity_unbound",
            )
            w.revoke.assert_not_called()
            self.assertIn("new_person_identity_unbound", " ".join(logs.output))
            self.assertNotIn("sensitive synthetic sentinel", " ".join(logs.output))
            self.assertNotIn("sensitive synthetic sentinel", str(raised.exception))

    def test_unrecognized_failure_code_is_not_exposed(self):
        with self.worker(error=SroError("private-message", code="private-code")) as w:
            with self.assertLogs(jobs.logger, level="ERROR") as logs:
                with self.assertRaises(jobs.SroJobExecutionError):
                    jobs.prepare_sro_form(w.task.id, 17)
            w.fail.assert_called_once_with(w.db, w.task, 17)
            self.assertNotIn("private-code", " ".join(logs.output))

    def test_unrecordable_result_becomes_failure_without_browser_retry(self):
        with self.worker() as w:
            w.complete.return_value = False
            with self.assertLogs(jobs.logger, level="ERROR"):
                with self.assertRaises(jobs.SroJobExecutionError):
                    jobs.prepare_sro_form(w.task.id, 17)
            w.fail.assert_called_once_with(w.db, w.task, 17)
            w.scenario.run.assert_called_once()

    def test_duplicate_job_for_consumed_claim_is_withheld(self):
        for decision in ("preparing", "save_started", "saved", "save_unknown", "closed"):
            with self.subTest(decision=decision), self.worker() as w:
                w.task.browser_decision = decision
                jobs.prepare_sro_form(w.task.id, 17)
                w.claim.assert_not_called()
                w.factory.assert_not_called()
                w.fail.assert_not_called()

    def test_other_scenarios_cannot_enter_person_save_mode(self):
        with self.worker() as w:
            w.task.scenario = "assignment"
            jobs.prepare_sro_form(w.task.id, 17)
            w.factory.assert_not_called()
            w.claim.assert_not_called()

