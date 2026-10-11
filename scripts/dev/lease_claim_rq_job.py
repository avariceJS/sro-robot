"""Synthetic RQ job that exercises the production OCR lease boundary."""

from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.base import session_scope
from app.db.models import Task
from app.services.leases import claim_ocr, complete_ocr
from app.settings import settings
from redis import Redis
from sro_robot.core.models import DebtorCard

from lease_claim_load_safety import verify_disposable_postgres, verify_disposable_redis


def recognize_synthetic(
    task_id: int,
    marker: str,
    crash_after_claim: bool = False,
) -> None:
    """Claim and complete one synthetic task without OCR or external I/O."""
    if not settings.ocr_task_leases_enabled:
        raise RuntimeError("synthetic RQ lease job requires OCR lease mode")
    verify_disposable_postgres(settings.database_url)
    verify_disposable_redis(settings.redis_url)

    with session_scope() as db:
        task = db.get(Task, task_id)
        if task is None or task.values.get("rq_worker_smoke") != marker:
            raise RuntimeError("synthetic RQ task identity check failed")
        claim = claim_ocr(db, task_id)
        if claim is None:
            return
        db.commit()

    if crash_after_claim:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=3,
        )
        try:
            client.set(f"sro:lease-smoke:claimed:{marker}", "1", ex=60)
        finally:
            client.close()
        os._exit(73)

    card = DebtorCard()
    card.raw_text = f"Synthetic RQ worker result {marker}"
    with session_scope() as db:
        if not complete_ocr(db, claim, card):
            raise RuntimeError("synthetic RQ lease completion was fenced")
        db.commit()
