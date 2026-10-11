"""Run the approved local validation workflow.

This deployment intentionally refuses real SRO writes. `live=False` validates
the approved payload and records a local-only completion without creating an
SRO browser session. The parameters remain for compatibility with queued jobs.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import Task, TaskStatus
from . import tasks as task_service
from .fields import card_of, scenario_of


class NotApproved(RuntimeError):
    pass


class LiveWritesDisabled(NotApproved):
    """Real registry writes are disabled until the integration is approved."""


def run_task(db: Session, task: Task, *, live: bool = False, force: bool = False,
             preview: bool = True, user_id: int | None = None) -> Task:
    # This deployment must not mutate the real registry. The explicit guard is
    # also ahead of approval checks so stale queued `live=True` jobs fail closed.
    if live:
        raise LiveWritesDisabled(
            "Запись в реальный реестр СРО отключена; разрешена только локальная проверка"
        )

    # Keep the payload and approval stable through local validation and its
    # completion write. Operator edits use the same row lock and wait here.
    task_service.lock_task_for_update(db, task)
    if task.status != TaskStatus.approved:
        raise NotApproved("Задание не подтверждено оператором")
    if not task_service.approval_matches(db, task):
        task_service.transition_task_status(task, TaskStatus.needs_review)
        task.approved_by_id = None
        task_service.log(
            db,
            task,
            "approval_invalidated",
            "Текущие поля не совпадают с последним подтверждением оператора",
            user_id,
        )
        db.flush()
        raise NotApproved("Поля изменились после подтверждения; проверьте и подтвердите их снова")

    scenario = scenario_of(task)
    card = card_of(task)

    missing = scenario.validate(card)
    if missing:
        task_service.fail(db, task, f"Не заполнены обязательные поля: {', '.join(missing)}")
        return task

    task_service.finish_local_validation(db, task, user_id)
    return task
