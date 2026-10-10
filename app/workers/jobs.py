"""Фоновые задания. Выполняются в процессе воркера, не в веб-процессе."""

from __future__ import annotations

import logging

from sro_robot.documents import get_profile

from ..db.base import session_scope
from ..db.models import Task
from ..services import runner
from ..services import leases
from ..services import tasks as task_service
from ..settings import settings

logger = logging.getLogger(__name__)


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

        card = profile.parse(path)

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
        logger.error(
            "Document recognition failed task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is not None:
                if lease_mode and claim is None:
                    # Missing migration or DB failure must not terminally change the task.
                    return
                if not lease_mode:
                    task_service.fail(db, task, f"Document recognition failed ({type(exc).__name__})")
                elif not leases.fail_ocr(db, claim, type(exc).__name__):
                    logger.warning(
                        "Ignored stale OCR failure task_id=%s attempt=%s generation=%s",
                        task_id,
                        claim.attempt,
                        claim.generation,
                    )


def write_to_sro(task_id: int, live: bool = False, force: bool = False,
                 preview: bool = True, user_id: int | None = None) -> None:
    with session_scope() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        try:
            runner.run_task(db, task, live=live, force=force, preview=preview, user_id=user_id)
        except runner.NotApproved as exc:
            task_service.log(db, task, "not_approved", str(exc), user_id)
