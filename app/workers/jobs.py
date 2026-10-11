"""Фоновые задания. Выполняются в процессе воркера, не в веб-процессе."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sro_robot.documents import get_profile
from sro_robot.kad import fetch_latest_ruling

from ..db.base import session_scope
from ..db.models import Task, TaskStatus
from ..services import runner
from ..services import leases
from ..services import tasks as task_service
from ..settings import settings

logger = logging.getLogger(__name__)


class SroJobExecutionError(RuntimeError):
    """Safe RQ failure marker; the external operation result may be unknown."""


def recognize(task_id: int) -> None:
    claim = None
    lease_mode = settings.ocr_task_leases_enabled
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
            path = task.document.stored_path
            profile = get_profile(task.profile)
    except Exception as exc:
        logger.error(
            "Document recognition claim or setup failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        return

    try:
        card = profile.parse(path)
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
                task_service.save_recognized(db, task, card)
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
            if task is not None:
                task_service.save_recognized(db, task, card)
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
