from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


DOCKER = "/usr/local/bin/docker"
IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
POSTGRES_URL = "postgresql+psycopg://{}@127.0.0.1:{}/postgres"


def docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [DOCKER, *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def psql(container: str, role: str, sql: str) -> str:
    result = docker(
        "exec", container, "psql", "-U", role, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-At", "-c", sql
    )
    return result.stdout.strip()


def probe(mode: str) -> None:
    container = f"ppv-definer-{uuid.uuid4().hex[:12]}"
    docker(
        "run", "--rm", "-d", "--name", container, "-p", "127.0.0.1::5432",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", IMAGE,
    )
    try:
        ready_marker = b"database system is ready to accept connections"
        for _ in range(120):
            logs = docker("logs", container, check=False)
            combined_logs = (logs.stdout + logs.stderr).encode()
            if combined_logs.count(ready_marker) >= 2:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("disposable PostgreSQL did not finish initialization")

        host_port = docker("port", container, "5432/tcp").stdout.strip().rsplit(":", 1)[-1]
        docker(
            "exec", container, "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
            "-c", "CREATE ROLE sro_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
            "-c", "CREATE ROLE sro_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
            "-c", "GRANT CONNECT ON DATABASE postgres TO sro_migrator, sro_runtime",
            "-c", "ALTER SCHEMA public OWNER TO sro_migrator",
            "-c", "GRANT USAGE ON SCHEMA public TO sro_runtime",
            capture=False,
        )
        docker(
            "exec", container, "psql", "-U", "sro_migrator", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
            "-c", "CREATE TABLE public.protected_synthetic_row (marker text NOT NULL)",
            "-c", "INSERT INTO public.protected_synthetic_row VALUES ('preserve-me')",
            "-c", "CREATE FUNCTION public.synthetic_definer_attack() RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$ BEGIN DELETE FROM public.protected_synthetic_row; END; $$",
            capture=False,
        )
        if mode == "exploit-explicit":
            psql(container, "sro_migrator", "GRANT EXECUTE ON FUNCTION public.synthetic_definer_attack() TO sro_runtime")
        elif mode == "variant-public":
            # PostgreSQL's default PUBLIC EXECUTE grant is the distinct ACL path.
            pass
        elif mode in {"behavior", "security-ungranted"}:
            psql(container, "sro_migrator", "REVOKE EXECUTE ON FUNCTION public.synthetic_definer_attack() FROM PUBLIC, sro_runtime")
        elif mode == "control":
            psql(container, "sro_migrator", "DROP FUNCTION public.synthetic_definer_attack()")
        else:
            raise ValueError("unknown probe mode")

        psql(container, "postgres", "GRANT SELECT, INSERT, UPDATE, DELETE ON public.protected_synthetic_row TO sro_runtime")
        os.environ.update(
            {
                "APP_DEBUG": "0",
                "APP_SECRET_KEY": "synthetic-post-patch-validation-secret-0000000000",
                "DATABASE_URL": POSTGRES_URL.format("sro_runtime", host_port),
                "MIGRATION_DATABASE_URL": POSTGRES_URL.format("sro_migrator", host_port),
                "REDIS_URL": f"redis://127.0.0.1:{host_port}/0",
            }
        )
        with tempfile.TemporaryDirectory(prefix="ppv-upload-") as upload_dir:
            os.environ["SRO_UPLOAD_DIR"] = upload_dir
            from sqlalchemy import text
            from app.db.base import engine

            if mode in {"exploit-explicit", "variant-public"}:
                denied = False
                try:
                    with engine.begin() as connection:
                        connection.execute(text("SELECT public.synthetic_definer_attack()"))
                except RuntimeError as exc:
                    denied = "privilege ceiling" in str(exc)
                remaining = int(psql(container, "sro_migrator", "SELECT count(*) FROM public.protected_synthetic_row"))
                print("PPV_REACHED", flush=True)
                assert denied and remaining == 1, "executable SECURITY DEFINER path was not blocked before its protected mutation"
                print("SAFE_DENIAL_OK")
            elif mode in {"behavior", "security-ungranted", "control"}:
                with engine.begin() as connection:
                    before = connection.execute(text("SELECT count(*) FROM public.protected_synthetic_row")).scalar_one()
                    assert before == 1
                    if mode == "security-ungranted":
                        allowed = connection.execute(
                            text("SELECT has_function_privilege(current_user, 'public.synthetic_definer_attack()', 'EXECUTE')")
                        ).scalar_one()
                        assert allowed is False
                    if mode != "control":
                        connection.execute(text("INSERT INTO public.protected_synthetic_row VALUES ('ordinary-dml')"))
                        connection.execute(text("DELETE FROM public.protected_synthetic_row WHERE marker='ordinary-dml'"))
                    else:
                        connection.execute(text("SELECT 1")).scalar_one()
                print("DML_BEHAVIOR_OK" if mode != "control" else "CONTROL_OK")
            else:
                raise ValueError("unknown probe mode")
            engine.dispose()
    finally:
        docker("rm", "-f", container, check=False)


if __name__ == "__main__":
    probe(sys.argv[1])
