#!/usr/bin/env bash

# Exercise fresh migrations, PostgreSQL claim/recovery tests, logical dump/restore,
# and populated-schema adoption using only a disposable loopback container and synthetic rows.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
container_name="sro-restore-drill-$$"
redis_container_name="${container_name}-redis"
python_bin="${SRO_ROBOT_PYTHON:-$repo_root/.venv/bin/python}"
drill_task_count="${SRO_DRILL_TASKS:-10000}"
if [[ ! -x "$python_bin" ]]; then
  echo "Python executable not found: $python_bin" >&2
  exit 2
fi
if [[ ! "$drill_task_count" =~ ^[1-9][0-9]*$ || "$drill_task_count" -gt 100000 ]]; then
  echo "SRO_DRILL_TASKS must be an integer between 1 and 100000" >&2
  exit 2
fi
upload_dir="$(mktemp -d /tmp/sro-restore-drill.XXXXXX)"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  docker rm -f "$redis_container_name" >/dev/null 2>&1 || true
  rmdir "$upload_dir" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

docker run --rm -d \
  --name "$container_name" \
  -p 127.0.0.1::5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  postgres:16-alpine >/dev/null

start_disposable_redis() {
  docker run --rm -d \
    --name "$redis_container_name" \
    -p 127.0.0.1::6379 \
    redis:7-alpine redis-server --save '' --appendonly no >/dev/null

  for attempt in {1..30}; do
    if docker exec "$redis_container_name" redis-cli ping 2>/dev/null | rg -q '^PONG$'; then
      break
    fi
    if [[ "$attempt" == 30 ]]; then
      echo "Disposable Redis did not become ready" >&2
      return 1
    fi
    sleep 1
  done

  local published_address
  published_address="$(docker port "$redis_container_name" 6379/tcp)"
  printf 'redis://127.0.0.1:%s/0' "${published_address##*:}"
}

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
docker exec "$container_name" createdb -U postgres migration_failure_target
failure_database_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/migration_failure_target"
DATABASE_URL="$failure_database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" PYTHONPATH="$repo_root" \
  "$python_bin" scripts/dev/migration_failure_smoke.py

DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli init-db

DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
SRO_DRILL_TASKS="$drill_task_count" \
  "$python_bin" - <<'PY'
import os

from app.db.base import SessionLocal
from app.db.models import Document, Event, IntakeSource, Task, TaskStatus, User

task_count = int(os.environ["SRO_DRILL_TASKS"])
staff_count = 20

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
    staff = [
        User(
            login=f"restore-staff-{index:02d}",
            full_name=f"Synthetic Staff {index:02d}",
            password_hash="synthetic-only",
            sro_login=f"restore-staff-{index:02d}",
            is_active=True,
        )
        for index in range(staff_count)
    ]
    db.add_all(staff)
    db.flush()
    for first_number in range(1, task_count + 1, 1000):
        tasks = [
            Task(
                document_id=document.id,
                scenario="person",
                profile="default",
                status=TaskStatus.needs_review,
                recognized={},
                values={"synthetic_workload": True},
                case_number=f"А99-{number}/2026",
                assignee_id=staff[(number - 1) % staff_count].id,
            )
            for number in range(first_number, min(first_number + 1000, task_count + 1))
        ]
        db.add_all(tasks)
        db.flush()
        db.add_all(
            Event(
                task_id=task_row.id,
                user_id=task_row.assignee_id,
                kind="restore_drill_workload",
                message="synthetic restore workload event",
                payload={"synthetic": True},
            )
            for task_row in tasks
        )
    db.add(Task(
        document_id=document.id,
        scenario="person",
        profile="default",
        status=TaskStatus.new,
        recognized={},
        values={"queue_recovery_candidate": True},
        case_number="А98-1/2026",
        assignee_id=user.id,
    ))
    db.commit()
PY

DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" SRO_TEST_POSTGRES=1 \
  "$python_bin" -m unittest tests.test_task_leases_postgres tests.test_workflow_concurrency_postgres -v

# Measure concurrent claims across distinct synthetic tasks. Results are diagnostic only.
DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" SRO_DISPOSABLE_DRILL=1 \
  "$python_bin" scripts/dev/lease_claim_load_smoke.py

# Prove a lost disposable Redis queue can be reconstructed from PostgreSQL state.
recovery_task_id="$(DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Task

with SessionLocal() as db:
    task_id = db.scalar(
        select(Task.id).where(Task.values["queue_recovery_candidate"].astext == "true")
    )
    if task_id is None:
        raise SystemExit("synthetic queue recovery candidate is missing")
    print(task_id)
PY
)"
recovery_task_ids="$(DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from app.db.base import SessionLocal
from app.services.leases import reconcilable_ocr_task_ids

with SessionLocal() as db:
    print(",".join(str(task_id) for task_id in reconcilable_ocr_task_ids(db)))
PY
)"
if [[ ",$recovery_task_ids," != *",$recovery_task_id,"* ]]; then
  echo "Synthetic queue recovery candidate is absent from the database reconciliation set" >&2
  exit 1
fi

verify_recovery_queue() {
  "$python_bin" - "$1" "$recovery_task_id" "$recovery_task_ids" <<'PY'
import sys

from redis import Redis
from rq import Queue

expected_ids = {int(task_id) for task_id in sys.argv[3].split(",") if task_id}
client = Redis.from_url(sys.argv[1], socket_connect_timeout=1, socket_timeout=1)
try:
    queue = Queue("ocr", connection=client)
    jobs = queue.jobs
    actual_ids = set()
    for job in jobs:
        if job.func_name.split(".")[-1] != "recognize" or len(job.args) != 1:
            raise SystemExit("recovered queue contains a job outside the local OCR contract")
        actual_ids.add(int(job.args[0]))
    if actual_ids != expected_ids or len(jobs) != len(expected_ids):
        raise SystemExit(
            f"recovered queue candidates differ from PostgreSQL: expected={len(expected_ids)} actual={len(jobs)}"
        )
    if int(sys.argv[2]) not in actual_ids:
        raise SystemExit("synthetic queue recovery candidate was not re-enqueued")
finally:
    client.close()
PY
}

redis_url="$(start_disposable_redis)"
DATABASE_URL="$database_url" REDIS_URL="$redis_url" APP_DEBUG=1 \
SRO_UPLOAD_DIR="$upload_dir" OCR_TASK_LEASES_ENABLED=1 \
  "$python_bin" -m app.cli ocr-reconcile --apply
verify_recovery_queue "$redis_url"
docker rm -f "$redis_container_name" >/dev/null

redis_url="$(start_disposable_redis)"
DATABASE_URL="$database_url" REDIS_URL="$redis_url" APP_DEBUG=1 \
SRO_UPLOAD_DIR="$upload_dir" OCR_TASK_LEASES_ENABLED=1 \
  "$python_bin" -m app.cli ocr-reconcile --apply
verify_recovery_queue "$redis_url"

DATABASE_URL="$database_url" REDIS_URL="$redis_url" APP_DEBUG=1 \
SRO_UPLOAD_DIR="$upload_dir" "$python_bin" - <<'PY'
import os
import re
import subprocess
import sys
import time

from redis import Redis
from rq import Queue, Worker
from rq.registry import FailedJobRegistry

client = Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=1, socket_timeout=1)
worker = Worker(
    [Queue("ocr", connection=client)],
    name="synthetic-ops-health",
    connection=client,
    prepare_for_work=False,
)
worker.pid = os.getpid()
worker.hostname = "synthetic"
worker.ip_address = "127.0.0.1"
worker.register_birth()
failed_registry = FailedJobRegistry("sro", connection=client)
expired_failed_id = "synthetic-expired-failure"
active_failed_id = "synthetic-active-failure"
client.zadd(failed_registry.key, {
    expired_failed_id: time.time() - 60,
    active_failed_id: time.time() + 600,
})
try:
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "ops-status"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        raise SystemExit(f"ops-status returned unexpected exit code {result.returncode}")
    if "Registered RQ workers: ocr=1 sro=0" not in result.stdout:
        raise SystemExit("ops-status did not detect the registered OCR worker")
    if "Failed RQ registry entries: ocr=0 sro=2" not in result.stdout:
        raise SystemExit("ops-status did not report aggregate failed-job registry counts")
    if not re.search(r"RQ worker heartbeat \(oldest age seconds / missing\): ocr=\d+s/missing=0 sro=none", result.stdout):
        raise SystemExit("ops-status did not report the synthetic worker heartbeat")
    if worker.name in result.stdout or worker.name in result.stderr:
        raise SystemExit("ops-status exposed a worker identifier")
    if expired_failed_id in result.stdout or active_failed_id in result.stdout:
        raise SystemExit("ops-status exposed a failed-job identifier")
    if client.zscore(failed_registry.key, expired_failed_id) is None:
        raise SystemExit("ops-status mutated the expired failed-job registry entry")
    if client.zscore(failed_registry.key, active_failed_id) is None:
        raise SystemExit("ops-status mutated the active failed-job registry entry")
finally:
    worker.register_death()
    client.close()
PY
echo "PostgreSQL-backed OCR queue-loss recovery passed after a disposable Redis restart"
echo "Disposable RQ worker registration and heartbeat diagnostics passed"

docker exec "$container_name" \
  pg_dump -U postgres -Fc -d postgres -f /tmp/sro-restore-drill.dump
docker exec "$container_name" createdb -U postgres restore_target
docker exec "$container_name" pg_restore \
  --exit-on-error --no-owner --no-privileges \
  -U postgres -d restore_target /tmp/sro-restore-drill.dump

restore_evidence="$(docker exec "$container_name" psql -U postgres -d restore_target -Atc \
  "SELECT 'revision=' || version_num FROM alembic_version; SELECT 'canary=' || count(*) FROM task t JOIN document d ON d.id=t.document_id JOIN event e ON e.task_id=t.id WHERE t.values->>'marker'='restore-canary' AND e.kind='restore_canary' AND e.payload->>'marker'='restore-canary'; SELECT 'workload_tasks=' || count(*) FROM task WHERE values->>'synthetic_workload'='true'; SELECT 'workload_events=' || count(*) FROM event WHERE kind='restore_drill_workload'; SELECT 'synthetic_staff=' || count(*) FROM \"user\" WHERE login LIKE 'restore-staff-%'; SELECT 'identity_mismatches=' || count(*) FROM task t JOIN \"user\" u ON u.id=t.assignee_id JOIN event e ON e.task_id=t.id WHERE t.values->>'synthetic_workload'='true' AND (u.login <> ('restore-staff-' || lpad(((substring(t.case_number from 'А99-([0-9]+)/2026')::int - 1) % 20)::text, 2, '0')) OR e.kind <> 'restore_drill_workload' OR e.user_id <> t.assignee_id);")"
expected_evidence=$'revision=7af0f5c2d441\ncanary=1\nworkload_tasks='"$drill_task_count"$'\nworkload_events='"$drill_task_count"$'\nsynthetic_staff=20\nidentity_mismatches=0'
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
  "$python_bin" -m app.cli init-db 2>&1)"; then
  echo "init-db unexpectedly accepted a populated unversioned database" >&2
  exit 1
fi
if [[ "$refusal_output" != *"без записанной ревизии Alembic"* ]]; then
  echo "init-db failed for an unexpected reason during adoption rehearsal" >&2
  exit 1
fi

schema_check_status=0
schema_check="$(DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli schema-check)" || schema_check_status=$?
if [[ "$schema_check_status" != 1 || \
      "$schema_check" != *"Alembic revision: unversioned"* || \
      "$schema_check" != *"Schema differs from ORM metadata (2 difference(s))"* || \
      "$schema_check" != *"Table('task_lease'"* || \
      "$schema_check" != *"Index('ix_task_lease_lease_until'"* ]]; then
  echo "Restored baseline schema did not report exactly the expected lease table and index" >&2
  echo "$schema_check" >&2
  exit 1
fi

# Run the legacy feature-disabled handler while the clone still has only the
# baseline schema. A later process will read its result after additive upgrade.
DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
OCR_TASK_LEASES_ENABLED=0 PYTHONPATH="$repo_root" \
  "$python_bin" scripts/dev/legacy_worker_schema_smoke.py run

DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from alembic import command
from alembic.config import Config
from sro_robot.config import ROOT

command.stamp(Config(str(ROOT / "alembic.ini")), "bc518c7514d3")
PY
DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli init-db >/dev/null
DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
OCR_TASK_LEASES_ENABLED=0 PYTHONPATH="$repo_root" \
  "$python_bin" scripts/dev/legacy_worker_schema_smoke.py verify
adoption_evidence="$(docker exec "$container_name" psql -U postgres -d restore_target -Atc \
  "SELECT 'revision=' || version_num FROM alembic_version; SELECT 'canary=' || count(*) FROM task t JOIN document d ON d.id=t.document_id JOIN event e ON e.task_id=t.id WHERE t.values->>'marker'='restore-canary' AND e.kind='restore_canary' AND e.payload->>'marker'='restore-canary'; SELECT 'workload_tasks=' || count(*) FROM task WHERE values->>'synthetic_workload'='true'; SELECT 'workload_events=' || count(*) FROM event WHERE kind='restore_drill_workload'; SELECT 'synthetic_staff=' || count(*) FROM \"user\" WHERE login LIKE 'restore-staff-%'; SELECT 'identity_mismatches=' || count(*) FROM task t JOIN \"user\" u ON u.id=t.assignee_id JOIN event e ON e.task_id=t.id WHERE t.values->>'synthetic_workload'='true' AND (u.login <> ('restore-staff-' || lpad(((substring(t.case_number from 'А99-([0-9]+)/2026')::int - 1) % 20)::text, 2, '0')) OR e.kind <> 'restore_drill_workload' OR e.user_id <> t.assignee_id); SELECT 'lease_table=' || (to_regclass('public.task_lease') IS NOT NULL);")"
expected_adoption_evidence=$'revision=7af0f5c2d441\ncanary=1\nworkload_tasks='"$drill_task_count"$'\nworkload_events='"$drill_task_count"$'\nsynthetic_staff=20\nidentity_mismatches=0\nlease_table=true'
if [[ "$adoption_evidence" != "$expected_adoption_evidence" ]]; then
  echo "Adoption rehearsal changed or lost the synthetic workflow canary" >&2
  exit 1
fi

echo "Populated unversioned schema refusal, exact additive comparison and isolated migration adoption passed"
echo "Disposable PostgreSQL backup/restore drill passed"
