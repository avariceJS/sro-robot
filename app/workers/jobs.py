"""Фоновые задания. Выполняются в процессе воркера, не в веб-процессе."""

from __future__ import annotations

import logging

from sro_robot.documents import get_profile
from sro_robot.core.ocr import OcrBudget
from sro_robot.core.native_tesseract import configure_native_tesseract
from sro_robot.platform import SroClient
from sro_robot.platform.errors import SroError
from sro_robot.scenarios import get_scenario

from ..db.base import engine, session_scope
from ..db.models import Task, TaskStatus, User
from ..db.schema import require_current_schema_revision
from ..services.kad_import import SOURCE_KEY, check_case
from ..services import runner
from ..services import leases
from ..services import tasks as task_service
from ..services import auth as auth_service
from ..services.sro_account_lock import SroAccountBusy, sro_account_lease
from ..services.browser_gate import DbReviewGate
from ..services.fields import card_of
from ..settings import settings

logger = logging.getLogger(__name__)


class SroJobExecutionError(RuntimeError):
    """Safe RQ failure marker; the external operation result may be unknown."""


class WorkerSchemaUnavailable(RuntimeError):
    """A job was withheld because the runtime database is not at the exact head."""


def _require_current_worker_schema(task_id: int) -> None:
    try:
        with engine.connect() as connection:
            require_current_schema_revision(connection)
    except Exception as exc:
        logger.error(
            "Worker job withheld because database schema is unavailable task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        raise WorkerSchemaUnavailable("Worker database schema is not current") from None


def recognize(task_id: int) -> None:
    _require_current_worker_schema(task_id)
    claim = None
    lease_mode = settings.ocr_task_leases_enabled
    content_hash = None
    try:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            if lease_mode:
                claim = leases.claim_ocr(db, task_id)
                if claim is None:
                    return
            else:
                if not task_service.claim_unleased_task(db, task):
                    return
            kad_source = (task.recognized or {}).get(SOURCE_KEY)
            path = task.document.stored_path
            content_hash = task.document.content_hash
            profile = get_profile(task.profile)
    except Exception as exc:
        logger.error(
            "Document recognition claim or setup failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        return

    try:
        task_service.validate_stored_document_file(path, content_hash)
        ocr_budget = OcrBudget(
            deadline_seconds=settings.ocr_deadline_seconds,
            pdfinfo_timeout_seconds=settings.ocr_pdfinfo_timeout_seconds,
            pdftext_timeout_seconds=settings.ocr_pdftext_timeout_seconds,
            tesseract_timeout_seconds=settings.ocr_tesseract_timeout_seconds,
        )
        if hasattr(ocr_budget, "allow_native_tesseract"):
            # Pure text-layer PDFs need no model preflight.
            # The shared budget selects once immediately before real OCR.
            ocr_budget.allow_native_tesseract = True
        else:
            # A long-lived dev worker may still hold an older budget class.
            configure_native_tesseract()
        card = profile.parse(
            path,
            max_pdf_pages=settings.ocr_max_pdf_pages,
            max_image_pixels=settings.ocr_max_image_pixels,
            max_tiff_frames=settings.ocr_max_tiff_frames,
            budget=ocr_budget,
        )
        check_case(kad_source, card)
    except Exception as exc:
        logger.error(
            "Document recognition failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        try:
            with session_scope() as db:
                task = db.get(Task, task_id)
                if task is not None:
                    if lease_mode:
                        if claim is None:
                            return
                        if not leases.fail_ocr(
                            db, claim, type(exc).__name__,
                            employee_message=getattr(exc, "employee_message", None),
                        ):
                            logger.warning(
                                "Ignored stale OCR failure task_id=%s attempt=%s generation=%s",
                                task_id,
                                claim.attempt,
                                claim.generation,
                            )
                    else:
                        failed = task_service.fail(
                            db, task, getattr(exc, "employee_message", None)
                            or f"Document recognition failed ({type(exc).__name__})"
                        )
                        if not failed:
                            logger.warning(
                                "Ignored stale document recognition failure task_id=%s",
                                task_id,
                            )
        except Exception as persist_exc:
            logger.error(
                "Document recognition failure persistence failed task_id=%s error_type=%s",
                task_id,
                type(persist_exc).__name__,
            )
        return

    try:
        recognized_saved = False
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is not None and claim is None:
                if not task_service.save_recognized(db, task, card):
                    logger.warning(
                        "Discarded stale OCR result task_id=%s",
                        task_id,
                    )
                else:
                    recognized_saved = True
            elif task is not None and not leases.complete_ocr(db, claim, card):
                logger.warning(
                    "Discarded stale OCR result task_id=%s attempt=%s generation=%s",
                    task_id,
                    claim.attempt,
                    claim.generation,
                )
            elif task is not None:
                recognized_saved = True
    except Exception as exc:
        # The commit may have succeeded even if its acknowledgement was lost.
        # Never turn a result-persistence error into a parse failure or retry state.
        logger.error(
            "Document recognition result persistence failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        return

    if recognized_saved:
        try:
            _resolve_and_enqueue_mixed_preview(task_id)
        except Exception as exc:
            # OCR is already committed. The batch intent remains durable and
            # is reconciled separately; never relabel a good parse as failed.
            logger.error(
                "Mixed preview delivery uncertain task_id=%s error_type=%s",
                task_id, type(exc).__name__,
            )


def _resolve_and_enqueue_mixed_preview(task_id: int) -> None:
    """Resolve a pending batch intent after committed OCR, then queue safely.

    The intent is durable before Redis delivery. A missing acknowledgement is
    handled by batch replay reconciliation; the task claim prevents a second
    external Add after a worker began the first attempt.
    """
    from ..services import batch_actions
    from .queues import enqueue_sro_form_preview

    with session_scope() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        admitted = batch_actions.resolve_prepare_intent(db, task)
    if admitted is None:
        return
    admitted_task_id, actor_id = admitted
    enqueue_sro_form_preview(admitted_task_id, actor_id)


def write_to_sro(task_id: int, live: bool = False, force: bool = False,
                 preview: bool = True, user_id: int | None = None) -> None:
    _require_current_worker_schema(task_id)
    if settings.sro_recognition_only and live:
        raise SroJobExecutionError("SRO write is disabled in recognition-only mode")
    try:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            try:
                runner.run_task(db, task, live=live, force=force, preview=preview, user_id=user_id)
            except runner.NotApproved as exc:
                task_service.log(db, task, "not_approved", str(exc), user_id)
    except Exception as exc:
        # RQ stores uncaught exception messages and tracebacks in Redis. Do not
        # let driver/platform errors there disclose credentials or document data.
        # A failed job is not permission to retry: a future external write may
        # have crossed its side-effect boundary before the failure was observed.
        logger.error(
            "SRO job failed; outcome requires reconciliation task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        raise SroJobExecutionError(
            "SRO job failed; outcome requires read-only reconciliation before retry"
        ) from None


def preparation_error_text(exc: Exception) -> str:
    """Operator-facing preparation failure without credentials or document text."""
    fallback = "Подготовка формы не завершилась; проверьте состояние перед повтором"
    if type(exc).__name__ == "Error" and (
        "Executable doesn't exist" in str(exc) or "Failed to launch" in str(exc)
    ):
        return "Не удалось открыть окно СРО: браузер робота не найден."
    if not isinstance(exc, SroError):
        return fallback
    text = " ".join(str(exc).split())
    lowered = text.casefold()
    if (
        not text
        or len(text) > 240
        or "://" in text
        or "sro_" in lowered
        or "password" in lowered
    ):
        return fallback
    return text


def _prepare_sro_form_locked(task_id: int, user_id: int) -> None:
    """Prepare a person form in its admitted preview mode.

    Durable previews publish the screenshot only after the fresh blank form has
    been read back and closed. No Save authorization exists in this job.
    """
    external_state = None
    external_session_id = None
    external_session_rejected = False
    claimed = False
    owned_add_claimed = False
    test_mode = settings.sro_existing_card_test_mode
    durable_preview = False
    try:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if (
                task is None
                or task.status != TaskStatus.writing
                or not task.dry_run
                or task.browser_decision not in ("", "retry")
                or task.scenario not in ("person_card", "entity_review")
            ):
                return
            durable_preview = getattr(task, "preview_mode", "live_session") == "durable_closed"
            if settings.sro_recognition_only and (
                not settings.sro_manual_preview_enabled or durable_preview
            ):
                return
            if durable_preview and getattr(task, "preview_purpose", "") != "new_record_review":
                return
            if not durable_preview and task.scenario != "person_card":
                return
            if not durable_preview and not task_service.approval_matches(db, task):
                logger.warning("SRO form preview withheld task_id=%s reason=approval_mismatch", task_id)
                return
            scenario = get_scenario(task.scenario)
            if durable_preview and not getattr(scenario, "supports_prepare", False):
                return
            card = card_of(task)
            missing = (
                scenario.validate_prepare(card) if durable_preview
                else scenario.validate(card)
            )
            if missing:
                task.error = "Заполните обязательные поля перед подготовкой формы СРО"
                db.commit()
                return
            active_external = auth_service.active_external_state(
                db, user_id, settings.secret_key
            )
            if active_external is None:
                task.error = "Сессия СРО истекла или отсутствует; войдите в приложение снова"
                db.commit()
                return
            external_session_id, external_state = active_external
            actor = db.get(User, user_id)
            principal = actor.sro_login.strip() if actor is not None and actor.sro_login else ""
            if not principal:
                raise RuntimeError("verified SRO account identity is missing")
            if not task_service.claim_sro_preview(db, task, user_id):
                return
            claimed = True
            # The row lock refreshes task values. Build the browser input from
            # that locked approval snapshot, not the pre-claim ORM contents.
            card = card_of(task)
            if (task.scenario not in ("person_card", "entity_review")
                    or (durable_preview and not getattr(scenario, "supports_prepare", False))
                    or (scenario.validate_prepare(card) if durable_preview
                        else scenario.validate(card))):
                raise RuntimeError("claimed SRO preview input is invalid")

        manual_close_only = settings.sro_recognition_only
        client = SroClient(
            dry_run=True,
            prepare_only=True,
            session_state=external_state,
            operator_save_enabled=not test_mode and not durable_preview and not manual_close_only,
            owned_add_required=durable_preview,
        )
        try:
            with client:
                try:
                    if durable_preview or not test_mode:
                        report = (
                            scenario.preflight(client, card, allow_partial=True)
                            if durable_preview else scenario.preflight(client, card)
                        )
                        if report.duplicates:
                            registry = "firm" if task.scenario == "entity_review" else "person"
                            code = (
                                f"{registry}_card_exists" if report.duplicate_identity_confirmed
                                else f"{registry}_card_possible_duplicate"
                            )
                            raise SroError("SRO registry match found", code=code)
                    if durable_preview:
                        with session_scope() as db:
                            current = db.get(Task, task_id)
                            if (current is None or not task_service.claim_owned_sro_add(
                                    db, current, user_id, principal)):
                                raise RuntimeError("owned SRO Add claim was refused")
                        owned_add_claimed = True
                        if task.scenario == "entity_review":
                            client.arm_owned_firm_add()
                        else:
                            client.arm_owned_person_add()

                        def record_owned_add(record_url: str, baseline: dict[str, str]) -> None:
                            with session_scope() as db:
                                current = db.get(Task, task_id)
                                if (current is None or not task_service.complete_owned_sro_add(
                                        db, current, user_id, principal, record_url, baseline)):
                                    raise RuntimeError("owned SRO Add receipt was not recorded")

                        result = scenario.run(client, card, on_add=record_owned_add)
                        if not owned_add_claimed:
                            raise RuntimeError("owned SRO Add was not claimed")
                        with session_scope() as db:
                            current = db.get(Task, task_id)
                            if current is None or current.owned_add_state != "bound":
                                raise RuntimeError("owned SRO Add receipt is missing")
                            bound_url = current.owned_draft_url
                            bound_baseline = dict(current.owned_baseline or {})
                        scenario.verify_owned_blank_after_close(
                            client, bound_url, bound_baseline,
                        )
                    else:
                        result = scenario.run(
                            client, card,
                            gate=DbReviewGate(
                                task_id, user_id=user_id,
                                operator_save=not test_mode and not manual_close_only,
                            ),
                        )
                except Exception:
                    # A protected page may redirect to login after the initial
                    # check. Revoke only when the readable page lacks the
                    # authenticated-account marker.
                    try:
                        external_session_rejected = not client.is_logged_in()
                    except Exception:
                        pass
                    raise
        finally:
            # Drop the worker's decrypted copy as soon as the ephemeral browser closes.
            external_state = None
        if not result.ok:
            raise RuntimeError("person preview outcome was not verified")
        if durable_preview and result.saved:
            raise RuntimeError("durable preparation unexpectedly saved a record")
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:
                raise RuntimeError("person preview task disappeared")
            if durable_preview:
                task_service.begin_durable_sro_preview(
                    db, task, result.screenshots, user_id,
                    destination_snapshot=(
                        result.destination_snapshot if task.scenario == "entity_review" else None
                    ),
                )
            elif not task_service.complete_sro_preview(db, task, result, user_id):
                raise RuntimeError("person preview outcome could not be recorded")
    except Exception as exc:
        # Never requeue an uncertain Save. Durable save_started survives a
        # crash, and failure handling requires read-only reconciliation.
        failure_code = getattr(exc, "code", None) if isinstance(exc, SroError) else None
        if failure_code not in task_service.SRO_PREVIEW_FAILURE_MESSAGES:
            failure_code = None
        try:
            with session_scope() as db:
                if external_session_id is not None and (
                    external_session_rejected
                    or failure_code == "external_session_expired"
                    or (
                        isinstance(exc, SroError)
                        and "сессия сро истекла" in str(exc).casefold()
                    )
                ):
                    auth_service.expire_external_login_session(
                        db, user_id, external_session_id
                    )
                task = db.get(Task, task_id)
                if task is not None and claimed:
                    if owned_add_claimed:
                        task_service.fail_owned_sro_add(db, task, user_id)
                    if failure_code:
                        task_service.fail_sro_preview(db, task, user_id, failure_code=failure_code)
                    else:
                        task_service.fail_sro_preview(db, task, user_id)
        except Exception as persist_exc:
            logger.error(
                "SRO form preparation failure state could not be stored task_id=%s error_type=%s",
                task_id,
                type(persist_exc).__name__,
            )
        logger.error(
            "SRO form preparation failed task_id=%s error_type=%s failure_code=%s",
            task_id,
            type(exc).__name__,
            failure_code or "unclassified",
        )
        raise SroJobExecutionError(
            "SRO form preparation failed; read-only reconciliation is required"
        ) from None


class _CommittedSaveGate:
    """Return Save only inside a worker that has committed the one-shot claim."""

    def ask(self, screenshots: list[str]) -> str:
        if not screenshots:
            raise RuntimeError("fresh SRO form has no screenshot")
        return "save"


def _save_prepared_sro_form_locked(task_id: int, user_id: int, expected_token: str) -> None:
    """Rebuild an approved durable preview, then perform one employee-requested Save.

    The one-shot database marker is committed before opening SRO. A crash or
    ambiguous external response leaves the task requiring read-only
    reconciliation; this job never retries the Save.
    """
    external_state = None
    external_session_id = None
    external_session_rejected = False
    claimed = False
    try:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if (
                task is None
                or task.status != TaskStatus.awaiting_preview
                or getattr(task, "preview_mode", "live_session") != "durable_closed"
                or getattr(task, "preview_purpose", "") != "new_record_review"
                or not getattr(task, "preview_save_enabled", False)
                or task.browser_decision != "save_requested"
                or task.scenario not in ("person_card", "entity_review")
            ):
                return
            active_external = auth_service.active_external_state(
                db, user_id, settings.secret_key
            )
            if active_external is None:
                # No external operation has begun; keep the durable request for
                # explicit session recovery instead of fabricating a Save result.
                logger.warning(
                    "Durable SRO Save withheld task_id=%s reason=no_external_session", task_id
                )
                return
            external_session_id, external_state = active_external
            actor = db.get(User, user_id)
            principal = actor.sro_login.strip() if actor is not None and actor.sro_login else ""
            if not principal:
                return
            scenario = get_scenario(task.scenario)
            if not getattr(scenario, "supports_prepare", False):
                return
            card = card_of(task)
            blockers = list(scenario.validate(card))
            save_blockers = getattr(scenario, "save_blockers", None)
            if callable(save_blockers):
                blockers.extend(save_blockers(card))
            if blockers:
                logger.warning(
                    "Durable SRO Save withheld task_id=%s reason=required_fields_or_mapping_missing",
                    task_id,
                )
                return
            if not task_service.claim_durable_sro_save(db, task, user_id, expected_token):
                return
            claimed = True
            card = card_of(task)
            expected_destination = (
                dict(task.preview_destination or {}) if task.scenario == "entity_review" else None
            )
            owned_record_url = task.owned_draft_url
            expected_baseline = dict(task.owned_baseline or {})
            if task.owned_add_state != "bound" or not owned_record_url or not expected_baseline:
                raise RuntimeError("owned SRO draft receipt is missing")

        client = SroClient(
            dry_run=True,
            prepare_only=True,
            session_state=external_state,
            operator_save_enabled=True,
            owned_add_required=True,
        )
        try:
            with client:
                try:
                    report = (
                        scenario.preflight(
                            client, card,
                            owned_record_url=owned_record_url,
                            expected_baseline=expected_baseline,
                        ) if task.scenario == "entity_review"
                        else scenario.preflight(client, card)
                    )
                    if report.duplicates:
                        registry = "firm" if task.scenario == "entity_review" else "person"
                        code = (
                            f"{registry}_card_exists" if report.duplicate_identity_confirmed
                            else f"{registry}_card_possible_duplicate"
                        )
                        raise SroError("SRO registry match found", code=code)
                    if expected_destination is None:
                        result = scenario.run(
                            client, card, gate=_CommittedSaveGate(),
                            owned_record_url=owned_record_url,
                            expected_baseline=expected_baseline,
                        )
                    else:
                        result = scenario.run(
                            client, card, gate=_CommittedSaveGate(),
                            owned_record_url=owned_record_url,
                            expected_baseline=expected_baseline,
                            expected_destination=expected_destination,
                        )
                except Exception:
                    try:
                        external_session_rejected = not client.is_logged_in()
                    except Exception:
                        pass
                    raise
        finally:
            external_state = None
        if not (result.ok and result.saved):
            raise RuntimeError("SRO Save result was not verified")
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None or not task_service.complete_sro_preview(db, task, result, user_id):
                raise RuntimeError("SRO Save receipt could not be recorded")
    except Exception as exc:
        failure_code = getattr(exc, "code", None) if isinstance(exc, SroError) else None
        if failure_code not in task_service.SRO_PREVIEW_FAILURE_MESSAGES:
            failure_code = None
        try:
            with session_scope() as db:
                if external_session_id is not None and external_session_rejected:
                    auth_service.expire_external_login_session(
                        db, user_id, external_session_id
                    )
                task = db.get(Task, task_id)
                if task is not None and claimed:
                    # The durable save_started marker cannot be cleared by a
                    # failure, even if a local step appears to have failed.
                    task_service.fail_sro_preview(db, task, user_id)
        except Exception as persist_exc:
            logger.error(
                "SRO Save uncertainty could not be stored task_id=%s error_type=%s",
                task_id, type(persist_exc).__name__,
            )
        logger.error(
            "SRO Save failed task_id=%s error_type=%s failure_code=%s",
            task_id, type(exc).__name__, failure_code or "unclassified",
        )
        raise SroJobExecutionError(
            "SRO Save outcome requires read-only reconciliation before any retry"
        ) from None


def prepare_sro_form(task_id: int, user_id: int) -> None:
    """Prepare with a shared account lock and a fresh isolated browser."""
    _require_current_worker_schema(task_id)
    try:
        with sro_account_lease(user_id, shared=True):
            _prepare_sro_form_locked(task_id, user_id)
    except SroAccountBusy:
        # No task claim or external Add occurred; replay can safely redeliver.
        logger.warning("SRO preview waiting for account gate task_id=%s", task_id)
        raise SroJobExecutionError("SRO preview account is busy; redelivery required") from None
    except SroJobExecutionError:
        raise
    except Exception as exc:
        logger.error(
            "SRO preview account gate failed task_id=%s error_type=%s",
            task_id, type(exc).__name__,
        )
        raise SroJobExecutionError("SRO preview account gate unavailable") from None


def save_prepared_sro_form(task_id: int, user_id: int, expected_token: str) -> None:
    """Serialize Save against same-account previews and other Saves."""
    _require_current_worker_schema(task_id)
    if settings.sro_recognition_only:
        raise SroJobExecutionError("SRO Save is disabled in recognition-only mode")
    try:
        with sro_account_lease(user_id, shared=False):
            _save_prepared_sro_form_locked(task_id, user_id, expected_token)
    except SroAccountBusy:
        # The durable save_requested state remains unconsumed. A reconciled
        # redelivery may try again; no external Save attempt was made.
        logger.warning("SRO Save waiting for account gate task_id=%s", task_id)
        raise SroJobExecutionError("SRO Save account is busy; redelivery required") from None
    except SroJobExecutionError:
        raise
    except Exception as exc:
        logger.error(
            "SRO Save account gate failed task_id=%s error_type=%s",
            task_id, type(exc).__name__,
        )
        raise SroJobExecutionError("SRO Save account gate unavailable") from None


def _refresh_owned_sro_preview_locked(task_id: int, user_id: int, request_id: str) -> None:
    """Refresh the screenshot of one already-owned technical blank GUID."""
    external_state = None
    external_session_id = None
    external_session_rejected = False
    claimed = False
    try:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if (
                task is None or task.status != TaskStatus.awaiting_preview
                or task.preview_mode != "durable_closed"
                or task.preview_purpose != "new_record_review"
                or task.preview_refresh_state != "requested"
                or task.owned_add_state != "bound"
                or task.scenario not in ("person_card", "entity_review")
            ):
                return
            active_external = auth_service.active_external_state(
                db, user_id, settings.secret_key
            )
            if active_external is None:
                logger.warning(
                    "Owned SRO preview refresh withheld task_id=%s reason=no_external_session",
                    task_id,
                )
                return
            external_session_id, external_state = active_external
            if not task_service.claim_owned_preview_refresh(db, task, user_id, request_id):
                return
            claimed = True
            scenario = get_scenario(task.scenario)
            if not getattr(scenario, "supports_prepare", False):
                raise RuntimeError("owned SRO refresh scenario is unsupported")
            card = card_of(task)
            if scenario.validate_prepare(card):
                raise RuntimeError("owned SRO refresh identity is incomplete")
            owned_record_url = task.owned_draft_url
            expected_baseline = dict(task.owned_baseline or {})
            expected_destination = (
                dict(task.preview_destination or {}) if task.scenario == "entity_review"
                else None
            )
            if not owned_record_url or not expected_baseline:
                raise RuntimeError("owned SRO refresh receipt is incomplete")

        client = SroClient(
            dry_run=True, prepare_only=True, session_state=external_state,
            operator_save_enabled=False, owned_add_required=True,
        )
        try:
            with client:
                try:
                    if task.scenario == "entity_review":
                        report = scenario.preflight(
                            client, card, allow_partial=True,
                            owned_record_url=owned_record_url,
                            expected_baseline=expected_baseline,
                        )
                    else:
                        report = scenario.preflight(client, card, allow_partial=True)
                    if report.duplicates:
                        raise SroError("SRO registry match found", code="owned_preview_duplicate")
                    kwargs = {
                        "gate": None,
                        "owned_record_url": owned_record_url,
                        "expected_baseline": expected_baseline,
                    }
                    if expected_destination is not None:
                        kwargs["expected_destination"] = expected_destination
                    result = scenario.run(client, card, **kwargs)
                    if not result.ok or result.saved or not result.screenshots:
                        raise RuntimeError("owned SRO refresh did not close safely")
                    scenario.verify_owned_blank_after_close(
                        client, owned_record_url, expected_baseline,
                    )
                except Exception:
                    try:
                        external_session_rejected = not client.is_logged_in()
                    except Exception:
                        pass
                    raise
        finally:
            external_state = None
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None or not task_service.complete_owned_preview_refresh(
                db, task, user_id, result.screenshots,
                destination_snapshot=(
                    result.destination_snapshot if task.scenario == "entity_review" else None
                ),
            ):
                raise RuntimeError("owned SRO refresh snapshot was not stored")
    except Exception as exc:
        try:
            with session_scope() as db:
                if external_session_id is not None and external_session_rejected:
                    auth_service.expire_external_login_session(
                        db, user_id, external_session_id,
                    )
                task = db.get(Task, task_id)
                if task is not None and claimed:
                    task_service.fail_owned_preview_refresh(db, task, user_id)
        except Exception as persist_exc:
            logger.error(
                "Owned SRO refresh failure could not be stored task_id=%s error_type=%s",
                task_id, type(persist_exc).__name__,
            )
        logger.error(
            "Owned SRO preview refresh failed task_id=%s error_type=%s",
            task_id, type(exc).__name__,
        )
        raise SroJobExecutionError(
            "Owned SRO preview refresh requires read-only reconciliation"
        ) from None


def refresh_owned_sro_preview(task_id: int, user_id: int, request_id: str) -> None:
    """Use a shared account gate; never create a second technical SRO draft."""
    _require_current_worker_schema(task_id)
    if settings.sro_recognition_only:
        raise SroJobExecutionError("SRO draft refresh is disabled in recognition-only mode")
    try:
        with sro_account_lease(user_id, shared=True):
            _refresh_owned_sro_preview_locked(task_id, user_id, request_id)
    except SroAccountBusy:
        logger.warning("Owned SRO refresh waiting for account gate task_id=%s", task_id)
        raise SroJobExecutionError("Owned SRO refresh account is busy; redelivery required") from None
    except SroJobExecutionError:
        raise
    except Exception as exc:
        logger.error(
            "Owned SRO refresh account gate failed task_id=%s error_type=%s",
            task_id, type(exc).__name__,
        )
        raise SroJobExecutionError("Owned SRO refresh account gate unavailable") from None
