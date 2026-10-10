from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


CHECKOUT = Path(os.environ["PPV_CHECKOUT"])
SCRATCH = Path(os.environ["PPV_SCRATCH"])
SCENARIO = sys.argv[1]
CONTAINER = f"sro-ppv-role-{uuid.uuid4().hex[:12]}"
DOCKER = "/usr/local/bin/docker"


def run(argv: list[str], *, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {argv!r}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def sql(database: str, statement: str, *, user: str = "postgres", check: bool = True):
    return run(
        [
            DOCKER, "exec", CONTAINER, "psql", "-U", user, "-d", database,
            "-v", "ON_ERROR_STOP=1", "-At", "-c", statement,
        ],
        check=check,
    )


def cli(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=CHECKOUT,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )


def main() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    upload_dir = SCRATCH / "uploads"
    upload_dir.mkdir(mode=0o700, exist_ok=True)
    run([
        DOCKER, "run", "--rm", "-d", "--name", CONTAINER,
        "-p", "127.0.0.1::5432",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
        "postgres:16-alpine",
    ])
    try:
        for _ in range(45):
            ready = run(
                [DOCKER, "exec", CONTAINER, "pg_isready", "-U", "postgres"],
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("disposable PostgreSQL did not become ready")

        published = run([DOCKER, "port", CONTAINER, "5432/tcp"]).stdout.strip().splitlines()
        if len(published) != 1 or not published[0].startswith("127.0.0.1:"):
            raise RuntimeError(f"unexpected disposable PostgreSQL binding: {published!r}")
        host_port = published[0].rsplit(":", 1)[1]
        run([
            DOCKER, "exec", CONTAINER, "psql", "-U", "postgres", "-d", "postgres",
            "-v", "ON_ERROR_STOP=1",
            "-c", "CREATE ROLE ppv_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
            "-c", "CREATE ROLE ppv_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
            "-c", "GRANT CONNECT ON DATABASE postgres TO ppv_migrator, ppv_runtime",
        ])

        if SCENARIO == "wrong-target":
            sql("postgres", "CREATE DATABASE ppv_other")
            sql("postgres", "GRANT CONNECT ON DATABASE ppv_other TO ppv_migrator")
            sql("postgres", "ALTER SCHEMA public OWNER TO ppv_runtime")
            sql("ppv_other", "ALTER SCHEMA public OWNER TO ppv_migrator")
            sql("ppv_other", "GRANT USAGE, CREATE ON SCHEMA public TO ppv_migrator")
            runtime_db = "postgres"
            migration_db = "ppv_other"
        else:
            sql("postgres", "ALTER SCHEMA public OWNER TO ppv_migrator")
            sql("postgres", "GRANT USAGE, CREATE ON SCHEMA public TO ppv_migrator")
            sql("postgres", "GRANT USAGE ON SCHEMA public TO ppv_runtime")
            runtime_db = migration_db = "postgres"

        runtime_url = f"postgresql+psycopg://ppv_runtime@127.0.0.1:{host_port}/{runtime_db}"
        migration_url = f"postgresql+psycopg://ppv_migrator@127.0.0.1:{host_port}/{migration_db}"
        child_env = os.environ.copy()
        child_env.update({
            "APP_DEBUG": "0",
            "APP_SECRET_KEY": "synthetic-post-patch-validation-secret-32-chars",
            "DATABASE_URL": runtime_url,
            "MIGRATION_DATABASE_URL": migration_url,
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "SRO_UPLOAD_DIR": str(upload_dir),
        })
        if SCENARIO == "same-target":
            denied_ddl = sql(
                "postgres",
                "CREATE TABLE public.ppv_runtime_ddl_probe (id integer)",
                user="ppv_runtime",
                check=False,
            )
            if denied_ddl.returncode == 0:
                raise RuntimeError("runtime role unexpectedly had schema DDL before migration")
            migration_result = cli(child_env)
            print(f"runtime_ddl_denied=true exit={denied_ddl.returncode}")
            print(f"init_db_exit={migration_result.returncode}")
            print(migration_result.stdout, end="")
            print(migration_result.stderr, end="")
            if migration_result.returncode != 0:
                print(os.environ["PPV_REACHED_MARKER"], flush=True)
                assert migration_result.returncode == 0

            revision = sql(
                "postgres", "SELECT version_num FROM alembic_version", user="ppv_migrator"
            ).stdout.strip()
            owner = sql(
                "postgres",
                "SELECT tableowner FROM pg_tables WHERE schemaname='public' AND tablename='alembic_version'",
                user="ppv_migrator",
            ).stdout.strip()
            sql("postgres", "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ppv_runtime")
            sql("postgres", "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ppv_runtime")
            sql("postgres", "REVOKE INSERT, UPDATE, DELETE ON TABLE alembic_version FROM ppv_runtime")
            sql("postgres", "GRANT SELECT ON TABLE alembic_version TO ppv_runtime")
            schema_check = subprocess.run(
                [sys.executable, "-m", "app.cli", "schema-check"],
                cwd=CHECKOUT,
                env=child_env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            runtime_ddl_after = sql(
                "postgres",
                "CREATE TABLE public.ppv_runtime_ddl_probe (id integer)",
                user="ppv_runtime",
                check=False,
            )
            runtime_dml = sql(
                "postgres",
                "BEGIN; INSERT INTO public.user (login, full_name, password_hash, sro_login, is_active) "
                "VALUES ('ppv-only', 'Synthetic', 'synthetic', 'ppv-only', true); "
                "ROLLBACK;",
                user="ppv_runtime",
                check=False,
            )
            print(f"revision={revision} alembic_table_owner={owner}")
            print(f"runtime_schema_check_exit={schema_check.returncode}")
            print(schema_check.stdout, end="")
            print(schema_check.stderr, end="")
            print(f"runtime_ddl_after_migration_exit={runtime_ddl_after.returncode}")
            print(f"runtime_dml_exit={runtime_dml.returncode}")
            print(os.environ["PPV_REACHED_MARKER"], flush=True)
            assert (
                revision == "d84f3a210c6b"
                and owner == "ppv_migrator"
                and schema_check.returncode == 0
                and runtime_ddl_after.returncode != 0
                and runtime_dml.returncode == 0
            )
            return

        migration_result = cli(child_env)
        runtime_migrated = sql(
            "postgres", "SELECT to_regclass('public.alembic_version') IS NOT NULL"
        ).stdout.strip() == "t"
        alternate_migrated = sql(
            "ppv_other", "SELECT to_regclass('public.alembic_version') IS NOT NULL"
        ).stdout.strip() == "t"
        print(f"init_db_exit={migration_result.returncode}")
        print(f"runtime_database_migrated={runtime_migrated}")
        print(f"configured_migration_database_migrated={alternate_migrated}")
        print(migration_result.stdout, end="")
        print(migration_result.stderr, end="")
        print(os.environ["PPV_REACHED_MARKER"], flush=True)
        assert migration_result.returncode != 0 and not runtime_migrated and not alternate_migrated
    finally:
        run([DOCKER, "rm", "-f", CONTAINER], check=False)


if __name__ == "__main__":
    main()
