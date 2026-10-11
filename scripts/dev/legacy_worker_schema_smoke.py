"""Exercise the feature-disabled OCR handler against a pre-lease schema."""

from __future__ import annotations

import hashlib
import sys
from unittest.mock import patch

from sqlalchemy import select, text

from app.db.base import session_scope
from app.db.models import Document, Event, IntakeSource, Task, TaskStatus
from app.workers import jobs
from sro_robot.core.models import DebtorCard

MARKER = "synthetic-legacy-worker-schema-smoke"
ROLLING_MARKER = "synthetic-rolling-worker-schema-smoke"


class SyntheticProfile:
    def parse(self, path: str) -> DebtorCard:
        return DebtorCard()


def run_legacy_handler() -> None:
    with session_scope() as db:
        document = Document(
            filename="synthetic-legacy-schema.pdf",
            stored_path="/synthetic/not-read.pdf",
            content_hash=hashlib.sha256(MARKER.encode()).hexdigest(),
            source=IntakeSource.upload,
        )
        db.add(document)
        db.flush()
        task = Task(
            document_id=document.id,
            scenario="person",
            profile="default",
            status=TaskStatus.new,
            recognized={},
            values={"rolling_compatibility": MARKER},
            case_number="",
        )
        db.add(task)
        db.flush()
        task_id = task.id

    with patch.object(jobs, "get_profile", return_value=SyntheticProfile()):
        jobs.recognize(task_id)

    verify_upgraded_state()
    print("legacy_worker_schema_smoke=passed")


def verify_upgraded_state() -> None:
    with session_scope() as db:
        task = db.scalar(
            select(Task).where(Task.values["rolling_compatibility"].astext == MARKER)
        )
        if task is None or task.status != TaskStatus.needs_review:
            raise RuntimeError("Synthetic legacy OCR result was not preserved")
        recognized = db.scalar(
            select(Event.id).where(Event.task_id == task.id, Event.kind == "recognized")
        )
        if recognized is None:
            raise RuntimeError("Synthetic legacy OCR audit event was not preserved")


def serve_across_upgrade() -> None:
    # Load the worker code and establish a pooled connection against the old schema.
    with session_scope() as db:
        db.scalar(select(Task.id).order_by(Task.id).limit(1))
        warmed_backend_pid = db.scalar(text("SELECT pg_backend_pid()"))
    print("rolling_worker_ready", flush=True)
    if sys.stdin.readline().strip() != "recognize":
        raise SystemExit("Rolling worker did not receive its synthetic work command")

    with session_scope() as db:
        document = Document(
            filename="synthetic-rolling-worker-schema.pdf",
            stored_path="/synthetic/not-read.pdf",
            content_hash=hashlib.sha256(ROLLING_MARKER.encode()).hexdigest(),
            source=IntakeSource.upload,
        )
        db.add(document)
        db.flush()
        task = Task(
            document_id=document.id,
            scenario="person",
            profile="default",
            status=TaskStatus.new,
            recognized={},
            values={"rolling_compatibility": ROLLING_MARKER},
            case_number="",
        )
        db.add(task)
        db.flush()
        task_id = task.id

    with patch.object(jobs, "get_profile", return_value=SyntheticProfile()):
        jobs.recognize(task_id)
    with session_scope() as db:
        current_backend_pid = db.scalar(text("SELECT pg_backend_pid()"))
    if current_backend_pid != warmed_backend_pid:
        raise RuntimeError("Long-lived worker did not reuse its pre-upgrade PostgreSQL connection")
    print("rolling_worker_recognition_completed", flush=True)


def verify_rolling_worker_state() -> None:
    with session_scope() as db:
        task = db.scalar(
            select(Task).where(Task.values["rolling_compatibility"].astext == ROLLING_MARKER)
        )
        if task is None or task.status != TaskStatus.needs_review:
            raise RuntimeError("Long-lived legacy worker result was not preserved")
        recognized = db.scalar(
            select(Event.id).where(Event.task_id == task.id, Event.kind == "recognized")
        )
        if recognized is None:
            raise RuntimeError("Long-lived legacy worker audit event was not preserved")


if __name__ == "__main__":
    if sys.argv[1:] == ["run"]:
        run_legacy_handler()
    elif sys.argv[1:] == ["verify"]:
        verify_upgraded_state()
        print("upgraded_process_readback=passed")
    elif sys.argv[1:] == ["serve-across-upgrade"]:
        serve_across_upgrade()
    elif sys.argv[1:] == ["verify-rolling"]:
        verify_rolling_worker_state()
        print("rolling_worker_fresh_process_readback=passed")
    else:
        raise SystemExit(
            "Usage: legacy_worker_schema_smoke.py run|verify|serve-across-upgrade|verify-rolling"
        )
