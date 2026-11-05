"""Synthetic checks for a one-shot employee-requested SRO Save."""

from contextlib import ExitStack, contextmanager, nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.db.models import Task, TaskStatus
from app.workers import jobs
from app.services.sro_account_lock import SroAccountBusy
from sro_robot.scenarios.base import ScenarioResult


class DurableSroSaveWorkerTests(unittest.TestCase):
    @contextmanager
    def worker(self, *, claimed=True, active_session=True, error=None,
               decision="save_requested", mode="durable_closed", enabled=True,
               test_mode=False):
        task = SimpleNamespace(
            id=87, status=TaskStatus.awaiting_preview,
            preview_mode=mode, preview_save_enabled=enabled,
            preview_purpose="new_record_review",
            browser_decision=decision, scenario="person_card", preview_destination={},
            owned_add_state="bound",
            owned_draft_url="https://synthetic.invalid/register/person_card.aspx?id=owned-87",
            owned_baseline={"last_name": ""},
        )
        db = MagicMock()
        db.get.side_effect = lambda model, _id: (
            task if model is Task else SimpleNamespace(sro_login="synthetic-principal")
        )
        scenario = MagicMock()
        scenario.supports_prepare = True
        scenario.validate.return_value = []
        scenario.save_blockers.return_value = []
        scenario.preflight.return_value = SimpleNamespace(
            duplicates=[], duplicate_identity_confirmed=False,
        )
        scenario.run.return_value = ScenarioResult(
            ok=True, saved=True,
            record_url="https://synthetic.invalid/register/person_card.aspx?id=87",
        )
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
                return stack.enter_context(patch.object(target, name, **kwargs))

            mocked(jobs, "_require_current_worker_schema")
            mocked(jobs, "sro_account_lease", return_value=nullcontext())
            mocked(jobs, "settings", secret_key="synthetic-secret",
                   sro_existing_card_test_mode=test_mode)
            mocked(jobs, "session_scope", side_effect=session)
            mocked(jobs, "get_scenario", return_value=scenario)
            card_of = mocked(jobs, "card_of", return_value="locked-input")
            claim = mocked(jobs.task_service, "claim_durable_sro_save", return_value=claimed)
            claim.side_effect = lambda *_args: order.append("claimed") or claimed
            complete = mocked(jobs.task_service, "complete_sro_preview", return_value=True)
            fail = mocked(jobs.task_service, "fail_sro_preview", return_value=True)
            active = mocked(
                jobs.auth_service, "active_external_state",
                return_value=(4, b"synthetic-session") if active_session else None,
            )
            factory = mocked(jobs, "SroClient", return_value=client)
            yield SimpleNamespace(
                task=task, db=db, scenario=scenario, client=client,
                claim=claim, complete=complete, fail=fail, active=active,
                factory=factory, card_of=card_of, order=order,
            )

    def test_exact_claim_commits_before_browser_and_saves_once(self):
        with self.worker() as w:
            jobs.save_prepared_sro_form(w.task.id, 17, "snapshot-token")
            w.claim.assert_called_once_with(w.db, w.task, 17, "snapshot-token")
            self.assertEqual(w.order[:2], ["claimed", "committed"])
            w.factory.assert_called_once_with(
                dry_run=True, prepare_only=True, session_state=b"synthetic-session",
                operator_save_enabled=True, owned_add_required=True,
            )
            w.scenario.preflight.assert_called_once_with(w.client, "locked-input")
            self.assertEqual(w.scenario.run.call_args.args, (w.client, "locked-input"))
            self.assertEqual(
                w.scenario.run.call_args.kwargs["owned_record_url"],
                w.task.owned_draft_url,
            )
            self.assertEqual(
                w.scenario.run.call_args.kwargs["expected_baseline"],
                w.task.owned_baseline,
            )
            w.client.arm_owned_person_add.assert_not_called()
            gate = w.scenario.run.call_args.kwargs["gate"]
            self.assertEqual(gate.ask(["fresh-screenshot"]), "save")
            w.complete.assert_called_once()
            w.fail.assert_not_called()

    def test_duplicate_delivery_or_missing_entitlement_never_opens_browser(self):
        for options in (
            {"claimed": False}, {"decision": "save_started"},
            {"mode": "live_session"}, {"enabled": False},
        ):
            with self.subTest(options=options), self.worker(**options) as w:
                jobs.save_prepared_sro_form(w.task.id, 17, "snapshot-token")
                w.factory.assert_not_called()
                w.complete.assert_not_called()

    def test_missing_external_session_preserves_unconsumed_request(self):
        with self.worker(active_session=False) as w:
            with self.assertLogs(jobs.logger, level="WARNING"):
                jobs.save_prepared_sro_form(w.task.id, 17, "snapshot-token")
            w.claim.assert_not_called()
            w.factory.assert_not_called()
            w.fail.assert_not_called()

    def test_scoped_durable_save_is_allowed_under_legacy_test_mode(self):
        with self.worker(test_mode=True) as w:
            jobs.save_prepared_sro_form(w.task.id, 17, "snapshot-token")
            w.claim.assert_called_once()
            w.factory.assert_called_once()

    def test_missing_new_record_purpose_cannot_enter_save_mode(self):
        with self.worker() as w:
            w.task.preview_purpose = ""
            jobs.save_prepared_sro_form(w.task.id, 17, "snapshot-token")
            w.claim.assert_not_called()
            w.factory.assert_not_called()

    def test_qualified_entity_scenario_rehydrates_under_same_one_shot_claim(self):
        with self.worker() as w:
            w.task.scenario = "entity_review"
            w.task.preview_destination = {
                "opf_label": "ООО", "opf_value": "synthetic-catalogue-id",
            }
            jobs.save_prepared_sro_form(w.task.id, 17, "snapshot-token")
            w.scenario.preflight.assert_called_once()
            self.assertEqual(w.scenario.run.call_args.kwargs["expected_destination"],
                             w.task.preview_destination)
            w.complete.assert_called_once()

    def test_known_firm_address_blocker_does_not_consume_save_or_open_browser(self):
        with self.worker() as w:
            w.task.scenario = "entity_review"
            w.scenario.save_blockers.return_value = ["Адрес организации не перенесён в СРО"]
            with self.assertLogs(jobs.logger, level="WARNING"):
                jobs.save_prepared_sro_form(w.task.id, 17, "snapshot-token")
            w.claim.assert_not_called()
            w.factory.assert_not_called()
            w.fail.assert_not_called()

    def test_missing_save_fields_do_not_consume_save_or_open_browser(self):
        with self.worker() as w:
            w.scenario.validate.return_value = ["synthetic missing field"]
            with self.assertLogs(jobs.logger, level="WARNING"):
                jobs.save_prepared_sro_form(w.task.id, 17, "snapshot-token")
            w.claim.assert_not_called()
            w.factory.assert_not_called()

    def test_failure_after_claim_is_unknown_without_sensitive_error(self):
        with self.worker(error=RuntimeError("private synthetic sentinel")) as w:
            with self.assertLogs(jobs.logger, level="ERROR") as logs:
                with self.assertRaises(jobs.SroJobExecutionError) as raised:
                    jobs.save_prepared_sro_form(w.task.id, 17, "snapshot-token")
            w.fail.assert_called_once_with(w.db, w.task, 17)
            w.complete.assert_not_called()
            self.assertNotIn("private synthetic sentinel", str(raised.exception))
            self.assertNotIn("private synthetic sentinel", " ".join(logs.output))

    def test_fresh_screenshot_required_for_authorized_save(self):
        with self.assertRaises(RuntimeError):
            jobs._CommittedSaveGate().ask([])

    def test_busy_account_exposes_failure_without_consuming_save_claim(self):
        with self.worker() as w:
            with (
                patch.object(jobs, "sro_account_lease", side_effect=SroAccountBusy),
                self.assertLogs(jobs.logger, level="WARNING"),
                self.assertRaises(jobs.SroJobExecutionError),
            ):
                jobs.save_prepared_sro_form(w.task.id, 17, "snapshot-token")
            w.claim.assert_not_called()
            w.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
