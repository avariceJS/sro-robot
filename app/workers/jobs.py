"""Фоновые задания. Выполняются в процессе воркера, не в веб-процессе."""

from __future__ import annotations

import logging

from sro_robot.documents import get_profile
from sro_robot.core.ocr import OcrBudget
from sro_robot.platform import SroClient
from sro_robot.platform.errors import SroError
from sro_robot.scenarios import get_scenario

from ..db.base import engine, session_scope
from ..db.models import Task, TaskStatus
from ..db.schema import require_current_schema_revision
from ..services.kad_import import SOURCE_KEY, check_case
from ..services import runner
from ..services import leases
from ..services import tasks as task_service
from ..services import auth as auth_service
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
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is not None and claim is None:
                if not task_service.save_recognized(db, task, card):
                    logger.warning(
                        "Discarded stale OCR result task_id=%s",
                        task_id,
                    )
            elif task is not None and not leases.complete_ocr(db, claim, card):
                logger.warning(
                    "Discarded stale OCR result task_id=%s attempt=%s generation=%s",
                    task_id,
                    claim.attempt,
                    claim.generation,
                )
    except Exception as exc:
        # The commit may have succeeded even if its acknowledgement was lost.
        # Never turn a result-persistence error into a parse failure or retry state.
        logger.error(
            "Document recognition result persistence failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )


def write_to_sro(task_id: int, live: bool = False, force: bool = False,
                 preview: bool = True, user_id: int | None = None) -> None:
    _require_current_worker_schema(task_id)
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


def prepare_sro_form(task_id: int, user_id: int) -> None:
    """Fill a new SRO person form for review, then close it without saving."""
    _require_current_worker_schema(task_id)
    external_state = None
    external_session_id = None
    external_session_rejected = False
    try:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if (
                task is None
                or task.status != TaskStatus.writing
                or not task.dry_run
                or task.browser_decision not in ("", "retry")
            ):
                return
            if not task_service.approval_matches(db, task):
                logger.warning("SRO form preview withheld task_id=%s reason=approval_mismatch", task_id)
                return
            scenario = get_scenario(task.scenario)
            card = card_of(task)
            missing = scenario.validate(card)
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

        client = SroClient(
            dry_run=True,
            prepare_only=True,
            session_state=external_state,
        )
        try:
            with client:
                try:
                    result = scenario.run(
                        client, card, gate=DbReviewGate(task_id, user_id=user_id)
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
        if not result.ok or result.saved:
            raise RuntimeError("prepare-only scenario did not close cleanly")
    except Exception as exc:
        # The request guard blocks every registry POST except login, Add and
        # Close. Save is never a permissible outcome in this job.
        try:
            with session_scope() as db:
                if external_session_id is not None and (
                    external_session_rejected
                    or (
                        isinstance(exc, SroError)
                        and "сессия сро истекла" in str(exc).casefold()
                    )
                ):
                    auth_service.expire_external_login_session(
                        db, user_id, external_session_id
                    )
                task = db.get(Task, task_id)
                if task is not None and task.status == TaskStatus.writing:
                    task.error = preparation_error_text(exc)
                    db.commit()
        except Exception as persist_exc:
            logger.error(
                "SRO form preparation failure state could not be stored task_id=%s error_type=%s",
                task_id,
                type(persist_exc).__name__,
            )
        logger.error(
            "SRO form preparation failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        raise SroJobExecutionError(
            "SRO form preparation failed; inspect the task before trying again"
        ) from None
