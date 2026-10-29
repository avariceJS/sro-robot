"""Остановка на заполненной форме до решения оператора.

Робот заполняет карточку за секунды и раньше сразу закрывал черновик — человек
не успевал ничего увидеть. Теперь сценарий останавливается, показывает снимки
заполненной формы в интерфейсе и ждёт ответа: сохранить или закрыть.

Браузер всё это время открыт, поэтому решение можно принимать и глядя в само
окно СРО, а не только на снимки.
"""

from __future__ import annotations

import time

from ..db.base import session_scope
from ..db.models import Task, TaskStatus
from . import tasks as task_service

# Дольше ждать смысла нет: воркер занят, браузер открыт. По истечении срока
# черновик закрывается — это безопасный исход.
DEFAULT_TIMEOUT_S = 600
POLL_S = 1.0


class DbReviewGate:
    """`ReviewGate`, который спрашивает оператора через базу и веб-интерфейс."""

    def __init__(self, task_id: int, timeout_s: int = DEFAULT_TIMEOUT_S, user_id: int | None = None,
                 *, operator_save: bool = False):
        self.task_id = task_id
        self.timeout_s = timeout_s
        self.user_id = user_id
        self.operator_save = operator_save

    def ask(self, screenshots: list[str]) -> str:
        with session_scope() as db:
            task = db.get(Task, self.task_id)
            if task is None:
                return "close"
            try:
                task_service.begin_preview(db, task, screenshots, self.user_id)
                snapshot_token = task_service.preview_token(task)
            except task_service.InvalidTaskTransition:
                return "close"

        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            time.sleep(POLL_S)
            with session_scope() as db:
                task = db.get(Task, self.task_id)
                if task is None:
                    return "close"
                if task.status != TaskStatus.awaiting_preview:
                    return "close"
                if task.browser_decision in ("close", "expired", "closed", "preview_failed"):
                    return "close"
                if task.browser_decision == "save_requested":
                    if self.operator_save and task_service.consume_preview_save(
                        db, task, self.user_id, snapshot_token,
                    ):
                        # session_scope commits the one-shot marker before return.
                        return "save"
                    task_service.expire_preview(db, task)
                    return "close"
                if task.browser_decision:
                    # No other worker may consume an already-started write.
                    return "close"

        with session_scope() as db:
            task = db.get(Task, self.task_id)
            if task is not None:
                task_service.expire_preview(db, task)
        return "close"
