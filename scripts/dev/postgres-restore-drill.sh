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
  if [[ "$upload_dir" == /tmp/sro-restore-drill.* ]]; then
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

DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
SRO_TEST_POSTGRES=1 SRO_DISPOSABLE_DRILL=1 SRO_DRILL_CONTAINER_NAME="$container_name" \
  "$python_bin" -m unittest tests.test_task_leases_postgres tests.test_workflow_concurrency_postgres -v

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
    if result.returncode not in (0, 1):
        raise SystemExit(f"ops-status returned unexpected exit code {result.returncode}")
    if "Queue backlog has no registered worker:" in result.stdout:
        raise SystemExit("ops-status detected an uncovered queue backlog")
    legacy_warning = "Legacy local OCR tasks require read-only reconciliation before processing"
    if result.returncode == 1 and legacy_warning not in result.stdout:
        raise SystemExit("ops-status returned exit 1 without a recognized legacy-task warning")
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

database_row_signature() {
  local database="$1"
  local -a tables=('"user"' document task event task_lease)
  local -a keys=(id id id id task_id)
  local index table key digest
  for index in "${!tables[@]}"; do
    table="${tables[$index]}"
    key="${keys[$index]}"
    digest="$(docker exec "$container_name" psql -U postgres -d "$database" -Atc \
      "COPY (SELECT row_to_json(row_data)::text FROM public.${table} AS row_data ORDER BY row_data.${key}) TO STDOUT" \
      | shasum -a 256 | awk '{print $1}')"
    printf '%s=%s\n' "$table" "$digest"
  done
  digest="$(docker exec "$container_name" psql -U postgres -d "$database" -Atc \
    "COPY (SELECT schemaname || '.' || sequencename || ':' || coalesce(last_value::text, 'NULL') FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename) TO STDOUT" \
    | shasum -a 256 | awk '{print $1}')"
  printf 'sequences=%s\n' "$digest"
}

source_row_signature="$(database_row_signature postgres)"
restored_row_signature="$(database_row_signature restore_target)"
if [[ "$source_row_signature" != "$restored_row_signature" ]]; then
  echo "Backup/restore changed one or more synthetic application rows or sequence states" >&2
  exit 1
fi
docker exec "$container_name" psql -U postgres -d restore_target -c \
  "UPDATE task SET error = 'synthetic-signature-probe' WHERE values->>'marker' = 'restore-canary'" >/dev/null
mutated_row_signature="$(database_row_signature restore_target)"
if [[ "$mutated_row_signature" == "$source_row_signature" ]]; then
  echo "Row signature failed to detect a synthetic restored-data mutation" >&2
  exit 1
fi
docker exec "$container_name" psql -U postgres -d restore_target -c \
  "UPDATE task SET error = '' WHERE values->>'marker' = 'restore-canary'" >/dev/null
restored_row_signature="$(database_row_signature restore_target)"
if [[ "$source_row_signature" != "$restored_row_signature" ]]; then
  echo "Synthetic row-signature control did not restore the disposable clone" >&2
  exit 1
fi
echo "Restored application rows and sequence state matched; synthetic mutation detection passed"

restore_evidence="$(docker exec "$container_name" psql -U postgres -d restore_target -Atc \
  "SELECT 'revision=' || version_num FROM alembic_version; SELECT 'canary=' || count(*) FROM task t JOIN document d ON d.id=t.document_id JOIN event e ON e.task_id=t.id WHERE t.values->>'marker'='restore-canary' AND e.kind='restore_canary' AND e.payload->>'marker'='restore-canary'; SELECT 'workload_tasks=' || count(*) FROM task WHERE values->>'synthetic_workload'='true'; SELECT 'workload_events=' || count(*) FROM event WHERE kind='restore_drill_workload'; SELECT 'synthetic_staff=' || count(*) FROM \"user\" WHERE login LIKE 'restore-staff-%'; SELECT 'identity_mismatches=' || count(*) FROM task t JOIN \"user\" u ON u.id=t.assignee_id JOIN event e ON e.task_id=t.id WHERE t.values->>'synthetic_workload'='true' AND (u.login <> ('restore-staff-' || lpad(((substring(t.case_number from 'А99-([0-9]+)/2026')::int - 1) % 20)::text, 2, '0')) OR e.kind <> 'restore_drill_workload' OR e.user_id <> t.assignee_id);")"
expected_evidence=$'revision=d84f3a210c6b\ncanary=1\nworkload_tasks='"$drill_task_count"$'\nworkload_events='"$drill_task_count"$'\nsynthetic_staff=20\nidentity_mismatches=0'
if [[ "$restore_evidence" != "$expected_evidence" ]]; then
  echo "Restored database did not contain the expected schema and synthetic workflow row" >&2
  exit 1
fi

echo "$restore_evidence"

# Simulate the previous baseline schema on the restored copy: its document hash
# index was non-unique, and it did not contain the lease table/revision marker.
# Add one synthetic duplicate to prove the uniqueness migration fails safely.
docker exec "$container_name" psql -U postgres -d restore_target -c \
  "DROP INDEX ix_document_content_hash; CREATE INDEX ix_document_content_hash ON document (content_hash); INSERT INTO document (filename, stored_path, content_hash, source) SELECT 'restore-duplicate-probe', '/tmp/restore-duplicate-probe', content_hash, source FROM document ORDER BY id LIMIT 1; DROP TABLE task_lease; DROP TABLE alembic_version" >/dev/null
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
      "$schema_check" != *"Schema differs from ORM metadata (5 difference(s))"* || \
      "$schema_check" != *"ix_document_content_hash"* || \
      "$schema_check" != *"Table('task_lease'"* || \
      "$schema_check" != *"Index('ix_task_lease_lease_until'"* ]]; then
  echo "Restored old schema did not report the expected lease/index drift" >&2
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
if migration_output="$(DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli init-db 2>&1)"; then
  echo "Unique-hash migration unexpectedly accepted a synthetic duplicate" >&2
  exit 1
fi
if [[ "$migration_output" != *"миграции не выполнены (IntegrityError)"* ]]; then
  echo "Unique-hash migration failed for an unexpected reason" >&2
  echo "$migration_output" >&2
  exit 1
fi
failed_migration_state="$(docker exec "$container_name" psql -U postgres -d restore_target -Atc \
  "SELECT 'revision=' || version_num FROM alembic_version; SELECT 'duplicate_excess=' || coalesce(sum(row_count - 1), 0) FROM (SELECT count(*) AS row_count FROM document GROUP BY content_hash HAVING count(*) > 1) AS duplicate_hashes; SELECT 'hash_index_unique=' || indisunique FROM pg_index WHERE indexrelid = 'ix_document_content_hash'::regclass; SELECT 'lease_table=' || (to_regclass('public.task_lease') IS NOT NULL);")"
expected_failed_migration_state=$'revision=bc518c7514d3\nduplicate_excess=1\nhash_index_unique=false\nlease_table=false'
if [[ "$failed_migration_state" != "$expected_failed_migration_state" ]]; then
  echo "Failed unique-hash migration changed data or left an unexpected schema state" >&2
  echo "$failed_migration_state" >&2
  exit 1
fi
docker exec "$container_name" psql -U postgres -d restore_target -c \
  "DELETE FROM document WHERE filename = 'restore-duplicate-probe'" >/dev/null
remaining_duplicate_excess="$(docker exec "$container_name" psql -U postgres -d restore_target -Atc \
  "SELECT coalesce(sum(row_count - 1), 0) FROM (SELECT count(*) AS row_count FROM document GROUP BY content_hash HAVING count(*) > 1) AS duplicate_hashes;")"
if [[ "$remaining_duplicate_excess" != "0" ]]; then
  echo "Synthetic migration blocker cleanup left duplicate hashes" >&2
  exit 1
fi
DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli init-db >/dev/null
DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
OCR_TASK_LEASES_ENABLED=0 PYTHONPATH="$repo_root" \
  "$python_bin" scripts/dev/legacy_worker_schema_smoke.py verify
adoption_evidence="$(docker exec "$container_name" psql -U postgres -d restore_target -Atc \
  "SELECT 'revision=' || version_num FROM alembic_version; SELECT 'canary=' || count(*) FROM task t JOIN document d ON d.id=t.document_id JOIN event e ON e.task_id=t.id WHERE t.values->>'marker'='restore-canary' AND e.kind='restore_canary' AND e.payload->>'marker'='restore-canary'; SELECT 'workload_tasks=' || count(*) FROM task WHERE values->>'synthetic_workload'='true'; SELECT 'workload_events=' || count(*) FROM event WHERE kind='restore_drill_workload'; SELECT 'synthetic_staff=' || count(*) FROM \"user\" WHERE login LIKE 'restore-staff-%'; SELECT 'identity_mismatches=' || count(*) FROM task t JOIN \"user\" u ON u.id=t.assignee_id JOIN event e ON e.task_id=t.id WHERE t.values->>'synthetic_workload'='true' AND (u.login <> ('restore-staff-' || lpad(((substring(t.case_number from 'А99-([0-9]+)/2026')::int - 1) % 20)::text, 2, '0')) OR e.kind <> 'restore_drill_workload' OR e.user_id <> t.assignee_id); SELECT 'lease_table=' || (to_regclass('public.task_lease') IS NOT NULL);")"
expected_adoption_evidence=$'revision=d84f3a210c6b\ncanary=1\nworkload_tasks='"$drill_task_count"$'\nworkload_events='"$drill_task_count"$'\nsynthetic_staff=20\nidentity_mismatches=0\nlease_table=true'
if [[ "$adoption_evidence" != "$expected_adoption_evidence" ]]; then
  echo "Adoption rehearsal changed or lost the synthetic workflow canary" >&2
  exit 1
fi

echo "Populated unversioned schema refusal, exact schema-drift comparison and isolated migration adoption passed"
echo "Disposable PostgreSQL backup/restore drill passed"

# Keep the optional/high-contention claim probe last so a saturated pool cannot
# prevent backup/restore/adoption evidence from completing. Its exit status still
# fails this drill when read-back finds unclaimed tasks.
DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
SRO_DISPOSABLE_DRILL=1 SRO_DRILL_CONTAINER_NAME="$container_name" \
  "$python_bin" scripts/dev/lease_claim_load_smoke.py
