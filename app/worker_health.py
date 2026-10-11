"""Shared read-only health predicates for RQ worker registrations."""

from __future__ import annotations

from datetime import datetime, timedelta

from rq.defaults import DEFAULT_WORKER_TTL


def has_fresh_worker_heartbeat(worker, checked_at: datetime) -> bool:
    """Return whether an RQ worker heartbeat is within its default lease TTL."""
    heartbeat = worker.last_heartbeat
    if not isinstance(heartbeat, datetime) or heartbeat.tzinfo is None:
        return False
    return checked_at - heartbeat <= timedelta(seconds=DEFAULT_WORKER_TTL)
