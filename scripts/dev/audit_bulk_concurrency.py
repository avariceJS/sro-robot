#!/usr/bin/env python3
"""Read-only audit of the retained synthetic 20-employee upload database."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.db.base import SessionLocal
from app.db.models import Event, Task
from app.settings import settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    env_path = Path(os.environ.get("SRO_ROBOT_ENV_FILE", ""))
    db_url = make_url(settings.database_url)
    if (
        not str(env_path)
        or env_path.is_symlink()
        or not env_path.is_file()
        or env_path.stat().st_size != 0
        or stat.S_IMODE(env_path.stat().st_mode) & 0o077
        or db_url.host not in {"127.0.0.1", "localhost", "::1"}
        or not db_url.database
        or "benchmark" not in db_url.database.casefold()
    ):
        raise SystemExit("Refusing audit unless using an empty private env file and loopback benchmark database")

    with SessionLocal() as db:
        created = db.scalars(select(Event).where(Event.kind == "created")).all()
        task_ids = [event.task_id for event in created]
        tasks = db.scalars(select(Task).where(Task.id.in_(task_ids))).all() if task_ids else []
        task_by_id = {task.id: task for task in tasks}
        distinct_actors = len({event.user_id for event in created if event.user_id is not None})
        actor_assignee_mismatches = sum(
            event.user_id is None
            or event.task_id not in task_by_id
            or event.user_id != task_by_id[event.task_id].assignee_id
            for event in created
        )
        invalid_request_ids = sum(
            not isinstance((event.payload or {}).get("request_id"), str)
            or re.fullmatch(r"[0-9a-f]{32}", (event.payload or {}).get("request_id", "")) is None
            for event in created
        )
        total_tasks = int(db.scalar(select(func.count()).select_from(Task)) or 0)

    result = {
        "database_name": db_url.database,
        "created_events": len(created),
        "distinct_actors": distinct_actors,
        "actor_assignee_mismatches": actor_assignee_mismatches,
        "invalid_or_missing_32hex_request_ids": invalid_request_ids,
        "total_tasks": total_tasks,
        "expected": {"created_events": 40, "distinct_actors": 20, "total_tasks": 40},
        "result": "pass" if (
            len(created) == 40 and distinct_actors == 20 and actor_assignee_mismatches == 0
            and invalid_request_ids == 0 and total_tasks == 40
        ) else "findings",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
