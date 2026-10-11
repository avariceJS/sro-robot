from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from sqlalchemy.exc import DBAPIError
from sqlalchemy import text

IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
EXPECTED_REVISION = "c4a781b6d902"


def run(*argv: str, capture: bool = False) -> str:
    completed = subprocess.run(
        argv,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
    )
    return completed.stdout.strip() if capture else ""


def main(mode: str) -> None:
    if mode not in {"behavior", "control", "exploit", "variant", "security"}:
        raise SystemExit("unsupported probe mode")

    container = f"sro-ppv-temp-shadow-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    run(
        "docker", "run", "--rm", "-d", "--name", container,
        "-p", "127.0.0.1::5432", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", IMAGE,
    )
    try:
        for attempt in range(30):
            ready = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-h", "127.0.0.1", "-p", "5432", "-U", "postgres"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if ready.returncode == 0:
                break
            if attempt == 29:
                raise RuntimeError("disposable PostgreSQL did not become ready")
            time.sleep(1)

        host_port = run("docker", "port", container, "5432/tcp", capture=True).rsplit(":", 1)[1]
        admin = ["docker", "exec", container, "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1"]
        run(*admin,
            "-c", "CREATE ROLE sro_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
            "-c", "GRANT CONNECT, TEMP ON DATABASE postgres TO sro_runtime",
            "-c", "CREATE TABLE public.alembic_version (version_num varchar(32) NOT NULL)",
            "-c", f"INSERT INTO public.alembic_version VALUES ('{EXPECTED_REVISION}')",
            "-c", "CREATE TABLE public.document (marker text NOT NULL)",
            "-c", "INSERT INTO public.document VALUES ('public-row')",
            "-c", "GRANT SELECT ON ALL TABLES IN SCHEMA public TO sro_runtime",
        )

        storage = Path(os.environ["PPV_SCRATCH"]) / "private-documents"
        storage.mkdir(mode=0o700)
        storage.chmod(0o700)
        url = f"postgresql+psycopg://sro_runtime@127.0.0.1:{host_port}/postgres"
        os.environ.update({
            "APP_DEBUG": "1",
            "DATABASE_URL": url,
            "MIGRATION_DATABASE_URL": url,
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "SRO_UPLOAD_DIR": str(storage),
        })

        from app.db.base import engine

        try:
            if mode in {"behavior", "control"}:
                with engine.connect() as connection:
                    schema = connection.execute(text("SELECT current_schema()")).scalar_one()
                    document = connection.execute(text("SELECT marker FROM document")).scalar_one()
                    revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                assert (schema, document, revision) == ("public", "public-row", EXPECTED_REVISION)
                print("schema=public;document=public-row;revision=c4a781b6d902", flush=True)
                return

            if mode in {"exploit", "variant"}:
                relation = "alembic_version" if mode == "exploit" else "document"
                column = "version_num" if mode == "exploit" else "marker"
                shadow = "shadowed-revision" if mode == "exploit" else "temporary-row"
                expected = EXPECTED_REVISION if mode == "exploit" else "public-row"
                with engine.begin() as connection:
                    connection.exec_driver_sql(
                        f"CREATE TEMP TABLE {relation} ({column} text NOT NULL)"
                    )
                    connection.execute(
                        text(f"INSERT INTO pg_temp.{relation} ({column}) VALUES (:value)"),
                        {"value": shadow},
                    )
                    observed = connection.execute(
                        text(f"SELECT {column} FROM {relation}")
                    ).scalar_one()
                    print("PPV_REACHED", flush=True)
                    assert observed == expected, f"temporary relation shadowed public {relation}"
                print(f"unqualified_{relation}=public", flush=True)
                return

            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    connection.exec_driver_sql(
                        "CREATE TABLE public.ppv_runtime_ddl_probe (id integer)"
                    )
                except DBAPIError:
                    transaction.rollback()
                else:
                    transaction.rollback()
                    raise AssertionError("runtime role unexpectedly created a public table")
            print("public_schema_ddl=denied", flush=True)
        finally:
            engine.dispose()
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main(sys.argv[1])
