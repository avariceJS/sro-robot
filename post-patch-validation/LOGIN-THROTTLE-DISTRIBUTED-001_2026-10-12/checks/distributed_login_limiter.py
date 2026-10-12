from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from redis import Redis

from app.security import LoginAttemptLimiter
import app.security as security


REDIS_IMAGE = "redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"
TEST_KEY = "synthetic-ppv-login-limiter-key-000000000000000000"


def shared_limiter(redis_url: str, *, max_attempts: int = 10, max_buckets: int = 4096):
    limiter_type = getattr(security, "RedisLoginAttemptLimiter", None)
    if limiter_type is None:
        return LoginAttemptLimiter(max_attempts=max_attempts, max_buckets=max_buckets)
    return limiter_type(
        Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1),
        secret_key=TEST_KEY,
        max_attempts=max_attempts,
        max_buckets=max_buckets,
    )


def start_redis() -> tuple[str, str]:
    container = subprocess.run(
        [
            "docker", "run", "--rm", "-d", "-p", "127.0.0.1::6379",
            REDIS_IMAGE, "redis-server", "--save", "", "--appendonly", "no",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if container.returncode != 0 or not container.stdout.strip():
        raise RuntimeError("pinned disposable Redis did not start")
    container_id = container.stdout.strip()
    try:
        published = subprocess.run(
            ["docker", "port", container_id, "6379/tcp"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if published.returncode != 0:
            raise RuntimeError("disposable Redis port was not published")
        address = published.stdout.strip().splitlines()[0]
        port = address.rsplit(":", 1)[1]
        redis_url = f"redis://127.0.0.1:{port}/0"
        client = Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)
        for _ in range(30):
            try:
                client.ping()
                client.close()
                return container_id, redis_url
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("disposable Redis did not become ready")
    except BaseException:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        raise


def stop_redis(container_id: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def child_admission(redis_url: str, peer: str) -> None:
    limiter = shared_limiter(redis_url)
    token, retry_after = limiter.reserve(peer, "synthetic operator")
    print(json.dumps({"admitted": token is not None, "retry_after": retry_after}))


def exploit_process_restart_bypass() -> None:
    container_id, redis_url = start_redis()
    peer = f"synthetic-ppv-peer-{uuid.uuid4().hex}"
    try:
        limiter = shared_limiter(redis_url)
        for _ in range(10):
            token, retry_after = limiter.reserve(peer, "Synthetic Operator")
            assert token is not None and retry_after is None
            limiter.finish(peer, "synthetic operator", token, outcome="failed")
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--admission", redis_url, peer],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=os.environ.copy(),
        )
        assert child.returncode == 0, child.stderr
        observed = json.loads(child.stdout)
        print("PPV_REACHED", flush=True)
        assert observed["admitted"] is False
    finally:
        stop_redis(container_id)


def variant_identity_bucket_cap() -> None:
    container_id, redis_url = start_redis()
    try:
        limiter = shared_limiter(redis_url, max_buckets=2)
        for peer in ("synthetic-capacity-a", "synthetic-capacity-b"):
            token, retry_after = limiter.reserve(peer, "synthetic operator")
            assert token is not None and retry_after is None
            limiter.finish(peer, "synthetic operator", token, outcome="aborted")
        token, retry_after = limiter.reserve("synthetic-capacity-c", "synthetic operator")
        print("PPV_REACHED", flush=True)
        assert token is None and retry_after is not None
    finally:
        stop_redis(container_id)


def control_local_admission() -> None:
    limiter = LoginAttemptLimiter()
    token, retry_after = limiter.reserve("synthetic-peer", "synthetic operator")
    admitted = token is not None and retry_after is None
    if token is not None:
        limiter.finish("synthetic-peer", "synthetic operator", token, outcome="aborted")
    print(f"admitted={admitted}")
    assert admitted is True


def behavior_success_reset() -> None:
    limiter = LoginAttemptLimiter(max_attempts=2, window_seconds=60)
    for _ in range(2):
        token, _retry = limiter.reserve("synthetic-peer", "synthetic operator")
        limiter.finish("synthetic-peer", "synthetic operator", token, outcome="failed")
    blocked, _retry = limiter.reserve("synthetic-peer", "synthetic operator")
    assert blocked is None
    token, _retry = limiter.reserve("other-synthetic-peer", "synthetic operator")
    limiter.finish("other-synthetic-peer", "synthetic operator", token, outcome="success")
    after_success, retry = limiter.reserve("other-synthetic-peer", "synthetic operator")
    print(f"blocked_at_limit={blocked is None};success_reset={after_success is not None}")
    assert after_success is not None and retry is None


def privacy_hmac_identity() -> None:
    peer = "synthetic-private-peer"
    login = "synthetic-private-login"
    limiter = LoginAttemptLimiter()
    token, _retry = limiter.reserve(peer, login)
    rendered_state = repr(limiter._attempts)
    safe = peer not in rendered_state and login not in rendered_state
    if token is not None:
        limiter.finish(peer, login, token, outcome="aborted")
    print(f"local_identity_hmac_only={safe}")
    assert safe is True


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--admission":
        child_admission(sys.argv[2], sys.argv[3])
    elif sys.argv[1:] == ["control"]:
        control_local_admission()
    elif sys.argv[1:] == ["exploit"]:
        exploit_process_restart_bypass()
    elif sys.argv[1:] == ["variant"]:
        variant_identity_bucket_cap()
    elif sys.argv[1:] == ["behavior"]:
        behavior_success_reset()
    elif sys.argv[1:] == ["security"]:
        privacy_hmac_identity()
    else:
        raise SystemExit("unsupported validation mode")
