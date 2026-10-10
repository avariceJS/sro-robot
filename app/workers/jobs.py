"""Фоновые задания. Выполняются в процессе воркера, не в веб-процессе."""

from __future__ import annotations

from sro_robot.documents import get_profile

from ..db.base import session_scope
from ..db.models import Task
from ..services import runner
from ..services import tasks as task_service


def recognize(task_id: int) -> None:
    try:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            task_service.mark_recognizing(db, task)
            path = task.document.stored_path
            profile = get_profile(task.profile)

        card = profile.parse(path)

        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is not None:
                task_service.save_recognized(db, task, card)
    except Exception as exc:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is not None:
                task_service.fail(db, task, f"{type(exc).__name__}: {exc}"[:2000])

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
