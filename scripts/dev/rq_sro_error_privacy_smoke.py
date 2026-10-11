#!/usr/bin/env python3
"""Prove unexpected SRO exceptions are sanitized before RQ stores them.

This smoke may write only to the explicitly identified disposable Redis used by
the restore drill. The task and failure are synthetic; no database or SRO access
is performed by the worker job.
"""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import logging
import os
from pathlib import Path
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from redis import Redis
from rq import Queue, SimpleWorker
from rq.job import Job
from rq.job import JobStatus
from rq.registry import FailedJobRegistry

from app.settings import settings
from app.workers import jobs
from scripts.dev.lease_claim_load_safety import verify_disposable_redis


class PrivacySmokeFailure(RuntimeError):
    """A fixed, non-sensitive diagnostic from this synthetic harness."""


def _run() -> None:
    verify_disposable_redis(settings.redis_url)
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=3)
    queue_name = f"sro-error-privacy-{uuid.uuid4().hex[:16]}"
    queue = Queue(queue_name, connection=client)
    task_id = 987654321
    sensitive_error = "postgresql://operator:synthetic-secret@db/private document text"
    task = SimpleNamespace(id=task_id, status="approved")
    db = MagicMock()
    db.get.return_value = task

    @contextmanager
    def fake_session_scope():
        yield db

    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.setLevel(logging.ERROR)
    jobs.logger.addHandler(handler)
    previous_level = jobs.logger.level
    jobs.logger.setLevel(logging.ERROR)
    try:
        with (
            patch.object(jobs, "session_scope", side_effect=lambda: fake_session_scope()),
            patch.object(jobs.runner, "run_task", side_effect=RuntimeError(sensitive_error)),
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            job = queue.enqueue(jobs.write_to_sro, task_id)
            worker = SimpleWorker(
                [queue], connection=client, name=f"synthetic-{uuid.uuid4().hex[:12]}"
            )
            if not worker.work(burst=True, max_jobs=1, with_scheduler=False):
                raise PrivacySmokeFailure("worker did not complete its bounded burst")
            job = Job.fetch(job.id, connection=client)
            failed_ids = FailedJobRegistry(queue_name, connection=client).get_job_ids(cleanup=False)

        if job.get_status(refresh=True) != JobStatus.FAILED or failed_ids != [job.id]:
            raise PrivacySmokeFailure("job was not retained as one failed RQ job")
        if queue.count != 0:
            raise PrivacySmokeFailure("failed SRO job was unexpectedly left queued for retry")
        if not isinstance(job.exc_info, str) or "read-only reconciliation before retry" not in job.exc_info:
            raise PrivacySmokeFailure("RQ did not persist the safe reconciliation marker")
        persisted = "\n".join((job.exc_info, captured.getvalue(), stdout.getvalue(), stderr.getvalue()))
        if sensitive_error in persisted or "synthetic-secret" in persisted or "private document text" in persisted:
            raise PrivacySmokeFailure("private exception details escaped into a log or RQ record")
        if "task_id=987654321 error_type=RuntimeError" not in captured.getvalue():
            raise PrivacySmokeFailure("safe task-correlated diagnostic was not recorded")
        if task.status != "approved":
            raise PrivacySmokeFailure("unexpected job failure changed the task outcome")
        print("synthetic_sro_failure_stored_in_failed_registry=true")
        print("sensitive_exception_absent_from_logs_and_rq_traceback=true")
        print("task_state_unchanged_and_no_retry_enqueued=true")
    finally:
        jobs.logger.removeHandler(handler)
        jobs.logger.setLevel(previous_level)
        client.close()


if __name__ == "__main__":
    if os.environ.get("SRO_DISPOSABLE_DRILL") != "1":
        raise SystemExit("refusing SRO worker privacy smoke without disposable-drill marker")
    if not os.environ.get("SRO_DRILL_REDIS_CONTAINER_NAME"):
        raise SystemExit("refusing SRO worker privacy smoke without disposable Redis identity")
    try:
        _run()
    except PrivacySmokeFailure as exc:
        print(f"SRO worker privacy smoke failed ({exc})", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"SRO worker privacy smoke failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(2)
