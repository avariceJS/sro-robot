#!/usr/bin/env bash

# Exercise the application's PostgreSQL baseline and a logical dump/restore
# using only a disposable, loopback-bound PostgreSQL container and synthetic rows.

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

docker exec "$container_name" \
  pg_dump -U postgres -Fc -d postgres -f /tmp/sro-restore-drill.dump
docker exec "$container_name" createdb -U postgres restore_target
docker exec "$container_name" pg_restore \
  --exit-on-error --no-owner --no-privileges \
  -U postgres -d restore_target /tmp/sro-restore-drill.dump

restore_evidence="$(docker exec "$container_name" psql -U postgres -d restore_target -Atc \
  "SELECT 'revision=' || version_num FROM alembic_version; SELECT 'canary=' || count(*) FROM task t JOIN document d ON d.id=t.document_id JOIN event e ON e.task_id=t.id WHERE t.values->>'marker'='restore-canary' AND e.kind='restore_canary' AND e.payload->>'marker'='restore-canary';")"
expected_evidence=$'revision=bc518c7514d3\ncanary=1'
if [[ "$restore_evidence" != "$expected_evidence" ]]; then
  echo "Restored database did not contain the expected schema and synthetic workflow row" >&2
  exit 1
fi

echo "$restore_evidence"
echo "Disposable PostgreSQL backup/restore drill passed"
