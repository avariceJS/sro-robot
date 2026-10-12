from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

POSTGRES_IMAGE = (
    "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
)
DOCKER = "/usr/local/bin/docker"
PYTHON = "/Users/root1/Desktop/sro-robot/.venv/bin/python"


def run_suite(mode: str, checkout: Path) -> int:
    environment = os.environ.copy()
    environment.pop("MIGRATION_DATABASE_URL", None)
    environment["PATH"] = os.pathsep.join(
        ["/usr/local/bin", "/bin", "/usr/bin", environment.get("PATH", "")]
    )
    environment.update(
        {
            "APP_DEBUG": "1",
            "DATABASE_URL": "postgresql+psycopg://synthetic@127.0.0.1:1/synthetic",
            "MIGRATION_DATABASE_URL": "postgresql+psycopg://synthetic@127.0.0.1:1/synthetic",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "PYTHONPATH": str(checkout),
            "SRO_ROBOT_PYTHON": PYTHON,
            "SRO_DRILL_TASKS": "10000",
        }
    )
    if mode == "regression":
        argv = [PYTHON, "-m", "unittest", "tests.test_auth_session"]
    else:
        argv = [PYTHON, "-m", "unittest", "discover", "-s", "tests"]
    result = subprocess.run(argv, cwd=checkout, env=environment, check=False)
    return result.returncode


def with_database(mode: str, checkout: Path) -> int:
    container = f"sro-ppv-session-{uuid.uuid4().hex[:12]}"
    upload_root = tempfile.TemporaryDirectory(prefix="sro-ppv-session-upload-")
    host_port = None
    try:
        subprocess.run(
            [
                DOCKER,
                "run",
                "--rm",
                "-d",
                "--name",
                container,
                "-p",
                "127.0.0.1::5432",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                POSTGRES_IMAGE,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        for _ in range(60):
            ready = subprocess.run(
                [DOCKER, "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("disposable PostgreSQL did not become ready")

        port_result = subprocess.run(
            [DOCKER, "port", container, "5432/tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        host_port = port_result.stdout.strip().rsplit(":", 1)[1]
        database_url = f"postgresql+psycopg://postgres@127.0.0.1:{host_port}/postgres"
        os.environ.update(
            {
                "APP_DEBUG": "1",
                "DATABASE_URL": database_url,
                "MIGRATION_DATABASE_URL": database_url,
                "REDIS_URL": "redis://127.0.0.1:1/0",
                "SRO_UPLOAD_DIR": upload_root.name,
            }
        )
        sys.path.insert(0, str(checkout))
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import create_engine, text

        command.upgrade(Config(str(checkout / "alembic.ini")), "head")
        engine = create_engine(database_url, connect_args={"connect_timeout": 5})
        try:
            with engine.begin() as connection:
                user_id = connection.execute(
                    text(
                        'INSERT INTO public."user" '
                        '(login, full_name, password_hash, sro_login, is_active) '
                        "VALUES (:login, 'Synthetic PPV User', 'synthetic-only', "
                        "'synthetic-ppv', :active) RETURNING id"
                    ),
                    {
                        "login": f"synthetic-ppv-{uuid.uuid4().hex}",
                        "active": mode not in {"inactive_insert"},
                    },
                ).scalar_one()

            if mode == "inactive_insert":
                rejected = False
                try:
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO public.login_session "
                                "(user_id, token_sha256, expires_at) VALUES "
                                "(:user_id, :digest, clock_timestamp() + interval '1 hour')"
                            ),
                            {"user_id": user_id, "digest": "a" * 64},
                        )
                except Exception as exc:
                    rejected = getattr(getattr(exc, "orig", None), "sqlstate", None) == "23514"
                print("PPV_REACHED", flush=True)
                assert rejected, "direct session insert for inactive account must be rejected"
                print("inactive_account_session_insert=rejected")
                return 0

            if mode == "revocation_reset":
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO public.login_session "
                            "(user_id, token_sha256, expires_at) VALUES "
                            "(:user_id, :digest, clock_timestamp() + interval '1 hour')"
                        ),
                        {"user_id": user_id, "digest": "b" * 64},
                    )
                    connection.execute(
                        text(
                            "UPDATE public.login_session SET revoked_at=clock_timestamp() "
                            "WHERE token_sha256=:digest"
                        ),
                        {"digest": "b" * 64},
                    )
                reset_rejected = False
                try:
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "UPDATE public.login_session SET revoked_at=NULL "
                                "WHERE token_sha256=:digest"
                            ),
                            {"digest": "b" * 64},
                        )
                except Exception as exc:
                    reset_rejected = getattr(getattr(exc, "orig", None), "sqlstate", None) == "23514"
                print("PPV_REACHED", flush=True)
                assert reset_rejected, "revoked session must not be restored to active"
                print("revocation_reset=rejected")
                return 0

            if mode == "unknown_user":
                rejected = False
                try:
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO public.login_session "
                                "(user_id, token_sha256, expires_at) VALUES "
                                "(-987654321, :digest, clock_timestamp() + interval '1 hour')"
                            ),
                            {"digest": "c" * 64},
                        )
                except Exception:
                    rejected = True
                assert rejected, "login session with no account reference must be rejected"
                print("unknown_user_session_insert=rejected")
                return 0

            if mode in {"active_control", "active_behavior"}:
                from app.db.base import session_scope
                from app.db.models import LoginSession
                from app.services import auth

                token = "synthetic-supported-session-token"
                with session_scope() as db:
                    auth.new_login_session(db, user_id, token)
                with session_scope() as db:
                    active_before = auth.login_session_is_active(db, user_id, token)
                    if mode == "active_behavior":
                        revoked = auth.revoke_login_session(db, user_id, token)
                    else:
                        revoked = False
                if mode == "active_control":
                    assert active_before
                    print("active_account_session=issued_and_readable")
                else:
                    with session_scope() as db:
                        active_after = auth.login_session_is_active(db, user_id, token)
                    assert active_before and revoked and not active_after
                    print("supported_issue_revoke=preserved")
                return 0

            raise ValueError(f"unsupported database check mode: {mode}")
        finally:
            engine.dispose()
    finally:
        if host_port is not None:
            subprocess.run(
                [DOCKER, "rm", "-f", container],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        upload_root.cleanup()


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: session_write_guard.py MODE")
    mode = sys.argv[1]
    checkout = Path(os.environ["PPV_CHECKOUT"]).resolve()
    if mode in {"regression", "suite"}:
        return run_suite(mode, checkout)
    return with_database(mode, checkout)


if __name__ == "__main__":
    raise SystemExit(main())
