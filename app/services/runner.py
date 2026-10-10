"""Запуск сценария по заданию.

Здесь те же три предохранителя, что и в консоли: пробный прогон по умолчанию,
поиск дублей до записи и обязательное подтверждение живым человеком. Запись в
реальный реестр включается только явным `live=True` от интерфейса.

На этом этапе используется общая учётка из `.env`. Безопасная изоляция сессий
между сотрудниками пока не реализована; текущие ограничения описаны в
`memory-bank/10-system.md`.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from sro_robot.config import Config
from sro_robot.platform import SroClient, SroError

from ..db.models import Task, TaskStatus
from . import tasks as task_service
from .browser_gate import DbReviewGate
from .fields import card_of, scenario_of

logger = logging.getLogger(__name__)


class NotApproved(RuntimeError):
    pass


def run_task(db: Session, task: Task, *, live: bool = False, force: bool = False,
             preview: bool = True, user_id: int | None = None) -> Task:
    if task.status != TaskStatus.approved:
        raise NotApproved("Задание не подтверждено оператором")
    if not task_service.approval_matches(db, task):
        task.status = TaskStatus.needs_review
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

    task_service.mark_writing(db, task, dry_run=not live)
    db.commit()

    cfg = Config.load()
    try:
        with SroClient(cfg, dry_run=not live) as client:
            report = scenario.preflight(client, card)
            if report.duplicates and not force:
                task_service.mark_duplicate(db, task, [h.name for h in report.duplicates])
                return task

            gate = DbReviewGate(task.id) if preview else None
            result = scenario.run(client, card, gate=gate)
            task_service.finish(db, task, record_url=result.record_url, message=result.message)
    except SroError as exc:
        logger.exception("SRO platform operation failed task_id=%s", task.id)
        task_service.fail(db, task, str(exc))
    except Exception as exc:  # платформа недоступна, браузер упал и прочее
        logger.exception("Unexpected SRO automation failure task_id=%s", task.id)
        task_service.fail(db, task, f"{type(exc).__name__}: {exc}")
    return task
