#!/usr/bin/env python3
"""Claim one synthetic task batch from an independently guarded worker process."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.base import SessionLocal
from app.services.leases import claim_ocr
from app.settings import settings

from lease_claim_load_safety import verify_disposable_postgres


def main() -> int:
    try:
        task_ids = json.loads(sys.stdin.read())
        if not isinstance(task_ids, list) or not task_ids or any(
            not isinstance(task_id, int) or task_id <= 0 for task_id in task_ids
        ):
            raise ValueError("invalid synthetic task batch")
        verify_disposable_postgres(settings.database_url)
    except (ValueError, TypeError, SystemExit) as exc:
        print(json.dumps({"guard_error": type(exc).__name__}))
        return 2

    latencies: list[float] = []
    failures: Counter[str] = Counter()
    for task_id in task_ids:
        started = time.perf_counter()
        try:
            with SessionLocal() as db:
                claim = claim_ocr(db, task_id)
                if claim is None:
                    raise RuntimeError("synthetic task did not receive a lease")
                db.commit()
            latencies.append(time.perf_counter() - started)
        except Exception as exc:
            # Parent performs authoritative read-back; keep output free of IDs,
            # exception messages and connection details.
            failures[f"{type(exc).__module__}.{type(exc).__name__}"] += 1
    print(json.dumps({"latencies": latencies, "failures": dict(failures)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
