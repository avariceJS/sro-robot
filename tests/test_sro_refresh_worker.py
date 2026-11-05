"""Synthetic same-GUID preview refresh; no external SRO calls."""

from contextlib import ExitStack, contextmanager, nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.db.models import Task, TaskStatus
from app.workers import jobs
from sro_robot.scenarios.base import ScenarioResult


class SroRefreshWorkerTests(unittest.TestCase):
    @contextmanager
    def worker(self, *, claim=True, error=None, scenario_name="person_card"):
        task = SimpleNamespace(
            id=73, status=TaskStatus.awaiting_preview,
            preview_mode="durable_closed", preview_purpose="new_record_review",
            preview_refresh_state="requested", owned_add_state="bound",
            scenario=scenario_name,
            owned_draft_url=(
                "https://synthetic.invalid/register/firm_card.aspx?id=owned-73"
                if scenario_name == "entity_review" else
                "https://synthetic.invalid/register/person_card.aspx?id=owned-73"
            ),
            owned_baseline={"last_name": ""},
            preview_destination={"opf_label": "ООО", "opf_value": "opaque-1"},
        )
        db = MagicMock()
        db.get.side_effect = lambda model, _id: (
            task if model is Task else SimpleNamespace(sro_login="synthetic-principal")
        )
        scenario = MagicMock()
        scenario.supports_prepare = True
        scenario.validate_prepare.return_value = []
        scenario.preflight.return_value = SimpleNamespace(duplicates=[])
        scenario.run.return_value = ScenarioResult(
            ok=True, saved=False, screenshots=["synthetic-refresh-shot"],
        )
        if scenario_name == "entity_review":
            scenario.run.return_value.destination_snapshot = dict(task.preview_destination)
        scenario.run.side_effect = error
        client = MagicMock()
        client.__enter__.return_value = client
        client.is_logged_in.return_value = True
        order = []

        @contextmanager
        def session():
            yield db
            order.append("committed")

        with ExitStack() as stack:
            def mocked(target, name, **kwargs):
                return stack.enter_context(patch.object(target, name, create=True, **kwargs))

            mocked(jobs, "_require_current_worker_schema")
            mocked(jobs, "sro_account_lease", return_value=nullcontext())
            mocked(jobs, "settings", secret_key="synthetic-secret")
            mocked(jobs, "session_scope", side_effect=session)
            mocked(jobs, "get_scenario", return_value=scenario)
            mocked(jobs, "card_of", return_value="locked-card")
            claim_mock = mocked(jobs.task_service, "claim_owned_preview_refresh", return_value=claim)
            complete = mocked(jobs.task_service, "complete_owned_preview_refresh", return_value=True)
            fail = mocked(jobs.task_service, "fail_owned_preview_refresh", return_value=True)
            mocked(jobs.auth_service, "active_external_state", return_value=(9, b"synthetic-session"))
            factory = mocked(jobs, "SroClient", return_value=client)
            yield SimpleNamespace(
                task=task, db=db, scenario=scenario, client=client,
                claim=claim_mock, complete=complete, fail=fail,
                factory=factory, order=order,
            )

    def test_refresh_claim_commits_before_browser_and_never_adds(self):
        with self.worker() as w:
            jobs.refresh_owned_sro_preview(w.task.id, 17, "request-1")
            w.claim.assert_called_once_with(w.db, w.task, 17, "request-1")
            w.factory.assert_called_once_with(
                dry_run=True, prepare_only=True, session_state=b"synthetic-session",
                operator_save_enabled=False, owned_add_required=True,
            )
            w.scenario.run.assert_called_once_with(
                w.client, "locked-card", gate=None,
                owned_record_url=w.task.owned_draft_url,
                expected_baseline=w.task.owned_baseline,
            )
            w.scenario.verify_owned_blank_after_close.assert_called_once_with(
                w.client, w.task.owned_draft_url, w.task.owned_baseline,
            )
            w.complete.assert_called_once_with(
                w.db, w.task, 17, ["synthetic-refresh-shot"], destination_snapshot=None,
            )
            w.client.arm_owned_person_add.assert_not_called()
            w.client.arm_owned_firm_add.assert_not_called()
            w.fail.assert_not_called()

    def test_duplicate_delivery_cannot_open_browser(self):
        with self.worker(claim=False) as w:
            jobs.refresh_owned_sro_preview(w.task.id, 17, "request-1")
            w.factory.assert_not_called()
            w.complete.assert_not_called()

    def test_failure_after_claim_marks_unknown_and_preserves_no_add(self):
        with self.worker(error=RuntimeError("private synthetic sentinel")) as w:
            with self.assertLogs(jobs.logger, level="ERROR") as logs:
                with self.assertRaises(jobs.SroJobExecutionError) as caught:
                    jobs.refresh_owned_sro_preview(w.task.id, 17, "request-1")
            w.fail.assert_called_once_with(w.db, w.task, 17)
            w.complete.assert_not_called()
            w.client.arm_owned_person_add.assert_not_called()
            self.assertNotIn("private synthetic sentinel", str(caught.exception))
            self.assertNotIn("private synthetic sentinel", " ".join(logs.output))

    def test_entity_refresh_retains_destination_pair(self):
        with self.worker(scenario_name="entity_review") as w:
            jobs.refresh_owned_sro_preview(w.task.id, 17, "request-1")
            w.scenario.preflight.assert_called_once_with(
                w.client, "locked-card", allow_partial=True,
                owned_record_url=w.task.owned_draft_url,
                expected_baseline=w.task.owned_baseline,
            )
            self.assertEqual(
                w.scenario.run.call_args.kwargs["expected_destination"],
                w.task.preview_destination,
            )
            self.assertEqual(
                w.complete.call_args.kwargs["destination_snapshot"],
                w.task.preview_destination,
            )


if __name__ == "__main__":
    unittest.main()
