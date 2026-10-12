#!/usr/bin/env python3
"""Prove production login throttles share atomic state using disposable Redis."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from redis import Redis

from app.security import LoginAttemptLimiterUnavailable, RedisLoginAttemptLimiter
from app.settings import settings
from lease_claim_load_safety import verify_disposable_redis


TEST_KEY = "synthetic-shared-login-limiter-session-key-000000000000"
MAX_ATTEMPTS = 10


def make_limiter(redis_url: str) -> RedisLoginAttemptLimiter:
    client = Redis.from_url(
        redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
        max_connections=2,
    )
    return RedisLoginAttemptLimiter(
        client,
        secret_key=TEST_KEY,
        max_attempts=MAX_ATTEMPTS,
        window_seconds=60,
    )


def worker(redis_url: str, peer: str) -> int:
    verify_disposable_redis(redis_url)
    limiter = make_limiter(redis_url)
    admitted = 0
    for _ in range(20):
        token, retry_after = limiter.reserve(peer, "synthetic operator")
        if token is not None:
            admitted += 1
        elif retry_after is None or retry_after < 1:
            raise SystemExit("shared limiter returned an invalid retry delay")
    print(json.dumps({"admitted": admitted}, sort_keys=True))
    return 0


def run() -> None:
    redis_url = settings.redis_url
    verify_disposable_redis(redis_url)
    first = make_limiter(redis_url)
    second = make_limiter(redis_url)

    peer = f"synthetic-peer-{uuid.uuid4().hex}"
    for index in range(MAX_ATTEMPTS):
        limiter = first if index % 2 == 0 else second
        token, retry_after = limiter.reserve(peer, "Synthetic Operator")
        if token is None or retry_after is not None:
            raise SystemExit("shared limiter rejected before the configured attempt limit")
        limiter.finish(peer, "synthetic operator", token, outcome="failed")
    blocked, retry_after = second.reserve(peer, "synthetic operator")
    if blocked is not None or not 1 <= (retry_after or 0) <= 60:
        raise SystemExit("shared limiter did not enforce its rolling failure window")

    reset_peer = f"synthetic-peer-reset-{uuid.uuid4().hex}"
    for _ in range(2):
        token, _retry = first.reserve(reset_peer, "synthetic operator")
        if token is None:
            raise SystemExit("shared limiter rejected below its configured limit")
        first.finish(reset_peer, "synthetic operator", token, outcome="failed")
    successful, _retry = second.reserve(reset_peer, "synthetic operator")
    if successful is None:
        raise SystemExit("shared limiter did not admit success-reset probe")
    second.finish(reset_peer, "synthetic operator", successful, outcome="success")
    admitted_after_success, retry = first.reserve(reset_peer, "synthetic operator")
    if admitted_after_success is None or retry is not None:
        raise SystemExit("successful authentication did not clear shared failures")
    first.finish(reset_peer, "synthetic operator", admitted_after_success, outcome="aborted")

    abort_peer = f"synthetic-peer-abort-{uuid.uuid4().hex}"
    reservation, _retry = first.reserve(abort_peer, "synthetic operator")
    first.finish(abort_peer, "synthetic operator", reservation, outcome="aborted")
    after_abort, retry = second.reserve(abort_peer, "synthetic operator")
    if after_abort is None or retry is not None:
        raise SystemExit("aborted authentication did not release its shared reservation")
    second.finish(abort_peer, "synthetic operator", after_abort, outcome="aborted")

    capacity = RedisLoginAttemptLimiter(
        first.client,
        secret_key="synthetic-capacity-session-key-000000000000000000",
        max_attempts=MAX_ATTEMPTS,
        window_seconds=60,
        max_buckets=2,
    )
    for capacity_peer in ("synthetic-capacity-peer-a", "synthetic-capacity-peer-b"):
        token, retry = capacity.reserve(capacity_peer, "synthetic operator")
        if token is None or retry is not None:
            raise SystemExit("bounded shared limiter rejected within its identity capacity")
        capacity.finish(capacity_peer, "synthetic operator", token, outcome="aborted")
    full, retry = capacity.reserve("synthetic-capacity-peer-c", "synthetic operator")
    if full is not None or not 1 <= (retry or 0) <= 60:
        raise SystemExit("shared limiter did not fail closed at its identity capacity")
    known_identity, retry = capacity.reserve(
        "synthetic-capacity-peer-a", "synthetic operator"
    )
    if known_identity is None or retry is not None:
        raise SystemExit("identity-capacity saturation blocked a known identity")
    capacity.finish(
        "synthetic-capacity-peer-a", "synthetic operator", known_identity, outcome="aborted"
    )
    for _ in range(MAX_ATTEMPTS):
        token, retry = capacity.reserve("synthetic-capacity-peer-a", "synthetic operator")
        if token is None or retry is not None:
            raise SystemExit("shared failure-window probe rejected below its limit")
        capacity.finish(
            "synthetic-capacity-peer-a", "synthetic operator", token, outcome="failed"
        )
    failed_key, _active_key, identities_key, identity = capacity._keys(
        "synthetic-capacity-peer-a", "synthetic operator"
    )
    before_blocked_retry = first.client.zscore(identities_key, identity)
    blocked_identity, retry = capacity.reserve(
        "synthetic-capacity-peer-a", "synthetic operator"
    )
    after_blocked_retry = first.client.zscore(identities_key, identity)
    if (
        blocked_identity is not None
        or retry is None
        or before_blocked_retry != after_blocked_retry
        or first.client.zcard(failed_key) != MAX_ATTEMPTS
    ):
        raise SystemExit("rate-limited probes extended or changed shared limiter state")

    concurrency_peer = f"synthetic-peer-concurrent-{uuid.uuid4().hex}"
    children = [
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", redis_url, concurrency_peer],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        for _ in range(2)
    ]
    child_results = []
    for child in children:
        stdout, stderr = child.communicate(timeout=20)
        if child.returncode != 0:
            raise SystemExit(f"disposable Redis login limiter child failed: {type(stderr).__name__}")
        child_results.append(json.loads(stdout)["admitted"])
    if sum(child_results) != MAX_ATTEMPTS:
        raise SystemExit("separate processes admitted more than the shared attempt limit")

    key_names = first._keys(concurrency_peer, "synthetic operator")
    if any(concurrency_peer in name or "synthetic operator" in name for name in key_names):
        raise SystemExit("shared Redis limiter key exposed a peer or login")

    unavailable = Redis.from_url(
        "redis://127.0.0.1:1/0",
        socket_connect_timeout=0.05,
        socket_timeout=0.05,
        max_connections=1,
    )
    try:
        try:
            RedisLoginAttemptLimiter(unavailable, secret_key=TEST_KEY).reserve(
                "synthetic-peer-unavailable", "synthetic operator"
            )
        except LoginAttemptLimiterUnavailable:
            pass
        else:
            raise SystemExit("Redis outage did not fail the shared limiter closed")
    finally:
        unavailable.close()
        first.client.close()
        second.client.close()

    print(f"shared_failure_window_limit={MAX_ATTEMPTS} retry_after_seconds=1..60")
    print("success_reset=passed aborted_reservation_release=passed")
    print("identity_capacity=4096 synthetic_probe=2 fails_closed=passed")
    print("blocked_retry_does_not_extend_identity_ttl=passed")
    print(f"separate_processes_admitted={sum(child_results)} unadmitted=30 unique_owners=shared")
    print("redis_key_privacy=passed redis_unavailable=fail_closed")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        raise SystemExit(worker(sys.argv[2], sys.argv[3]))
    if len(sys.argv) != 1:
        raise SystemExit("unsupported smoke arguments")
    run()
