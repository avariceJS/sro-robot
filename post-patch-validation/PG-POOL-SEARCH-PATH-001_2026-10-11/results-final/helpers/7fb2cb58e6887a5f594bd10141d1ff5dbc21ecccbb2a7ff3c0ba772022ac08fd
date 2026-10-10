from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


mode = sys.argv[1]
checkout = Path(os.environ["PPV_CHECKOUT"])
scratch = Path(os.environ["PPV_SCRATCH"])
upload_dir = scratch / "documents"
upload_dir.mkdir(mode=0o700)
container = f"sro-ppv-pool-path-{uuid.uuid4().hex[:12]}"


def run(argv: list[str], *, env: dict[str, str] | None = None, check: bool = True):
    return subprocess.run(argv, check=check, capture_output=True, text=True, env=env)


run([
    "docker", "run", "--rm", "-d", "--name", container,
    "-p", "127.0.0.1::5432",
    "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
    "postgres:16-alpine",
])
try:
    for _ in range(30):
        ready = run(["docker", "exec", container, "pg_isready", "-U", "postgres"], check=False)
        if ready.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("disposable PostgreSQL did not become ready")

    published = run(["docker", "port", container, "5432/tcp"]).stdout.strip()
    if not published.startswith("127.0.0.1:"):
        raise RuntimeError("disposable PostgreSQL did not bind to loopback")
    port = published.rsplit(":", 1)[1]
    run([
        "docker", "exec", container, "psql", "-U", "postgres", "-d", "postgres",
        "-v", "ON_ERROR_STOP=1",
        "-c", "CREATE ROLE runtime_probe LOGIN",
        "-c", "CREATE ROLE migration_probe LOGIN",
        "-c", "CREATE SCHEMA runtime_probe AUTHORIZATION runtime_probe",
        "-c", "CREATE SCHEMA migration_probe AUTHORIZATION migration_probe",
        "-c", "CREATE TABLE runtime_probe.user (id integer)",
        "-c", "CREATE TABLE migration_probe.alembic_version (version_num varchar(32))",
    ])

    runtime_url = f"postgresql+psycopg://runtime_probe@127.0.0.1:{port}/postgres"
    migration_url = f"postgresql+psycopg://migration_probe@127.0.0.1:{port}/postgres"
    target_engine = "engine" if mode in ("runtime", "control", "behavior") else "migration_engine"
    target_schema = "runtime_probe" if mode == "runtime" else "migration_probe"
    mutate = mode in ("runtime", "migration")
    first_query = "SELECT 1" if mode == "behavior" else "SELECT current_schema()"
    second_query = first_query
    second_checkout = mode != "control"
    expected = "1" if mode == "behavior" else "public"
    success_label = "query" if mode == "behavior" else "pooled_path"
    code = f"""
from app.db.base import engine, migration_engine
from sqlalchemy import text

target = {target_engine}
with target.connect() as connection:
    first_schema = connection.execute(text({first_query!r})).scalar_one()
    {"connection.exec_driver_sql('SET SESSION search_path TO " + target_schema + ", public')" if mutate else "pass"}
    {"connection.commit()" if mutate else "pass"}
if {second_checkout!r}:
    with target.connect() as connection:
        second_schema = connection.execute(text({second_query!r})).scalar_one()
else:
    second_schema = first_schema
engine.dispose()
migration_engine.dispose()
print(f'first_checkout={{first_schema}} second_checkout={{second_schema}}', flush=True)
{"print('PPV_REACHED', flush=True)" if mutate else ""}
assert str(first_schema) == {expected!r} and str(second_schema) == {expected!r}, 'pooled connection resolved a role-specific shadow schema or changed normal query behavior'
print('{mode}_{success_label}={expected}')
"""
    environment = {
        "DATABASE_URL": runtime_url,
        "MIGRATION_DATABASE_URL": migration_url,
        "APP_DEBUG": "1",
        "SRO_UPLOAD_DIR": str(upload_dir),
        "PYTHONPATH": str(checkout),
    }
    result = run([
        "/Users/root1/Desktop/sro-robot/.venv/bin/python", "-c", code,
    ], env=environment, check=False)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)
finally:
    run(["docker", "stop", container], check=False)
