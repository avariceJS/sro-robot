from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import update
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql import operators, visitors

from app.db.models import Task, TaskStatus
from app.services import tasks


class SingleRowSession:
    """Small deterministic SQL UPDATE interpreter for the transition predicate."""

    def __init__(self, task) -> None:
        self.task = task

    def flush(self) -> None:
        pass

    def refresh(self, _task) -> None:
        pass

    def scalar(self, statement):
        assert isinstance(statement, Update)
        allowed_statuses = None
        for node in visitors.iterate(statement.whereclause):
            if (
                getattr(getattr(node, "left", None), "key", None) == "status"
                and getattr(node, "operator", None) is operators.in_op
            ):
                values = getattr(node.right, "value", None)
                if values is not None:
                    allowed_statuses = {
                        value if isinstance(value, TaskStatus) else TaskStatus(value)
                        for value in values
                    }

        # Model the effect of the SQL predicate against the single synthetic row.
        # Missing status restriction intentionally behaves like an unsafe UPDATE.
        if allowed_statuses is not None and self.task.status not in allowed_statuses:
            return None
        self.task.status = TaskStatus.failed
        self.task.error = "synthetic-private-error"
        return self.task.id


def run(state_name: str) -> None:
    status = TaskStatus(state_name)
    task = SimpleNamespace(id=17, status=status, error="")
    db = SingleRowSession(task)
    events: list[str] = []

    def record_event(_db, _task, kind, *_args, **_kwargs):
        events.append(kind)

    with patch.object(tasks, "log", side_effect=record_event):
        tasks.fail(db, task, "synthetic-private-error")

    print(f"status={task.status.value} failed_events={events.count('failed')}")
    if state_name in {"done", "awaiting_preview"}:
        print("PPV_REACHED", flush=True)
        assert task.status == status, f"stale failure changed {state_name} to {task.status.value}"
        assert events.count("failed") == 0, "stale failure emitted a failed event"
    else:
        assert task.status == TaskStatus.failed, "eligible local failure was not recorded"
        assert events.count("failed") == 1, "eligible local failure event was not recorded once"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("expected one TaskStatus name")
    run(sys.argv[1])
