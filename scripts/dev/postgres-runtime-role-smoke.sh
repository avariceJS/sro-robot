#!/usr/bin/env bash

# Probe a proposed split between a PostgreSQL migration owner and DML-only app role.
# All schema/data changes are confined to one ephemeral PostgreSQL 16 container.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
container_name="sro-role-smoke-$$"
python_bin="${SRO_ROBOT_PYTHON:-$repo_root/.venv/bin/python}"
upload_dir=""

if [[ ! -x "$python_bin" ]]; then
  echo "Python executable not found: $python_bin" >&2
  exit 2
fi

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  if [[ -n "$upload_dir" && "$upload_dir" == /tmp/sro-runtime-role.* ]]; then
    if [[ -L "$upload_dir" ]]; then
      rm -f -- "$upload_dir"
    elif [[ -d "$upload_dir" ]]; then
      rm -rf -- "$upload_dir"
    fi
  fi
}
trap cleanup EXIT INT TERM

upload_dir="$(mktemp -d /tmp/sro-runtime-role.XXXXXX)"
docker run --rm -d \
  --name "$container_name" \
  -p 127.0.0.1::5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  postgres:16-alpine >/dev/null

for attempt in {1..30}; do
  if docker exec "$container_name" pg_isready -U postgres >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" == 30 ]]; then
    echo "Disposable PostgreSQL did not become ready" >&2
    exit 1
  fi
  sleep 1
done

image="$(docker inspect --format '{{.Config.Image}}' "$container_name")"
published_address="$(docker port "$container_name" 5432/tcp)"
if [[ "$image" != "postgres:16-alpine" || "$published_address" != 127.0.0.1:* ]]; then
  echo "Refusing role probe without the expected loopback PostgreSQL 16 container" >&2
  exit 2
fi
host_port="${published_address##*:}"
migrator_url="postgresql+psycopg://sro_migrator@127.0.0.1:${host_port}/postgres"
runtime_url="postgresql+psycopg://sro_runtime@127.0.0.1:${host_port}/postgres"

docker exec "$container_name" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'CREATE ROLE sro_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'GRANT CONNECT ON DATABASE postgres TO sro_migrator, sro_runtime' \
  -c 'ALTER SCHEMA public OWNER TO sro_migrator' \
  -c 'GRANT USAGE, CREATE ON SCHEMA public TO sro_migrator' \
  -c 'GRANT USAGE ON SCHEMA public TO sro_runtime' \
  -c 'ALTER DEFAULT PRIVILEGES FOR ROLE sro_migrator IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sro_runtime' \
  -c 'ALTER DEFAULT PRIVILEGES FOR ROLE sro_migrator IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO sro_runtime' \
  >/dev/null

# Existing Settings routes Alembic through DATABASE_URL. Use the migrator URL
# here to exercise the current migration path as a non-superuser schema owner.
cd "$repo_root"
DATABASE_URL="$migrator_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli init-db >/dev/null

docker exec "$container_name" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sro_runtime' \
  -c 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sro_runtime' \
  -c 'REVOKE INSERT, UPDATE, DELETE ON TABLE alembic_version FROM sro_runtime' \
  -c 'GRANT SELECT ON TABLE alembic_version TO sro_runtime' \
  >/dev/null

if ! schema_output="$(DATABASE_URL="$migrator_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli schema-check)"; then
  printf '%s\n' "$schema_output" >&2
  exit 1
fi
if ! schema_output="$(DATABASE_URL="$runtime_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli schema-check)"; then
  printf '%s\n' "$schema_output" >&2
  exit 1
fi
role_report="$(DATABASE_URL="$runtime_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli db-role-check)"
for capability in superuser create_role create_database replication bypass_row_security database_create public_schema_create; do
  if ! rg -q -- "- ${capability}: False" <<<"$role_report"; then
    echo "Runtime role unexpectedly has elevated capability: ${capability}" >&2
    exit 1
  fi
done

DATABASE_URL="$runtime_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import User

with session_scope() as db:
    assert db.scalar(select(User.id).limit(1)) is None
    user = User(
        login="synthetic-role-probe",
        full_name="Synthetic Role Probe",
        password_hash="synthetic-only",
        sro_login="synthetic-role-probe",
        is_active=True,
    )
    db.add(user)
    db.flush()
    user.full_name = "Synthetic Role Probe Updated"
    db.flush()
    db.delete(user)
PY

docker exec "$container_name" psql -U sro_migrator -d postgres -v ON_ERROR_STOP=1 \
  -c 'CREATE TABLE public.migration_ddl_probe (id serial PRIMARY KEY, marker text NOT NULL)' \
  >/dev/null
docker exec "$container_name" psql -U sro_runtime -d postgres -v ON_ERROR_STOP=1 \
  -c "INSERT INTO public.migration_ddl_probe (marker) VALUES ('synthetic') RETURNING id" \
  -c "UPDATE public.migration_ddl_probe SET marker='updated' WHERE marker='synthetic'" \
  -c "DELETE FROM public.migration_ddl_probe WHERE marker='updated'" \
  >/dev/null
docker exec "$container_name" psql -U sro_migrator -d postgres -v ON_ERROR_STOP=1 \
  -c 'DROP TABLE public.migration_ddl_probe' >/dev/null
if docker exec "$container_name" psql -U sro_runtime -d postgres \
  -c 'CREATE TABLE public.runtime_ddl_probe (id integer)' >/dev/null 2>&1; then
  echo "Runtime role unexpectedly performed schema DDL" >&2
  exit 1
fi
if docker exec "$container_name" psql -U sro_runtime -d postgres \
  -c "UPDATE alembic_version SET version_num='runtime-must-not-change-this'" >/dev/null 2>&1; then
  echo "Runtime role unexpectedly modified Alembic's schema revision" >&2
  exit 1
fi

echo "Disposable role probe passed: non-superuser migrator applied Alembic; runtime DML and schema inspection worked, while DDL and Alembic revision writes were denied"
