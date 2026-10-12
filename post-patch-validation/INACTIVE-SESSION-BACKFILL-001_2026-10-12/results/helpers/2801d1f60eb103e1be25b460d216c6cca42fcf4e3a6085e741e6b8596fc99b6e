from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

IMAGE = (
    "postgres@sha256:"
    "721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
)
PYTHON = "/Users/root1/Desktop/sro-robot/.venv/bin/python"


def run(arguments: list[str], *, env=None, timeout=90) -> str:
    result = subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return result.stdout.strip()


def migration(env: dict[str, str], revision: str) -> None:
    source = (
        "from alembic import command; from alembic.config import Config; "
        "from sro_robot.config import ROOT; command.upgrade(Config(str(ROOT / 'alembic.ini')), "
        "REVISION)"
    )
    run([PYTHON, "-c", source.replace("REVISION", repr(revision))], env=env, timeout=120)


def database(revision: str) -> tuple[str, str, dict[str, str]]:
    container = f"sro-ppv-inactive-{uuid.uuid4().hex[:12]}"
    run(
        [
            "docker", "run", "--rm", "-d", "--name", container,
            "-p", "127.0.0.1::5432", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", IMAGE,
        ]
    )
    try:
        for _ in range(60):
            ready = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-h", "127.0.0.1", "-p", "5432", "-U", "postgres"],
                check=False, capture_output=True, text=True, timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("disposable PostgreSQL did not become ready")
        address = run(["docker", "port", container, "5432/tcp"])
        port = address.rsplit(":", 1)[1]
        url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"
        env = os.environ.copy()
        env.update(
            APP_DEBUG="1",
            DATABASE_URL=url,
            MIGRATION_DATABASE_URL=url,
            REDIS_URL="redis://synthetic:synthetic@127.0.0.1:1/0",
            PYTHONPATH=os.environ["PPV_CHECKOUT"],
        )
        migration(env, revision)
        return container, url, env
    except BaseException:
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)
        raise


def add_user_and_session(engine, *, active: bool, expires_at=None) -> tuple[int, str]:
    token = f"synthetic-inactive-session-{uuid.uuid4().hex}"
    digest = hashlib.sha256(token.encode()).hexdigest()
    with engine.begin() as connection:
        user_id = connection.execute(
            text(
                'INSERT INTO public."user" '
                '(login, full_name, password_hash, sro_login, is_active) '
                "VALUES (:login, 'Synthetic User', 'synthetic-only', 'synthetic', :active) "
                "RETURNING id"
            ),
            {"login": f"synthetic-ppv-{uuid.uuid4().hex}", "active": active},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO public.login_session "
                "(user_id, token_sha256, expires_at, revoked_at) "
                "VALUES (:user_id, :digest, :expires_at, NULL)"
            ),
            {
                "user_id": user_id,
                "digest": digest,
                "expires_at": expires_at or datetime.now(timezone.utc) + timedelta(days=1),
            },
        )
    return user_id, digest


def session_revoked(engine, digest: str) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT revoked_at IS NOT NULL FROM public.login_session "
                    "WHERE token_sha256 = :digest"
                ),
                {"digest": digest},
            ).scalar_one()
        )


def control() -> None:
    container, url, _env = database("head")
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        user_id, digest = add_user_and_session(engine, active=True)
        with engine.begin() as connection:
            connection.execute(
                text('UPDATE public."user" SET full_name=:name WHERE id=:id'),
                {"name": "Synthetic Updated User", "id": user_id},
            )
        assert not session_revoked(engine, digest)
        print("unrelated_account_update_preserves_session")
    finally:
        engine.dispose()
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)


def exploit() -> None:
    container, url, env = database("d92f41b7a6c3")
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        user_id, digest = add_user_and_session(engine, active=False)
        migration(env, "head")
        with engine.begin() as connection:
            connection.execute(
                text('UPDATE public."user" SET is_active=true WHERE id=:id'),
                {"id": user_id},
            )
        reached_safe_state = session_revoked(engine, digest)
        print("PPV_REACHED", flush=True)
        assert reached_safe_state, "stale pre-migration session survived reactivation"
        print("pre_migration_inactive_session_revoked=true")
    finally:
        engine.dispose()
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)


def variant() -> None:
    container, url, _env = database("head")
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        user_id, digest = add_user_and_session(engine, active=False)
        with engine.begin() as connection:
            connection.execute(
                text('UPDATE public."user" SET is_active=true WHERE id=:id'),
                {"id": user_id},
            )
        reached_safe_state = session_revoked(engine, digest)
        print("PPV_REACHED", flush=True)
        assert reached_safe_state, "reactivation revived a session inserted while account was inactive"
        print("inactive_state_session_revoked_on_reactivation=true")
    finally:
        engine.dispose()
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)


def regression() -> None:
    container, url, env = database("head")
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        user_id, digest = add_user_and_session(engine, active=True)
        with Session(engine) as session, session.begin():
            login = session.execute(
                text('SELECT login FROM public."user" WHERE id=:id'), {"id": user_id}
            ).scalar_one()
            from app.services import auth

            _user, revoked = auth.deactivate_user(session, login)
            assert revoked == 1
        assert session_revoked(engine, digest)
        print("supported_deactivation_count_and_revocation=preserved")
    finally:
        engine.dispose()
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)


def security() -> None:
    container, url, _env = database("head")
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        expired = datetime.now(timezone.utc) - timedelta(days=1)
        user_id, digest = add_user_and_session(engine, active=False, expires_at=expired)
        with engine.begin() as connection:
            connection.execute(
                text('UPDATE public."user" SET is_active=true WHERE id=:id'),
                {"id": user_id},
            )
        assert not session_revoked(engine, digest)
        print("expired_stale_session_remains_unchanged")
    finally:
        engine.dispose()
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)


def behavior() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.environ["PPV_CHECKOUT"]
    output = run(
        [
            PYTHON,
            "-c",
            "from app.services.auth import normalize_login; "
            "print('normalized_login=' + normalize_login('  Synthetic   Operator  '))",
        ],
        env=env,
        timeout=30,
    )
    assert output == "normalized_login=Synthetic Operator"
    print(output)


def suite() -> None:
    env = os.environ.copy()
    env.update(
        APP_DEBUG="1",
        DATABASE_URL="postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/synthetic",
        MIGRATION_DATABASE_URL="postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/synthetic",
        REDIS_URL="redis://synthetic:synthetic@127.0.0.1:1/0",
        PYTHONPATH=os.environ["PPV_CHECKOUT"],
    )
    run([PYTHON, "-m", "unittest", "tests.test_auth_session", "tests.test_schema_revision", "-q"], env=env, timeout=60)
    print("authentication and schema-head unit suite passed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: inactive_account_sessions.py MODE")
    modes = {
        "control": control,
        "exploit": exploit,
        "variant": variant,
        "regression": regression,
        "security": security,
        "behavior": behavior,
        "suite": suite,
    }
    modes[sys.argv[1]]()
