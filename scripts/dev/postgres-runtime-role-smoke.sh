#!/usr/bin/env bash

# Probe a proposed split between a PostgreSQL migration owner and DML-only app role.
# All schema/data changes are confined to one ephemeral PostgreSQL 16 container.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
container_name="sro-role-smoke-$$"
python_bin="${SRO_ROBOT_PYTHON:-$repo_root/.venv/bin/python}"
upload_dir=""
# This drill exercises PostgreSQL only. Never inherit a developer's Redis target.
export REDIS_URL='redis://synthetic:synthetic@127.0.0.1:1/0'

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
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

upload_dir="$(mktemp -d /tmp/sro-runtime-role.XXXXXX)"
docker run --rm -d \
  --name "$container_name" \
  -p 127.0.0.1::5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea >/dev/null

for attempt in {1..30}; do
  # The socket-only bootstrap server stops before the permanent TCP server.
  if docker exec "$container_name" pg_isready -h 127.0.0.1 -p 5432 -U postgres >/dev/null 2>&1; then
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
if [[ "$image" != "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea" || "$published_address" != 127.0.0.1:* ]]; then
  echo "Refusing role probe without the expected loopback PostgreSQL 16 container" >&2
  exit 2
fi
host_port="${published_address##*:}"
migrator_url="postgresql+psycopg://sro_migrator@127.0.0.1:${host_port}/postgres"
runtime_url="postgresql+psycopg://sro_runtime@127.0.0.1:${host_port}/postgres"
overpriv_runtime_url="postgresql+psycopg://sro_overpriv_runtime@127.0.0.1:${host_port}/postgres"
member_runtime_url="postgresql+psycopg://sro_member_runtime@127.0.0.1:${host_port}/postgres"
owner_runtime_url="postgresql+psycopg://sro_owner_runtime@127.0.0.1:${host_port}/postgres"
truncate_runtime_url="postgresql+psycopg://sro_truncate_runtime@127.0.0.1:${host_port}/postgres"
revision_runtime_url="postgresql+psycopg://sro_revision_runtime@127.0.0.1:${host_port}/postgres"
references_runtime_url="postgresql+psycopg://sro_references_runtime@127.0.0.1:${host_port}/postgres"
trigger_runtime_url="postgresql+psycopg://sro_trigger_runtime@127.0.0.1:${host_port}/postgres"
sequence_runtime_url="postgresql+psycopg://sro_sequence_runtime@127.0.0.1:${host_port}/postgres"
definer_runtime_url="postgresql+psycopg://sro_definer_runtime@127.0.0.1:${host_port}/postgres"
shadow_create_runtime_url="postgresql+psycopg://sro_shadow_create_runtime@127.0.0.1:${host_port}/postgres"
shadow_owner_runtime_url="postgresql+psycopg://sro_shadow_owner_runtime@127.0.0.1:${host_port}/postgres"
shadow_routine_runtime_url="postgresql+psycopg://sro_shadow_routine_runtime@127.0.0.1:${host_port}/postgres"
shadow_definer_runtime_url="postgresql+psycopg://sro_shadow_definer_runtime@127.0.0.1:${host_port}/postgres"
sequence_owner_runtime_url="postgresql+psycopg://sro_sequence_owner_runtime@127.0.0.1:${host_port}/postgres"
routine_owner_runtime_url="postgresql+psycopg://sro_routine_owner_runtime@127.0.0.1:${host_port}/postgres"
type_owner_runtime_url="postgresql+psycopg://sro_type_owner_runtime@127.0.0.1:${host_port}/postgres"

docker exec "$container_name" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'CREATE ROLE sro_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_overpriv_runtime LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_object_owner_group NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_member_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_owner_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_truncate_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_revision_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_references_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_trigger_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_sequence_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_definer_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_sequence_owner_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_routine_owner_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_type_owner_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_shadow_create_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_shadow_owner_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_shadow_routine_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'CREATE ROLE sro_shadow_definer_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS' \
  -c 'GRANT sro_object_owner_group TO sro_member_runtime' \
  -c 'GRANT CONNECT ON DATABASE postgres TO sro_migrator, sro_runtime, sro_overpriv_runtime, sro_member_runtime, sro_owner_runtime, sro_truncate_runtime, sro_revision_runtime, sro_references_runtime, sro_trigger_runtime, sro_sequence_runtime, sro_definer_runtime, sro_sequence_owner_runtime, sro_routine_owner_runtime, sro_type_owner_runtime, sro_shadow_create_runtime, sro_shadow_owner_runtime, sro_shadow_routine_runtime, sro_shadow_definer_runtime' \
  -c 'CREATE SCHEMA migration_shadow AUTHORIZATION sro_migrator' \
  -c 'CREATE SCHEMA runtime_shadow AUTHORIZATION sro_object_owner_group' \
  -c 'GRANT USAGE ON SCHEMA runtime_shadow TO sro_runtime' \
  -c 'CREATE SCHEMA runtime_create_shadow AUTHORIZATION sro_migrator' \
  -c 'GRANT USAGE, CREATE ON SCHEMA runtime_create_shadow TO sro_shadow_create_runtime' \
  -c 'CREATE SCHEMA runtime_owned_shadow AUTHORIZATION sro_shadow_owner_runtime' \
  -c 'CREATE SCHEMA runtime_routine_shadow AUTHORIZATION sro_migrator' \
  -c 'GRANT USAGE ON SCHEMA runtime_routine_shadow TO sro_shadow_definer_runtime' \
  -c 'ALTER ROLE sro_migrator IN DATABASE postgres SET search_path TO migration_shadow, public' \
  -c 'ALTER ROLE sro_runtime IN DATABASE postgres SET search_path TO runtime_shadow, public' \
  -c 'CREATE TABLE migration_shadow.alembic_version (version_num varchar(32) NOT NULL)' \
  -c 'CREATE TABLE runtime_shadow."user" (id integer NOT NULL)' \
  -c 'ALTER SCHEMA public OWNER TO sro_migrator' \
  -c 'GRANT USAGE, CREATE ON SCHEMA public TO sro_migrator' \
  -c 'GRANT USAGE ON SCHEMA public TO sro_runtime' \
  -c 'GRANT TEMP ON DATABASE postgres TO sro_runtime' \
  -c 'ALTER DEFAULT PRIVILEGES FOR ROLE sro_migrator IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sro_runtime' \
  -c 'ALTER DEFAULT PRIVILEGES FOR ROLE sro_migrator IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO sro_runtime' \
  >/dev/null

# Keep the application's runtime connection on the DML-only role while Alembic
# receives a distinct schema-owner connection.
cd "$repo_root"
DATABASE_URL="$runtime_url" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli init-db >/dev/null

docker exec "$container_name" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sro_runtime' \
  -c 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sro_runtime' \
  -c 'REVOKE INSERT, UPDATE, DELETE ON TABLE alembic_version FROM sro_runtime' \
  -c 'GRANT SELECT ON TABLE alembic_version TO sro_runtime' \
  -c 'GRANT TRUNCATE ON ALL TABLES IN SCHEMA public TO sro_truncate_runtime' \
  -c 'GRANT UPDATE ON TABLE alembic_version TO sro_revision_runtime' \
  -c 'GRANT REFERENCES ON ALL TABLES IN SCHEMA public TO sro_references_runtime' \
  -c 'GRANT TRIGGER ON ALL TABLES IN SCHEMA public TO sro_trigger_runtime' \
  -c 'GRANT UPDATE ON ALL SEQUENCES IN SCHEMA public TO sro_sequence_runtime' \
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
table_count="$(sed -n 's/^- public_table_count: //p' <<<"$role_report")"
if [[ ! "$table_count" =~ ^[0-9]+$ || "$table_count" -lt 1 ]]; then
  echo "Runtime role check did not report a public table count" >&2
  exit 1
fi
sequence_count="$(sed -n 's/^- public_sequence_count: //p' <<<"$role_report")"
if [[ ! "$sequence_count" =~ ^[0-9]+$ || "$sequence_count" -lt 1 ]]; then
  echo "Runtime role check did not report public sequences" >&2
  exit 1
fi
for capability in public_table_select; do
  if ! rg -q -- "- ${capability}: ${table_count}/${table_count}" <<<"$role_report"; then
    echo "Runtime role lacks read access required for current tables: ${capability}" >&2
    exit 1
  fi
done
for capability in public_table_insert public_table_update public_table_delete; do
  expected_write_count=$((table_count - 1))
  if ! rg -q -- "- ${capability}: ${expected_write_count}/${table_count}" <<<"$role_report"; then
    echo "Runtime role DML boundary differs from the probe grant profile: ${capability}" >&2
    exit 1
  fi
done
if ! rg -q -- "- public_sequence_usage: ${sequence_count}/${sequence_count}" <<<"$role_report"; then
  echo "Runtime role lacks usage on every public serial sequence" >&2
  exit 1
fi
for capability in superuser create_role create_database replication bypass_row_security database_create public_schema_create; do
  if ! rg -q -- "- ${capability}: False" <<<"$role_report"; then
    echo "Runtime role unexpectedly has elevated capability: ${capability}" >&2
    exit 1
  fi
done
for capability in inherited_role_count settable_role_count settable_elevated_role_count; do
  if ! rg -q -- "- ${capability}: 0$" <<<"$role_report"; then
    echo "Runtime role unexpectedly inherits or can assume another role: ${capability}" >&2
    exit 1
  fi
done

DATABASE_URL="$runtime_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import User

with session_scope() as db:
    assert db.connection().exec_driver_sql("SELECT current_schema()").scalar_one() == "public"
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

DATABASE_URL="$runtime_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from app.db.base import engine
from sqlalchemy import text

with engine.begin() as connection:
    assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == "public"
    backend_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
    connection.exec_driver_sql(
        "CREATE TEMP TABLE alembic_version (version_num varchar(32) NOT NULL)"
    )
    connection.exec_driver_sql(
        "INSERT INTO pg_temp.alembic_version (version_num) VALUES ('shadowed-revision')"
    )
    # Reproduce the old explicit-public setting on this disposable backend: the
    # implicit temporary schema takes precedence over public relations.
    connection.exec_driver_sql("SET SESSION search_path TO public")
    baseline_revision = connection.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one()
    assert baseline_revision == "shadowed-revision"
    connection.exec_driver_sql("SET SESSION search_path TO runtime_shadow, public")

with engine.connect() as connection:
    assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == "public"
    assert connection.execute(text("SELECT pg_backend_pid()")).scalar_one() == backend_pid
    assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "e75a2c8d190b"
    assert connection.exec_driver_sql("SHOW search_path").scalar_one() == "public, pg_temp"
engine.dispose()
PY

DATABASE_URL="$runtime_url" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from app.db.base import migration_engine

with migration_engine.begin() as connection:
    assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == "public"
    backend_pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
    connection.exec_driver_sql(
        "CREATE TEMP TABLE alembic_version (version_num varchar(32) NOT NULL)"
    )
    connection.exec_driver_sql(
        "INSERT INTO pg_temp.alembic_version (version_num) VALUES ('shadowed-revision')"
    )
    # Model a stale pooled migration connection where init-db's unqualified
    # Alembic preflight would otherwise resolve the session temp relation.
    connection.exec_driver_sql("SET SESSION search_path TO public")
    baseline_revision = connection.exec_driver_sql(
        "SELECT version_num FROM alembic_version"
    ).scalar_one()
    assert baseline_revision == "shadowed-revision"

with migration_engine.connect() as connection:
    assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == "public"
    assert connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one() == backend_pid
    assert connection.exec_driver_sql(
        "SELECT version_num FROM alembic_version"
    ).scalar_one() == "e75a2c8d190b"
    assert connection.exec_driver_sql("SHOW search_path").scalar_one() == "public, pg_temp"
PY

docker exec "$container_name" psql -U sro_migrator -d postgres -v ON_ERROR_STOP=1 \
  -c 'CREATE TABLE public.migration_ddl_probe (id serial PRIMARY KEY, marker text NOT NULL)' \
  >/dev/null
docker exec "$container_name" psql -U sro_runtime -d postgres -v ON_ERROR_STOP=1 \
  -c "INSERT INTO public.migration_ddl_probe (marker) VALUES ('synthetic') RETURNING id" \
  -c "UPDATE public.migration_ddl_probe SET marker='updated' WHERE marker='synthetic'" \
  -c "DELETE FROM public.migration_ddl_probe WHERE marker='updated'" \
  >/dev/null
audit_event_id="$(docker exec "$container_name" psql -U sro_runtime -d postgres -qAtc \
  "INSERT INTO public.event (kind, message, payload) VALUES ('recognized', 'synthetic append', '{}') RETURNING id")"
if docker exec "$container_name" psql -U sro_runtime -d postgres \
  -c "UPDATE public.event SET message='synthetic tamper' WHERE id=${audit_event_id}" >/dev/null 2>&1; then
  echo "Runtime role unexpectedly modified an audit event" >&2
  exit 1
fi
if docker exec "$container_name" psql -U sro_runtime -d postgres \
  -c "DELETE FROM public.event WHERE id=${audit_event_id}" >/dev/null 2>&1; then
  echo "Runtime role unexpectedly deleted an audit event" >&2
  exit 1
fi
docker exec "$container_name" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'GRANT TRUNCATE ON TABLE public.event TO sro_runtime' >/dev/null
if docker exec "$container_name" psql -U sro_runtime -d postgres \
  -c 'TRUNCATE TABLE public.event' >/dev/null 2>&1; then
  echo "Audit trigger unexpectedly accepted TRUNCATE" >&2
  exit 1
fi
docker exec "$container_name" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'REVOKE TRUNCATE ON TABLE public.event FROM sro_runtime' >/dev/null
if [[ "$(docker exec "$container_name" psql -U sro_runtime -d postgres -Atc \
  "SELECT message FROM public.event WHERE id=${audit_event_id}")" != "Документ распознан" ]]; then
  echo "Append-only event guard did not preserve the original audit row" >&2
  exit 1
fi
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

docker exec "$container_name" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'CREATE TABLE public.runtime_group_owner_probe (id integer NOT NULL)' \
  -c 'ALTER TABLE public.runtime_group_owner_probe OWNER TO sro_object_owner_group' \
  -c 'CREATE TABLE public.runtime_direct_owner_probe (id integer NOT NULL)' \
  -c 'ALTER TABLE public.runtime_direct_owner_probe OWNER TO sro_owner_runtime' \
  >/dev/null

docker exec "$container_name" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'GRANT CREATE ON SCHEMA public TO sro_sequence_owner_runtime, sro_routine_owner_runtime, sro_type_owner_runtime' \
  -c 'CREATE SEQUENCE public.runtime_owned_sequence_probe' \
  -c 'ALTER SEQUENCE public.runtime_owned_sequence_probe OWNER TO sro_sequence_owner_runtime' \
  -c 'CREATE FUNCTION public.runtime_owned_routine_probe() RETURNS integer LANGUAGE SQL AS $$ SELECT 1 $$' \
  -c 'ALTER FUNCTION public.runtime_owned_routine_probe() OWNER TO sro_routine_owner_runtime' \
  -c "CREATE TYPE public.runtime_owned_type_probe AS ENUM ('synthetic')" \
  -c 'ALTER TYPE public.runtime_owned_type_probe OWNER TO sro_type_owner_runtime' \
  -c 'REVOKE CREATE ON SCHEMA public FROM sro_sequence_owner_runtime, sro_routine_owner_runtime, sro_type_owner_runtime' \
  -c 'CREATE FUNCTION runtime_routine_shadow.runtime_owned_routine_probe() RETURNS integer LANGUAGE SQL AS $$ SELECT 1 $$' \
  -c 'ALTER FUNCTION runtime_routine_shadow.runtime_owned_routine_probe() OWNER TO sro_shadow_routine_runtime' \
  -c 'CREATE TABLE runtime_routine_shadow.definer_probe_data (marker text NOT NULL)' \
  -c 'ALTER TABLE runtime_routine_shadow.definer_probe_data OWNER TO sro_migrator' \
  -c "INSERT INTO runtime_routine_shadow.definer_probe_data (marker) VALUES ('synthetic')" \
  -c 'CREATE FUNCTION runtime_routine_shadow.security_definer_probe() RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$ BEGIN DELETE FROM runtime_routine_shadow.definer_probe_data; END; $$' \
  -c 'ALTER FUNCTION runtime_routine_shadow.security_definer_probe() OWNER TO sro_migrator' \
  -c 'REVOKE EXECUTE ON FUNCTION runtime_routine_shadow.security_definer_probe() FROM PUBLIC' \
  -c 'GRANT EXECUTE ON FUNCTION runtime_routine_shadow.security_definer_probe() TO sro_shadow_definer_runtime' \
  >/dev/null

DATABASE_URL="$runtime_url" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=0 \
  APP_SECRET_KEY=synthetic-runtime-policy-secret-000000000000 \
  REDIS_URL="redis://127.0.0.1:${host_port}/0" SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<PY
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, text

from app.db.base import engine, migration_engine

admin_engine = create_engine("postgresql+psycopg://postgres@127.0.0.1:${host_port}/postgres")
try:
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1
    with migration_engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1

    def check_connection(_index: int) -> int:
        with engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar_one()

    with ThreadPoolExecutor(max_workers=20) as pool:
        assert list(pool.map(check_connection, range(40))) == [1] * 40

    with admin_engine.begin() as connection:
        connection.exec_driver_sql("ALTER ROLE sro_runtime CREATEDB")
    try:
        with engine.connect():
            raise AssertionError("pool checkout accepted a newly elevated role")
    except RuntimeError as exc:
        assert "privilege ceiling" in str(exc)
    finally:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql("ALTER ROLE sro_runtime NOCREATEDB")
finally:
    engine.dispose()
    migration_engine.dispose()
    admin_engine.dispose()
PY

DATABASE_URL="$overpriv_runtime_url" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=0 \
  APP_SECRET_KEY=synthetic-runtime-policy-secret-000000000000 \
  REDIS_URL="redis://127.0.0.1:${host_port}/0" SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from app.db.base import engine

try:
    with engine.connect():
        raise AssertionError("overprivileged runtime role was accepted")
except RuntimeError as exc:
    assert "privilege ceiling" in str(exc)
    assert "sro_overpriv_runtime" not in str(exc)
finally:
    engine.dispose()
PY

DATABASE_URL="$member_runtime_url" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=0 \
  APP_SECRET_KEY=synthetic-runtime-policy-secret-000000000000 \
  REDIS_URL="redis://127.0.0.1:${host_port}/0" SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from app.db.base import engine

try:
    with engine.connect():
        raise AssertionError("runtime role with elevated membership was accepted")
except RuntimeError as exc:
    assert "privilege ceiling" in str(exc)
    assert "sro_member_runtime" not in str(exc)
finally:
    engine.dispose()
PY

DATABASE_URL="$owner_runtime_url" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=0 \
  APP_SECRET_KEY=synthetic-runtime-policy-secret-000000000000 \
  REDIS_URL="redis://127.0.0.1:${host_port}/0" SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from app.db.base import engine

try:
    with engine.connect():
        raise AssertionError("runtime role owning a public table was accepted")
except RuntimeError as exc:
    assert "privilege ceiling" in str(exc)
    assert "sro_owner_runtime" not in str(exc)
finally:
    engine.dispose()
PY

DATABASE_URL="$truncate_runtime_url" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=0 \
  APP_SECRET_KEY=synthetic-runtime-policy-secret-000000000000 \
  REDIS_URL="redis://127.0.0.1:${host_port}/0" SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from app.db.base import engine

try:
    with engine.connect():
        raise AssertionError("runtime role with TRUNCATE was accepted")
except RuntimeError as exc:
    assert "privilege ceiling" in str(exc)
    assert "sro_truncate_runtime" not in str(exc)
finally:
    engine.dispose()
PY

DATABASE_URL="$revision_runtime_url" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=0 \
  APP_SECRET_KEY=synthetic-runtime-policy-secret-000000000000 \
  REDIS_URL="redis://127.0.0.1:${host_port}/0" SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from app.db.base import engine

try:
    with engine.connect():
        raise AssertionError("runtime role able to alter Alembic revision was accepted")
except RuntimeError as exc:
    assert "privilege ceiling" in str(exc)
    assert "sro_revision_runtime" not in str(exc)
finally:
    engine.dispose()
PY

for runtime_url_with_forbidden_grant in \
  "$references_runtime_url" \
  "$trigger_runtime_url" \
  "$sequence_runtime_url"; do
  DATABASE_URL="$runtime_url_with_forbidden_grant" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=0 \
    APP_SECRET_KEY=synthetic-runtime-policy-secret-000000000000 \
    REDIS_URL="redis://127.0.0.1:${host_port}/0" SRO_UPLOAD_DIR="$upload_dir" \
    "$python_bin" - <<'PY'
from app.db.base import engine

try:
    with engine.connect():
        raise AssertionError("runtime role with a destructive/public-object grant was accepted")
except RuntimeError as exc:
    assert "privilege ceiling" in str(exc)
finally:
    engine.dispose()
PY
done

for runtime_url_with_owned_object in \
  "$sequence_owner_runtime_url" \
  "$routine_owner_runtime_url" \
  "$type_owner_runtime_url" \
  "$shadow_owner_runtime_url" \
  "$shadow_routine_runtime_url"; do
  DATABASE_URL="$runtime_url_with_owned_object" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=0 \
    APP_SECRET_KEY=synthetic-runtime-policy-secret-000000000000 \
    REDIS_URL="redis://127.0.0.1:${host_port}/0" SRO_UPLOAD_DIR="$upload_dir" \
    "$python_bin" - <<'PY'
from app.db.base import engine

try:
    with engine.connect():
        raise AssertionError("runtime role owning a public object was accepted")
except RuntimeError as exc:
    assert "privilege ceiling" in str(exc)
finally:
    engine.dispose()
PY
done

for runtime_url_with_shadow_schema_privilege in \
  "$shadow_create_runtime_url" \
  "$shadow_owner_runtime_url"; do
  DATABASE_URL="$runtime_url_with_shadow_schema_privilege" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=0 \
    APP_SECRET_KEY=synthetic-runtime-policy-secret-000000000000 \
    REDIS_URL="redis://127.0.0.1:${host_port}/0" SRO_UPLOAD_DIR="$upload_dir" \
    "$python_bin" - <<'PY'
from app.db.base import engine

try:
    with engine.connect():
        raise AssertionError("runtime role with non-public schema DDL authority was accepted")
except RuntimeError as exc:
    assert "privilege ceiling" in str(exc)
finally:
    engine.dispose()
PY
done

# Prove a non-public SECURITY DEFINER routine has real owner authority before
# confirming the app's production connection refuses a role that can execute it.
docker exec "$container_name" psql -U sro_shadow_definer_runtime -d postgres -v ON_ERROR_STOP=1 \
  -c 'SELECT runtime_routine_shadow.security_definer_probe()' >/dev/null
remaining_shadow_definer_rows="$(docker exec "$container_name" psql -U sro_migrator -d postgres -At \
  -c 'SELECT count(*) FROM runtime_routine_shadow.definer_probe_data')"
if [[ "$remaining_shadow_definer_rows" != 0 ]]; then
  echo "Synthetic non-public SECURITY DEFINER function did not exercise its owner privileges" >&2
  exit 1
fi
DATABASE_URL="$shadow_definer_runtime_url" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=0 \
  APP_SECRET_KEY=synthetic-runtime-policy-secret-000000000000 \
  REDIS_URL="redis://127.0.0.1:${host_port}/0" SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from app.db.base import engine

try:
    with engine.connect():
        raise AssertionError("runtime role with non-public SECURITY DEFINER execution was accepted")
except RuntimeError as exc:
    assert "privilege ceiling" in str(exc)
finally:
    engine.dispose()
PY

docker exec "$container_name" psql -U sro_migrator -d postgres -v ON_ERROR_STOP=1 \
  -c 'CREATE TABLE public.runtime_security_definer_probe_data (marker text NOT NULL)' \
  -c "INSERT INTO public.runtime_security_definer_probe_data (marker) VALUES ('synthetic')" \
  -c 'CREATE FUNCTION public.runtime_security_definer_probe() RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$ BEGIN DELETE FROM public.runtime_security_definer_probe_data; END; $$' \
  -c 'REVOKE EXECUTE ON FUNCTION public.runtime_security_definer_probe() FROM PUBLIC' \
  -c 'GRANT EXECUTE ON FUNCTION public.runtime_security_definer_probe() TO sro_definer_runtime' \
  >/dev/null

runtime_role_diagnostics="$(DATABASE_URL="$runtime_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli db-role-check)"
if ! rg -q -- '- public_security_definer_exec_count: 0$' <<<"$runtime_role_diagnostics"; then
  echo "Runtime role diagnostic reported an inaccessible SECURITY DEFINER routine" >&2
  exit 1
fi
definer_role_diagnostics="$(DATABASE_URL="$definer_runtime_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli db-role-check)"
if ! rg -q -- '- public_security_definer_exec_count: 1$' <<<"$definer_role_diagnostics"; then
  echo "Runtime role diagnostic did not report its executable SECURITY DEFINER routine" >&2
  exit 1
fi

# Prove the synthetic function can mutate owner-protected data when the EXECUTE
# path is used directly, before asserting that production app connections fail
# closed for this identity. Everything remains inside the disposable database.
docker exec "$container_name" psql -U sro_definer_runtime -d postgres -v ON_ERROR_STOP=1 \
  -c 'SELECT public.runtime_security_definer_probe()' >/dev/null
remaining_definer_probe_rows="$(docker exec "$container_name" psql -U sro_migrator -d postgres -At \
  -c 'SELECT count(*) FROM public.runtime_security_definer_probe_data')"
if [[ "$remaining_definer_probe_rows" != 0 ]]; then
  echo "Synthetic SECURITY DEFINER function did not exercise its owner privileges" >&2
  exit 1
fi

DATABASE_URL="$definer_runtime_url" MIGRATION_DATABASE_URL="$migrator_url" APP_DEBUG=0 \
  APP_SECRET_KEY=synthetic-runtime-policy-secret-000000000000 \
  REDIS_URL="redis://127.0.0.1:${host_port}/0" SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from app.db.base import engine

try:
    with engine.connect():
        raise AssertionError("runtime role with SECURITY DEFINER execution was accepted")
except RuntimeError as exc:
    assert "privilege ceiling" in str(exc)
finally:
    engine.dispose()
PY

echo "Disposable role probe passed: migration/DML profiles worked; role escalation, user-schema DDL/ownership, destructive grants, Alembic revision writes and SECURITY DEFINER execution were rejected"
