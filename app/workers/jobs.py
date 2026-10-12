"""Фоновые задания. Выполняются в процессе воркера, не в веб-процессе."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sro_robot.documents import get_profile
from sro_robot.kad import fetch_latest_ruling
from sro_robot.platform import SroClient
from sro_robot.scenarios import get_scenario

from ..capabilities import KAD_ACQUISITION_ENABLED
from ..db.base import engine, session_scope
from ..db.models import Task, TaskStatus
from ..db.schema import require_current_schema_revision
from ..services.kad_import import SOURCE_KEY, check_case
from ..services import runner
from ..services import leases
from ..services import tasks as task_service
from ..services.browser_gate import DbReviewGate
from ..services.fields import card_of
from ..settings import settings

logger = logging.getLogger(__name__)


class SroJobExecutionError(RuntimeError):
    """Safe RQ failure marker; the external operation result may be unknown."""


class KADAcquisitionDisabled(RuntimeError):
    """The KAD worker is withheld until act-selection safety is qualified."""


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
        card = profile.parse(path)
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
                        if not leases.fail_ocr(db, claim, type(exc).__name__):
                            logger.warning(
                                "Ignored stale OCR failure task_id=%s attempt=%s generation=%s",
                                task_id,
                                claim.attempt,
                                claim.generation,
                            )
                    else:
                        failed = task_service.fail(
                            db, task, f"Document recognition failed ({type(exc).__name__})"
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


def recognize_from_kad(task_id: int) -> None:
    """Получает последний опубликованный акт из КАД и разбирает его локально."""
    if not KAD_ACQUISITION_ENABLED:
        logger.error(
            "MKAD acquisition withheld task_id=%s reason=candidate_selection_unqualified",
            task_id,
        )
        raise KADAcquisitionDisabled("MKAD candidate selection is not qualified")
    _require_current_worker_schema(task_id)
    try:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            if not task_service.mark_recognizing(db, task):
                return
            case_number = task.case_number
            destination = Path(task.document.stored_path)
            profile = get_profile(task.profile)
            task_service.log(
                db,
                task,
                "kad_lookup_started",
                "Поиск судебного акта в КАД начат",
                task.assignee_id,
            )
    except Exception as exc:
        logger.error(
            "MKAD worker claim or setup failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        return

    try:
        pdf_bytes, filename = fetch_latest_ruling(case_number)
        if not pdf_bytes.startswith(b"%PDF-"):
            raise ValueError("КАД вернул файл, который не является PDF")
        destination.parent.mkdir(parents=True, exist_ok=True)
        task_service._write_document_file(destination, pdf_bytes)
    except Exception as exc:
        _fail_kad_acquisition_or_parse(task_id, exc)
        return

    try:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            task_service.lock_task_for_update(db, task)
            if task.status != TaskStatus.recognizing:
                logger.warning(
                    "Discarded stale MKAD document metadata task_id=%s",
                    task_id,
                )
                return
            task.document.filename = filename or f"КАД {case_number}.pdf"
            task.document.content_hash = hashlib.sha256(pdf_bytes).hexdigest()
            task_service.log(db, task, "kad_document_received", "Судебный акт получен; начинается разбор PDF", task.assignee_id)
    except Exception as exc:
        # A failed commit acknowledgement can mean the acquired document metadata
        # already committed. Leave this non-replayable KAD task for reconciliation.
        logger.error(
            "MKAD document metadata persistence failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        return

    try:
        card = profile.parse(destination)
    except Exception as exc:
        _fail_kad_acquisition_or_parse(task_id, exc)
        return

    try:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is not None and not task_service.save_recognized(db, task, card):
                logger.warning(
                    "Discarded stale MKAD parse result task_id=%s",
                    task_id,
                )
    except Exception as exc:
        logger.error(
            "MKAD parsed-result persistence failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        # The result commit may have succeeded. Never overwrite it with failure.


def _fail_kad_acquisition_or_parse(task_id: int, exc: Exception) -> None:
    logger.error(
        "MKAD acquisition or document parsing failed task_id=%s error_type=%s",
        task_id,
        type(exc).__name__,
    )
    try:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is not None and task.status == TaskStatus.recognizing:
                task_service.fail(
                    db,
                    task,
                    f"MKAD acquisition or document parsing failed ({type(exc).__name__})",
                )
    except Exception as persist_exc:
        logger.error(
            "MKAD failure-state persistence failed task_id=%s error_type=%s",
            task_id,
            type(persist_exc).__name__,
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


def prepare_sro_form(task_id: int, user_id: int) -> None:
    """Fill a new SRO person form for review, then close it without saving."""
    _require_current_worker_schema(task_id)
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

        client = SroClient(dry_run=True, prepare_only=True)
        with client:
            result = scenario.run(client, card, gate=DbReviewGate(task_id, user_id=user_id))
        if not result.ok or result.saved:
            raise RuntimeError("prepare-only scenario did not close cleanly")
    except Exception as exc:
        # The request guard blocks every registry POST except login, Add and
        # Close. Save is never a permissible outcome in this job.
        try:
            with session_scope() as db:
                task = db.get(Task, task_id)
                if task is not None and task.status == TaskStatus.writing:
                    task.error = "Подготовка формы не завершилась; проверьте состояние перед повтором"
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
