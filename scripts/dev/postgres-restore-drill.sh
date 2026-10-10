#!/usr/bin/env bash

# Exercise fresh migrations, PostgreSQL claim/recovery tests, logical dump/restore,
# and populated-schema adoption using only a disposable loopback container and synthetic rows.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
container_name="sro-restore-drill-$$"
upload_dir="$(mktemp -d /tmp/sro-restore-drill.XXXXXX)"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  rmdir "$upload_dir" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

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

published_address="$(docker port "$container_name" 5432/tcp)"
host_port="${published_address##*:}"
database_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/postgres"

cd "$repo_root"
DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  .venv/bin/python -m app.cli init-db

DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  .venv/bin/python - <<'PY'
from app.db.base import SessionLocal
from app.db.models import Document, Event, IntakeSource, Task, TaskStatus, User

with SessionLocal() as db:
    user = User(
        login="restore-canary",
        full_name="Synthetic Restore Canary",
        password_hash="synthetic-only",
        sro_login="restore-canary",
        is_active=True,
    )
    db.add(user)
    db.flush()
    document = Document(
        filename="synthetic.pdf",
        stored_path="/tmp/synthetic.pdf",
        content_hash="a" * 64,
        source=IntakeSource.upload,
        uploaded_by_id=user.id,
    )
    db.add(document)
    db.flush()
    task = Task(
        document_id=document.id,
        scenario="person",
        profile="default",
        status=TaskStatus.needs_review,
        recognized={"canary": True},
        values={"marker": "restore-canary"},
        case_number="А00-1/2026",
        assignee_id=user.id,
    )
    db.add(task)
    db.flush()
    db.add(Event(
        task_id=task.id,
        user_id=user.id,
        kind="restore_canary",
        message="synthetic restore evidence",
        payload={"marker": "restore-canary"},
    ))
    db.commit()
PY

DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" SRO_TEST_POSTGRES=1 \
  .venv/bin/python -m unittest tests.test_task_leases_postgres -v

docker exec "$container_name" \
  pg_dump -U postgres -Fc -d postgres -f /tmp/sro-restore-drill.dump
docker exec "$container_name" createdb -U postgres restore_target
docker exec "$container_name" pg_restore \
  --exit-on-error --no-owner --no-privileges \
  -U postgres -d restore_target /tmp/sro-restore-drill.dump

restore_evidence="$(docker exec "$container_name" psql -U postgres -d restore_target -Atc \
  "SELECT 'revision=' || version_num FROM alembic_version; SELECT 'canary=' || count(*) FROM task t JOIN document d ON d.id=t.document_id JOIN event e ON e.task_id=t.id WHERE t.values->>'marker'='restore-canary' AND e.kind='restore_canary' AND e.payload->>'marker'='restore-canary';")"
expected_evidence=$'revision=7af0f5c2d441\ncanary=1'
if [[ "$restore_evidence" != "$expected_evidence" ]]; then
  echo "Restored database did not contain the expected schema and synthetic workflow row" >&2
  exit 1
fi

echo "$restore_evidence"

# Simulate the previous baseline schema on the restored copy by removing only
# the new lease table and revision marker. No source or shared database is changed.
docker exec "$container_name" psql -U postgres -d restore_target -c \
  'DROP TABLE task_lease; DROP TABLE alembic_version' >/dev/null
adoption_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/restore_target"
if refusal_output="$(DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  .venv/bin/python -m app.cli init-db 2>&1)"; then
  echo "init-db unexpectedly accepted a populated unversioned database" >&2
  exit 1
fi
if [[ "$refusal_output" != *"без записанной ревизии Alembic"* ]]; then
  echo "init-db failed for an unexpected reason during adoption rehearsal" >&2
  exit 1
fi

schema_check_status=0
schema_check="$(DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  .venv/bin/python -m app.cli schema-check)" || schema_check_status=$?
if [[ "$schema_check_status" != 1 || \
      "$schema_check" != *"Alembic revision: unversioned"* || \
      "$schema_check" != *"Schema differs from ORM metadata (2 difference(s))"* || \
      "$schema_check" != *"Table('task_lease'"* || \
      "$schema_check" != *"Index('ix_task_lease_lease_until'"* ]]; then
  echo "Restored baseline schema did not report exactly the expected lease table and index" >&2
  echo "$schema_check" >&2
  exit 1
fi

DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  .venv/bin/python - <<'PY'
from alembic import command
from alembic.config import Config
from sro_robot.config import ROOT

command.stamp(Config(str(ROOT / "alembic.ini")), "bc518c7514d3")
PY
DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  .venv/bin/python -m app.cli init-db >/dev/null
adoption_evidence="$(docker exec "$container_name" psql -U postgres -d restore_target -Atc \
  "SELECT 'revision=' || version_num FROM alembic_version; SELECT 'canary=' || count(*) FROM task t JOIN document d ON d.id=t.document_id JOIN event e ON e.task_id=t.id WHERE t.values->>'marker'='restore-canary' AND e.kind='restore_canary' AND e.payload->>'marker'='restore-canary'; SELECT 'lease_table=' || (to_regclass('public.task_lease') IS NOT NULL);")"
expected_adoption_evidence=$'revision=7af0f5c2d441\ncanary=1\nlease_table=true'
if [[ "$adoption_evidence" != "$expected_adoption_evidence" ]]; then
  echo "Adoption rehearsal changed or lost the synthetic workflow canary" >&2
  exit 1
fi

echo "Populated unversioned schema refusal, exact additive comparison and isolated migration adoption passed"
echo "Disposable PostgreSQL backup/restore drill passed"
