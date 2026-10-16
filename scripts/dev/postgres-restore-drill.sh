#!/usr/bin/env bash

# Exercise fresh migrations, PostgreSQL claim/recovery tests, logical dump/restore,
# and populated-schema adoption using only a disposable loopback container and synthetic rows.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
container_name="sro-restore-drill-$$"
redis_container_name="${container_name}-redis"
python_bin="${SRO_ROBOT_PYTHON:-$repo_root/.venv/bin/python}"
drill_task_count="${SRO_DRILL_TASKS:-10000}"
# Commands that do not exercise Redis still need explicit settings. Redis
# integration sections below replace this with their disposable container URL.
export REDIS_URL='redis://synthetic:synthetic@127.0.0.1:1/0'
export APP_SECRET_KEY='synthetic-disposable-session-key-0000000000000000'
if [[ ! -x "$python_bin" ]]; then
  echo "Python executable not found: $python_bin" >&2
  exit 2
fi
if [[ ! "$drill_task_count" =~ ^[1-9][0-9]*$ || "$drill_task_count" -gt 100000 ]]; then
  echo "SRO_DRILL_TASKS must be an integer between 1 and 100000" >&2
  exit 2
fi
upload_dir="$(mktemp -d /tmp/sro-restore-drill.XXXXXX)"
rolling_worker_pid=""
rolling_worker_fd_open=0

cleanup() {
  if [[ -n "$rolling_worker_pid" ]] && kill -0 "$rolling_worker_pid" 2>/dev/null; then
    kill "$rolling_worker_pid" 2>/dev/null || true
    wait "$rolling_worker_pid" 2>/dev/null || true
  fi
  if [[ "$rolling_worker_fd_open" == 1 ]]; then
    exec 9>&- 2>/dev/null || true
  fi
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
  postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea >/dev/null

start_disposable_redis() {
  docker run --rm -d \
    --name "$redis_container_name" \
    -p 127.0.0.1::6379 \
    redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499 \
    redis-server --save '' --appendonly no >/dev/null

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

published_address="$(docker port "$container_name" 5432/tcp)"
host_port="${published_address##*:}"
database_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/postgres"

cd "$repo_root"
docker exec "$container_name" createdb -U postgres migration_failure_target
failure_database_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/migration_failure_target"
DATABASE_URL="$failure_database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" PYTHONPATH="$repo_root" \
  "$python_bin" scripts/dev/migration_failure_smoke.py

docker exec "$container_name" createdb -U postgres intake_backfill_target
intake_backfill_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/intake_backfill_target"
DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
SRO_DISPOSABLE_DRILL=1 SRO_DRILL_CONTAINER_NAME="$container_name" \
SRO_INTAKE_BACKFILL_DATABASE_URL="$intake_backfill_url" \
  "$python_bin" scripts/dev/intake_item_migration_smoke.py

docker exec "$container_name" createdb -U postgres intake_digest_target
intake_digest_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/intake_digest_target"
DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
SRO_DISPOSABLE_DRILL=1 SRO_DRILL_CONTAINER_NAME="$container_name" \
SRO_INTAKE_DIGEST_DATABASE_URL="$intake_digest_url" \
  "$python_bin" scripts/dev/intake_metadata_digest_migration_smoke.py

docker exec "$container_name" createdb -U postgres deactivation_session_target
deactivation_session_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/deactivation_session_target"
DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
SRO_DISPOSABLE_DRILL=1 SRO_DRILL_CONTAINER_NAME="$container_name" \
SRO_DEACTIVATION_DATABASE_URL="$deactivation_session_url" \
  "$python_bin" scripts/dev/deactivation_session_migration_smoke.py

docker exec "$container_name" createdb -U postgres audit_task_context_target
audit_task_context_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/audit_task_context_target"
DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
SRO_DISPOSABLE_DRILL=1 SRO_DRILL_CONTAINER_NAME="$container_name" \
SRO_AUDIT_TASK_CONTEXT_DATABASE_URL="$audit_task_context_url" \
  "$python_bin" scripts/dev/audit_event_task_context_migration_smoke.py

docker exec "$container_name" createdb -U postgres lease_downgrade_target
lease_downgrade_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/lease_downgrade_target"
DATABASE_URL="$lease_downgrade_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from alembic import command
from alembic.config import Config
from datetime import datetime, timedelta, timezone
import os
from sqlalchemy import create_engine, text

from sro_robot.config import ROOT

command.upgrade(Config(str(ROOT / "alembic.ini")), "7af0f5c2d441")
engine = create_engine(os.environ["DATABASE_URL"])
with engine.begin() as connection:
    document_id = connection.execute(text("""
        INSERT INTO document (filename, stored_path, content_hash, source)
        VALUES (:filename, :stored_path, :content_hash, 'upload')
        RETURNING id
    """), {
        "filename": "synthetic-lease-downgrade.pdf",
        "stored_path": "/synthetic/lease-downgrade.pdf",
        "content_hash": "b" * 64,
    }).scalar_one()
    task_id = connection.execute(text("""
        INSERT INTO task (
            document_id, scenario, profile, status, recognized, values, case_number,
            record_url, error, dry_run, screenshots, browser_decision
        )
        VALUES (:document_id, 'person', 'default', 'recognizing', '{}'::jsonb,
                jsonb_build_object('lease_downgrade_probe', true), '', '', '', true,
                '[]'::jsonb, '')
        RETURNING id
    """), {"document_id": document_id}).scalar_one()
    connection.execute(text("""
        INSERT INTO task_lease (task_id, owner_token, job_kind, attempt, generation, lease_until)
        VALUES (:task_id, 'synthetic-owner-token-000000000000', 'ocr', 1, 1, :lease_until)
    """), {
        "task_id": task_id,
        "lease_until": datetime.now(timezone.utc) + timedelta(minutes=10),
    })
engine.dispose()
PY
if downgrade_output="$(DATABASE_URL="$lease_downgrade_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from alembic import command
from alembic.config import Config

from sro_robot.config import ROOT

try:
    command.downgrade(Config(str(ROOT / "alembic.ini")), "bc518c7514d3")
except RuntimeError as exc:
    expected = "Task lease history cannot be dropped automatically"
    if not str(exc).startswith(expected):
        raise
    print("lease_downgrade=refused")
else:
    raise SystemExit("active synthetic lease downgrade unexpectedly completed")
PY
)"; then
  :
else
  echo "Task-lease downgrade smoke failed" >&2
  exit 1
fi
if [[ "$downgrade_output" != "lease_downgrade=refused" ]]; then
  echo "Task-lease downgrade did not report the expected safety refusal" >&2
  exit 1
fi
lease_downgrade_state="$(docker exec "$container_name" psql -U postgres -d lease_downgrade_target -Atc \
  "SELECT version_num FROM alembic_version; SELECT count(*) FROM task WHERE status='recognizing' AND values->>'lease_downgrade_probe'='true'; SELECT count(*) FROM task_lease WHERE job_kind='ocr' AND attempt=1 AND generation=1 AND lease_until > now();")"
if [[ "$lease_downgrade_state" != $'7af0f5c2d441\n1\n1' ]]; then
  echo "Refused task-lease downgrade changed its schema or active ownership data" >&2
  exit 1
fi
echo "Active OCR lease downgrade refusal preserved task, lease and schema revision"

DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli init-db

DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
SRO_DRILL_TASKS="$drill_task_count" \
  "$python_bin" - <<'PY'
import os
from hashlib import sha256
from pathlib import Path

from app.db.base import SessionLocal
from app.db.models import Document, Event, IntakeSource, Task, TaskStatus, User

task_count = int(os.environ["SRO_DRILL_TASKS"])
staff_count = 20
workload_root = Path(os.environ["SRO_UPLOAD_DIR"]) / "workload"
workload_root.mkdir(mode=0o700)
workload_root.chmod(0o700)


def write_synthetic_pdf(filename: str, payload: bytes) -> str:
    path = workload_root / filename
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(payload)
    return sha256(payload).hexdigest()


canary_payload = b"%PDF-1.4\n% synthetic restore canary\n%%EOF\n"
canary_hash = write_synthetic_pdf("canary.pdf", canary_payload)

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
        stored_path=str(workload_root / "canary.pdf"),
        content_hash=canary_hash,
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
        kind="recognized",
        message="synthetic restore evidence",
        payload={},
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
        batch_end = min(first_number + 1000, task_count + 1)
        batch_numbers = range(first_number, batch_end)
        documents = []
        for number in batch_numbers:
            filename = f"synthetic-workload-{number:06d}.pdf"
            payload = f"%PDF-1.4\n% synthetic restore document {number}\n%%EOF\n".encode()
            documents.append(
                Document(
                    filename=filename,
                    stored_path=str(workload_root / filename),
                    content_hash=write_synthetic_pdf(filename, payload),
                    source=IntakeSource.upload,
                    uploaded_by_id=staff[(number - 1) % staff_count].id,
                )
            )
        db.add_all(documents)
        db.flush()
        tasks = [
            Task(
                document_id=document_row.id,
                scenario="person",
                profile="default",
                status=TaskStatus.needs_review,
                recognized={},
                values={"synthetic_workload": True},
                case_number=f"А99-{number}/2026",
                assignee_id=staff[(number - 1) % staff_count].id,
            )
            for number, document_row in zip(batch_numbers, documents, strict=True)
        ]
        db.add_all(tasks)
        db.flush()
        db.add_all(
            Event(
                task_id=task_row.id,
                user_id=task_row.assignee_id,
                kind="recognized",
                message="synthetic restore workload event",
                payload={},
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
  "$python_bin" -m unittest \
    tests.test_task_leases_postgres \
    tests.test_workflow_concurrency_postgres \
    tests.test_migration_lock_timeout_postgres \
    tests.test_ops_status_postgres \
    tests.test_schema_preflight_timeout_postgres \
    tests.test_db_session_timeouts_postgres \
    tests.test_bulk_processing_postgres \
    -v

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
SRO_UPLOAD_DIR="$upload_dir" SRO_DISPOSABLE_DRILL=1 \
SRO_DRILL_CONTAINER_NAME="$container_name" \
SRO_DRILL_REDIS_CONTAINER_NAME="$redis_container_name" \
  "$python_bin" scripts/dev/redis_login_limiter_smoke.py

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
    legacy_warning = "Unleased recognizing tasks require read-only reconciliation before processing"
    if result.returncode == 1 and legacy_warning not in result.stdout:
        raise SystemExit("ops-status returned exit 1 without a recognized legacy-task warning")
    if "Registered RQ workers: ocr=1 sro=0" not in result.stdout:
        raise SystemExit("ops-status did not detect the registered OCR worker")
    registry_line = (
        "RQ registry entries (cleanup disabled): "
        "ocr=started=0,deferred=0,scheduled=0,failed=0 "
        "sro=started=0,deferred=0,scheduled=0,failed=2"
    )
    if registry_line not in result.stdout:
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

DATABASE_URL="$database_url" REDIS_URL="$redis_url" APP_DEBUG=1 \
SRO_UPLOAD_DIR="$upload_dir" OCR_TASK_LEASES_ENABLED=1 \
SRO_DISPOSABLE_DRILL=1 SRO_DRILL_CONTAINER_NAME="$container_name" \
SRO_DRILL_REDIS_CONTAINER_NAME="$redis_container_name" \
  "$python_bin" scripts/dev/rq_lease_worker_smoke.py

DATABASE_URL="$database_url" REDIS_URL="$redis_url" APP_DEBUG=1 \
SRO_UPLOAD_DIR="$upload_dir" SRO_DISPOSABLE_DRILL=1 \
SRO_DRILL_CONTAINER_NAME="$container_name" \
SRO_DRILL_REDIS_CONTAINER_NAME="$redis_container_name" \
  "$python_bin" scripts/dev/rq_sro_error_privacy_smoke.py

docker exec "$container_name" \
  pg_dump -U postgres -Fc -d postgres -f /tmp/sro-restore-drill.dump
workload_archive="$upload_dir/workload-documents.tar"
restored_document_dir="$upload_dir/restored-documents"
mkdir -m 700 "$restored_document_dir"
tar -cf "$workload_archive" -C "$upload_dir" workload
tar -xf "$workload_archive" -C "$restored_document_dir"
docker exec "$container_name" createdb -U postgres restore_target
docker exec "$container_name" pg_restore \
  --exit-on-error --no-owner --no-privileges \
  -U postgres -d restore_target /tmp/sro-restore-drill.dump

DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
SRO_DRILL_TASKS="$drill_task_count" RESTORED_DOCUMENT_DIR="$restored_document_dir/workload" \
  "$python_bin" - <<'PY'
import hashlib
import os
import stat
from pathlib import Path

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Document

task_count = int(os.environ["SRO_DRILL_TASKS"])
source_root = Path(os.environ["SRO_UPLOAD_DIR"]) / "workload"
restored_root = Path(os.environ["RESTORED_DOCUMENT_DIR"])
signature = hashlib.sha256()
restored_count = 0

def verify_one_document(stored_path: str, expected_hash: str) -> tuple[Path, Path]:
    original = Path(stored_path)
    try:
        relative_path = original.relative_to(source_root)
    except ValueError as exc:
        raise SystemExit("Synthetic document path escaped its private drill root") from exc
    if len(relative_path.parts) != 1:
        raise SystemExit("Synthetic document path is outside the flat workload directory")
    restored = restored_root / relative_path
    try:
        file_stat = restored.lstat()
    except FileNotFoundError as exc:
        raise SystemExit("A database document is missing from the restored file snapshot") from exc
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise SystemExit("Restored synthetic document is not a private regular file")
    actual_hash = hashlib.sha256(restored.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise SystemExit("Restored document bytes differ from the database content hash")
    return relative_path, restored


with SessionLocal() as db:
    query = select(Document.stored_path, Document.content_hash).where(
        Document.stored_path.like(f"{source_root}/%")
    )
    first_document = db.execute(query.order_by(Document.stored_path)).first()
    if first_document is None:
        raise SystemExit("Database restore did not contain any documents under the drill root")
    first_stored_path, first_expected_hash = first_document
    _, first_restored_path = verify_one_document(first_stored_path, first_expected_hash)
    original_bytes = first_restored_path.read_bytes()

    first_restored_path.write_bytes(original_bytes + b"synthetic-corruption")
    try:
        verify_one_document(first_stored_path, first_expected_hash)
    except SystemExit as exc:
        if "content hash" not in str(exc):
            raise
    else:
        raise SystemExit("Document restore check accepted synthetic hash corruption")
    finally:
        first_restored_path.write_bytes(original_bytes)
        first_restored_path.chmod(0o600)

    first_restored_path.chmod(0o644)
    try:
        verify_one_document(first_stored_path, first_expected_hash)
    except SystemExit as exc:
        if "not a private regular file" not in str(exc):
            raise
    else:
        raise SystemExit("Document restore check accepted a group-readable synthetic file")
    finally:
        first_restored_path.chmod(0o600)

    first_restored_path.chmod(0o4600)
    try:
        verify_one_document(first_stored_path, first_expected_hash)
    except SystemExit as exc:
        if "not a private regular file" not in str(exc):
            raise
    else:
        raise SystemExit("Document restore check accepted a synthetic special permission bit")
    finally:
        first_restored_path.chmod(0o600)

    symlink_target = restored_root / "symlink-target-canary"
    symlink_target.write_bytes(b"synthetic symlink target")
    first_restored_path.unlink()
    first_restored_path.symlink_to(symlink_target)
    try:
        verify_one_document(first_stored_path, first_expected_hash)
    except SystemExit as exc:
        if "not a private regular file" not in str(exc):
            raise
    else:
        raise SystemExit("Document restore check accepted a synthetic symlink")
    finally:
        first_restored_path.unlink(missing_ok=True)
        first_restored_path.write_bytes(original_bytes)
        first_restored_path.chmod(0o600)
        symlink_target.unlink(missing_ok=True)

    first_restored_path.unlink()
    try:
        verify_one_document(first_stored_path, first_expected_hash)
    except SystemExit as exc:
        if "missing from the restored file snapshot" not in str(exc):
            raise
    else:
        raise SystemExit("Document restore check accepted a missing synthetic file")
    finally:
        first_restored_path.write_bytes(original_bytes)
        first_restored_path.chmod(0o600)

    rows = db.execute(query.order_by(Document.stored_path)).yield_per(1000)
    for stored_path, expected_hash in rows:
        relative_path, _restored = verify_one_document(stored_path, expected_hash)
        signature.update(relative_path.as_posix().encode())
        signature.update(b"\0")
        signature.update(expected_hash.encode())
        signature.update(b"\n")
        restored_count += 1

if restored_count != task_count + 1:
    raise SystemExit("Restored document count does not match the synthetic database workload")
print(f"restored_document_files={restored_count}")
print(f"restored_document_signature={signature.hexdigest()}")
print("document_integrity_negative_controls=corrupt_hash,missing_file,unsafe_mode,special_mode,symlink")
PY

expected_storage_files=$((drill_task_count + 1))
docker exec "$container_name" createdb -U postgres storage_audit_target
storage_audit_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/storage_audit_target"
DATABASE_URL="$storage_audit_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli init-db
DATABASE_URL="$database_url" STORAGE_AUDIT_DATABASE_URL="$storage_audit_url" APP_DEBUG=1 \
SRO_UPLOAD_DIR="$upload_dir/workload" SRO_EXPECTED_STORAGE_FILES="$expected_storage_files" \
  "$python_bin" - <<'PY'
import os

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import Document

source_root = os.path.abspath(os.environ["SRO_UPLOAD_DIR"])
expected_count = int(os.environ["SRO_EXPECTED_STORAGE_FILES"])
source_engine = create_engine(os.environ["DATABASE_URL"])
target_engine = create_engine(os.environ["STORAGE_AUDIT_DATABASE_URL"])
copied = 0
with Session(source_engine) as source, Session(target_engine) as target:
    rows = source.execute(
        select(Document).where(Document.stored_path.like(f"{source_root}/%"))
        .order_by(Document.id).execution_options(stream_results=True)
    ).scalars().yield_per(1000)
    for row in rows:
        target.add(Document(
            filename=row.filename,
            stored_path=row.stored_path,
            content_hash=row.content_hash,
            source=row.source,
            uploaded_by_id=None,
        ))
        copied += 1
        if copied % 1000 == 0:
            target.flush()
    target.commit()
if copied != expected_count:
    raise SystemExit("Isolated storage audit fixture has an unexpected document count")
print(f"isolated_storage_audit_references={copied}")
PY

storage_audit_status=0
storage_audit_output="$(DATABASE_URL="$storage_audit_url" APP_DEBUG=1 \
  SRO_UPLOAD_DIR="$upload_dir/workload" \
  "$python_bin" -m app.cli storage-check --database)" || storage_audit_status=$?
if [[ "$storage_audit_status" != 0 ]]; then
  echo "Read-only storage inventory audit exited with status $storage_audit_status" >&2
  echo "$storage_audit_output" >&2
  exit 1
fi
expected_storage_audit="database_documents=${expected_storage_files} missing_files=0 unreferenced_files=0 paths_outside_storage=0 duplicate_paths=0"
expected_storage_inventory="files=${expected_storage_files} files_with_group_or_other_access=0 symlinks=0 files_with_other_owner=0 unexpected_entries=0"
if [[ "$storage_audit_output" != *"directory_mode=0700 directory_owner_matches=True"* || \
      "$storage_audit_output" != *"$expected_storage_inventory"* || \
      "$storage_audit_output" != *"$expected_storage_audit"* || \
      "$storage_audit_output" == *"synthetic-"* ]]; then
  echo "Read-only storage inventory audit failed at the synthetic restore workload size" >&2
  echo "$storage_audit_output" >&2
  exit 1
fi
echo "synthetic_storage_audit_files=${expected_storage_files} missing=0 unreferenced=0 unsafe=0"

database_row_signature() {
  local database="$1"
  local -a tables=('"user"' document task event task_lease intake_operation job_outbox intake_item)
  local -a keys=(id id id id task_id id id id)
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
  "SELECT 'revision=' || version_num FROM alembic_version; SELECT 'canary=' || count(*) FROM task t JOIN document d ON d.id=t.document_id JOIN event e ON e.task_id=t.id WHERE t.values->>'marker'='restore-canary' AND e.kind='recognized'; SELECT 'workload_tasks=' || count(*) FROM task WHERE values->>'synthetic_workload'='true'; SELECT 'workload_events=' || count(*) FROM event e JOIN task t ON t.id=e.task_id WHERE t.values->>'synthetic_workload'='true' AND e.kind='recognized'; SELECT 'synthetic_staff=' || count(*) FROM \"user\" WHERE login LIKE 'restore-staff-%'; SELECT 'identity_mismatches=' || count(*) FROM task t JOIN \"user\" u ON u.id=t.assignee_id JOIN event e ON e.task_id=t.id WHERE t.values->>'synthetic_workload'='true' AND (u.login <> ('restore-staff-' || lpad(((substring(t.case_number from 'А99-([0-9]+)/2026')::int - 1) % 20)::text, 2, '0')) OR e.kind <> 'recognized' OR e.user_id <> t.assignee_id);")"
expected_evidence=$'revision=a6c2d8e41b90\ncanary=1\nworkload_tasks='"$drill_task_count"$'\nworkload_events='"$drill_task_count"$'\nsynthetic_staff=20\nidentity_mismatches=0'
if [[ "$restore_evidence" != "$expected_evidence" ]]; then
  echo "Restored database did not contain the expected schema and synthetic workflow row" >&2
  echo "Expected: $expected_evidence" >&2
  echo "Observed: $restore_evidence" >&2
  exit 1
fi

echo "$restore_evidence"

verify_workload_document_links() {
  local database="$1"
  local document_evidence
  local expected_document_evidence
  document_evidence="$(docker exec "$container_name" psql -U postgres -d "$database" -Atc \
    "SELECT count(*) FROM document WHERE filename LIKE 'synthetic-workload-%'; SELECT count(DISTINCT d.id) FROM task t JOIN document d ON d.id=t.document_id WHERE t.values->>'synthetic_workload'='true'; SELECT count(DISTINCT d.content_hash) FROM task t JOIN document d ON d.id=t.document_id WHERE t.values->>'synthetic_workload'='true'; SELECT count(*) FROM task t JOIN document d ON d.id=t.document_id JOIN \"user\" u ON u.id=t.assignee_id WHERE t.values->>'synthetic_workload'='true' AND (d.uploaded_by_id <> t.assignee_id OR u.login <> ('restore-staff-' || lpad(((substring(t.case_number from 'А99-([0-9]+)/2026')::int - 1) % 20)::text, 2, '0')) OR d.filename <> ('synthetic-workload-' || lpad((substring(t.case_number from 'А99-([0-9]+)/2026')::int)::text, 6, '0') || '.pdf') OR d.content_hash !~ '^[0-9a-f]{64}$');")"
  expected_document_evidence="${drill_task_count}"$'\n'"${drill_task_count}"$'\n'"${drill_task_count}"$'\n0'
  if [[ "$document_evidence" != "$expected_document_evidence" ]]; then
    echo "Synthetic document verification differed in $database (expected ${drill_task_count}/${drill_task_count}/${drill_task_count}/0; observed $document_evidence)" >&2
    exit 1
  fi
}

verify_workload_document_links restore_target

# Simulate the previous baseline schema on the restored copy: its document hash
# index was non-unique, and it did not contain the lease table/revision marker.
# Add one synthetic duplicate to prove the uniqueness migration fails safely.
docker exec "$container_name" psql -v ON_ERROR_STOP=1 -U postgres -d restore_target -c \
  "DROP TABLE debtor_card_draft_revision; DROP TABLE assignment_draft_revision; DROP TABLE assignment_draft; DROP TABLE debtor_card_draft; DROP FUNCTION public.sro_record_debtor_card_revision(); DROP FUNCTION public.sro_record_assignment_draft_revision(); DROP FUNCTION public.sro_reject_assignment_draft_revision_mutation(); DROP FUNCTION public.sro_require_assignment_draft_revision_trigger();" >/dev/null
docker exec "$container_name" psql -U postgres -d restore_target -c \
  "DROP TRIGGER user_revoke_login_sessions_on_deactivation ON \"user\"; DROP FUNCTION public.sro_revoke_sessions_on_user_deactivation(); DROP TRIGGER login_session_write_guard ON login_session; DROP FUNCTION public.sro_guard_login_session_write(); DROP TRIGGER task_status_history_require_transition ON task_status_history; DROP FUNCTION public.sro_require_triggered_status_history_insert(); DROP TRIGGER task_status_history_record_change ON task; DROP TRIGGER task_status_history_no_truncate ON task_status_history; DROP TRIGGER task_status_history_append_only ON task_status_history; DROP FUNCTION public.sro_record_task_status_change(); DROP FUNCTION public.sro_reject_task_status_history_mutation(); DROP TABLE task_status_history; DROP TABLE login_session; DROP TRIGGER task_status_transition_guard ON task; DROP FUNCTION public.sro_enforce_task_status_transition(); DROP TRIGGER event_no_truncate ON event; DROP TRIGGER event_append_only ON event; DROP TRIGGER event_contract_validate ON event; DROP FUNCTION public.sro_reject_event_mutation(); DROP FUNCTION public.sro_validate_event_insert(); DROP INDEX ix_document_content_hash; CREATE INDEX ix_document_content_hash ON document (content_hash); ALTER TABLE document DROP COLUMN IF EXISTS source_metadata; INSERT INTO document (filename, stored_path, content_hash, source) SELECT 'restore-duplicate-probe', '/tmp/restore-duplicate-probe', content_hash, source FROM document ORDER BY id LIMIT 1; DROP TABLE job_outbox; DROP TABLE task_lease; ALTER TABLE task DROP COLUMN intake_item_id; DROP TABLE intake_item; ALTER TABLE task DROP COLUMN intake_operation_id; DROP TABLE intake_operation; DROP TABLE alembic_version" >/dev/null
adoption_url="postgresql+psycopg://postgres@127.0.0.1:${host_port}/restore_target"
legacy_worker_revision="526508f02f4031607f087cef80a6e0c741fa1a94"
legacy_worker_root="$upload_dir/legacy-worker-source"
mkdir -p "$legacy_worker_root"
git -C "$repo_root" archive "$legacy_worker_revision" app sro_robot \
  | tar -x -C "$legacy_worker_root"
printf 'rolling_worker_source_revision=%s\n' "$legacy_worker_revision"
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
      "$schema_check" != *"Table('debtor_card_draft'"* || \
      "$schema_check" != *"Table('assignment_draft'"* || \
      "$schema_check" != *"Table('assignment_draft_revision'"* || \
      "$schema_check" != *"append-only event trigger function missing or ambiguous"* || \
      "$schema_check" != *"append-only event trigger set missing or ambiguous"* || \
      "$schema_check" != *"runtime trigger function set missing or ambiguous"* || \
      "$schema_check" != *"user deactivation trigger function missing or ambiguous"* || \
      "$schema_check" != *"user deactivation trigger missing or ambiguous"* || \
      "$schema_check" != *"active-account session trigger function missing or ambiguous"* || \
      "$schema_check" != *"active-account session trigger missing or ambiguous"* || \
      "$schema_check" != *"task status trigger function missing or ambiguous"* || \
      "$schema_check" != *"task status trigger missing or ambiguous"* || \
      "$schema_check" != *"task status history trigger functions missing or ambiguous"* || \
      "$schema_check" != *"task status history trigger set missing or ambiguous"* || \
      "$schema_check" != *"task status history insert guard function missing or ambiguous"* || \
      "$schema_check" != *"task status history insert trigger missing or ambiguous"* || \
      "$schema_check" != *"ix_document_content_hash"* || \
      "$schema_check" != *"Table('task_lease'"* || \
      "$schema_check" != *"Index('ix_task_lease_lease_until'"* || \
      "$schema_check" != *"Table('intake_operation'"* || \
      "$schema_check" != *"Table('job_outbox'"* || \
      "$schema_check" != *"Table('task_status_history'"* || \
      "$schema_check" != *"Table('login_session'"* || \
      "$schema_check" != *"Table('intake_item'"* || \
      "$schema_check" != *"missing sequence login_session_id_seq"* || \
      "$schema_check" != *"missing sequence task_status_history_id_seq"* || \
      "$schema_check" != *"intake_operation_id"* || \
      "$schema_check" != *"intake_item_id"* || \
      "$schema_check" != *"source_metadata_sha256"* || \
      "$schema_check" != *"fk_task_intake_item_context"* ]]; then
  echo "Restored old schema did not report the expected lease/index drift" >&2
  echo "$schema_check" >&2
  exit 1
fi

# Run the legacy feature-disabled handler while the clone still has only the
# baseline schema. A later process will read its result after additive upgrade.
DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
OCR_TASK_LEASES_ENABLED=0 \
SRO_EXPECT_WORKER_SOURCE_ROOT="$legacy_worker_root" PYTHONPATH="$legacy_worker_root" \
  "$python_bin" scripts/dev/legacy_worker_schema_smoke.py run

# Keep the pinned pre-lease worker source and a pooled connection loaded against
# the old schema, alive through the failed migration and additive upgrade.
rolling_worker_command="$upload_dir/rolling-worker-command"
rolling_worker_log="$upload_dir/rolling-worker.log"
mkfifo -m 600 "$rolling_worker_command"
exec 9<>"$rolling_worker_command"
rolling_worker_fd_open=1
DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
OCR_TASK_LEASES_ENABLED=0 \
SRO_EXPECT_WORKER_SOURCE_ROOT="$legacy_worker_root" PYTHONPATH="$legacy_worker_root" \
  "$python_bin" scripts/dev/legacy_worker_schema_smoke.py serve-across-upgrade \
  <"$rolling_worker_command" 9>&- >"$rolling_worker_log" 2>&1 &
rolling_worker_pid=$!
rolling_worker_ready=0
for attempt in {1..100}; do
  if rg -q '^rolling_worker_ready$' "$rolling_worker_log"; then
    rolling_worker_ready=1
    break
  fi
  if ! kill -0 "$rolling_worker_pid" 2>/dev/null; then
    cat "$rolling_worker_log" >&2
    echo "Long-lived legacy worker did not start against the old schema" >&2
    exit 1
  fi
  sleep 0.1
done
if [[ "$rolling_worker_ready" != 1 ]]; then
  cat "$rolling_worker_log" >&2
  echo "Long-lived legacy worker did not become ready in time" >&2
  exit 1
fi

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
  "SELECT 'revision=' || version_num FROM alembic_version; SELECT 'duplicate_excess=' || coalesce(sum(row_count - 1), 0) FROM (SELECT count(*) AS row_count FROM document GROUP BY content_hash HAVING count(*) > 1) AS duplicate_hashes; SELECT 'hash_index_unique=' || indisunique FROM pg_index WHERE indexrelid = 'ix_document_content_hash'::regclass; SELECT 'lease_table=' || (to_regclass('public.task_lease') IS NOT NULL); SELECT 'intake_table=' || (to_regclass('public.intake_operation') IS NOT NULL); SELECT 'outbox_table=' || (to_regclass('public.job_outbox') IS NOT NULL); SELECT 'login_session_table=' || (to_regclass('public.login_session') IS NOT NULL); SELECT 'intake_item_table=' || (to_regclass('public.intake_item') IS NOT NULL); SELECT 'source_metadata=' || EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='document' AND column_name='source_metadata');")"
expected_failed_migration_state=$'revision=bc518c7514d3\nduplicate_excess=1\nhash_index_unique=false\nlease_table=false\nintake_table=false\noutbox_table=false\nlogin_session_table=false\nintake_item_table=false\nsource_metadata=false'
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
if ! retry_migration_output="$(DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" -m app.cli init-db 2>&1)"; then
  echo "Retry after removing the synthetic duplicate did not reach migration head" >&2
  echo "$retry_migration_output" >&2
  exit 1
fi
printf 'recognize\n' >&9
rolling_worker_completed=0
if "$python_bin" scripts/dev/worker_marker_wait.py \
  "$rolling_worker_pid" "$rolling_worker_log" \
  rolling_worker_recognition_completed 30; then
  rolling_worker_completed=1
fi
if [[ "$rolling_worker_completed" != 1 ]]; then
  kill "$rolling_worker_pid" 2>/dev/null || true
  wait "$rolling_worker_pid" 2>/dev/null || true
  rolling_worker_pid=""
  exec 9>&-
  rolling_worker_fd_open=0
  cat "$rolling_worker_log" >&2
  echo "Long-lived legacy worker did not complete within 30 seconds" >&2
  exit 1
fi
if ! wait "$rolling_worker_pid"; then
  cat "$rolling_worker_log" >&2
  echo "Long-lived legacy worker failed after the additive schema upgrade" >&2
  exit 1
fi
rolling_worker_pid=""
exec 9>&-
rolling_worker_fd_open=0
DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
OCR_TASK_LEASES_ENABLED=0 PYTHONPATH="$repo_root" \
  "$python_bin" scripts/dev/legacy_worker_schema_smoke.py verify
DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
OCR_TASK_LEASES_ENABLED=0 PYTHONPATH="$repo_root" \
  "$python_bin" scripts/dev/legacy_worker_schema_smoke.py verify-rolling
echo "Long-lived feature-disabled worker completed after the additive schema upgrade"
verify_workload_document_links restore_target
adoption_evidence="$(docker exec "$container_name" psql -U postgres -d restore_target -Atc \
  "SELECT 'revision=' || version_num FROM alembic_version; SELECT 'canary=' || count(*) FROM task t JOIN document d ON d.id=t.document_id JOIN event e ON e.task_id=t.id WHERE t.values->>'marker'='restore-canary' AND e.kind='recognized'; SELECT 'workload_tasks=' || count(*) FROM task WHERE values->>'synthetic_workload'='true'; SELECT 'workload_events=' || count(*) FROM event e JOIN task t ON t.id=e.task_id WHERE t.values->>'synthetic_workload'='true' AND e.kind='recognized'; SELECT 'synthetic_staff=' || count(*) FROM \"user\" WHERE login LIKE 'restore-staff-%'; SELECT 'identity_mismatches=' || count(*) FROM task t JOIN \"user\" u ON u.id=t.assignee_id JOIN event e ON e.task_id=t.id WHERE t.values->>'synthetic_workload'='true' AND (u.login <> ('restore-staff-' || lpad(((substring(t.case_number from 'А99-([0-9]+)/2026')::int - 1) % 20)::text, 2, '0')) OR e.kind <> 'recognized' OR e.user_id <> t.assignee_id); SELECT 'lease_table=' || (to_regclass('public.task_lease') IS NOT NULL); SELECT 'outbox_table=' || (to_regclass('public.job_outbox') IS NOT NULL); SELECT 'login_session_table=' || (to_regclass('public.login_session') IS NOT NULL); SELECT 'intake_item_table=' || (to_regclass('public.intake_item') IS NOT NULL); SELECT 'intake_metadata_constraint=' || (SELECT count(*) FROM pg_constraint WHERE conrelid='public.intake_operation'::regclass AND conname='ck_intake_operation_source_metadata_sha256');")"
expected_adoption_evidence=$'revision=a6c2d8e41b90\ncanary=1\nworkload_tasks='"$drill_task_count"$'\nworkload_events='"$drill_task_count"$'\nsynthetic_staff=20\nidentity_mismatches=0\nlease_table=true\noutbox_table=true\nlogin_session_table=true\nintake_item_table=true\nintake_metadata_constraint=1'

if [[ "$adoption_evidence" != "$expected_adoption_evidence" ]]; then
  echo "Adoption rehearsal changed or lost the synthetic workflow canary" >&2
  exit 1
fi

adoption_signature_before_downgrade="$(database_row_signature restore_target)"
downgrade_output="$(DATABASE_URL="$adoption_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  "$python_bin" - <<'PY'
from alembic import command
from alembic.config import Config
from sro_robot.config import ROOT

try:
    command.downgrade(Config(str(ROOT / "alembic.ini")), "base")
except RuntimeError as exc:
    expected = (
        "Audit event contracts must not be removed automatically",
        "Login session revocation records must not be removed automatically",
        "Intake source provenance must not be removed by downgrade",
        "Intake replay metadata must not be removed automatically",
        "Intake replay metadata validation must not be removed automatically",
        "Account deactivation session revocation must not be removed automatically",
        "Inactive-account session revocation must not be removed automatically",
        "Active-account login-session enforcement must not be removed automatically",
        "Task status history insert integrity must not be removed automatically",
        "Audit event payload requirements must not be removed automatically",
        "Audit event attribution requirements must not be removed automatically",
        "Task-scoped audit event linkage must not be removed automatically",
        "Retired public-source acquisition schema must not be restored automatically",
        "Internal assignment provenance is forward-only; preserve drafts and upgrade with a reviewed migration.",
        "Internal debtor and assignment history must not be dropped automatically",
        "Internal preparation provenance requires a forward-only migration",
    )
    if not str(exc).startswith(expected):
        raise
    print("data_downgrade=refused")
else:
    raise SystemExit("data-bearing downgrade unexpectedly completed")
PY
)"
if [[ "$downgrade_output" != "data_downgrade=refused" ]]; then
  echo "Data-bearing downgrade did not fail closed at the durable-intake migration" >&2
  exit 1
fi
adoption_signature_after_downgrade="$(database_row_signature restore_target)"
post_downgrade_state="$(docker exec "$container_name" psql -U postgres -d restore_target -Atc \
  "SELECT version_num FROM alembic_version; SELECT to_regclass('public.job_outbox') IS NOT NULL; SELECT to_regclass('public.task_lease') IS NOT NULL; SELECT indisunique FROM pg_index WHERE indexrelid='ix_document_content_hash'::regclass; SELECT count(*) FROM pg_trigger WHERE tgname IN ('event_append_only', 'event_no_truncate', 'event_contract_validate') AND NOT tgisinternal; SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname IN ('sro_reject_event_mutation', 'sro_validate_event_insert'); SELECT to_regclass('public.intake_operation') IS NOT NULL; SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname='task' AND t.tgname='task_status_transition_guard' AND NOT t.tgisinternal; SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname='sro_enforce_task_status_transition'; SELECT to_regclass('public.task_status_history') IS NOT NULL; SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname IN ('task', 'task_status_history') AND t.tgname IN ('task_status_history_record_change', 'task_status_history_append_only', 'task_status_history_no_truncate') AND NOT t.tgisinternal; SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname IN ('sro_record_task_status_change', 'sro_reject_task_status_history_mutation'); SELECT to_regclass('public.login_session') IS NOT NULL; SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND tablename='login_session' AND indexname='ix_login_session_user_expires'; SELECT count(*) FROM pg_constraint WHERE conrelid='public.login_session'::regclass AND conname='uq_login_session_token_sha256'; SELECT to_regclass('public.intake_item') IS NOT NULL; SELECT count(*) FROM pg_constraint WHERE conrelid='public.task'::regclass AND conname IN ('fk_task_intake_item_context', 'uq_task_intake_item_id'); SELECT count(*) FROM pg_constraint WHERE conrelid='public.intake_operation'::regclass AND conname='ck_intake_operation_source_metadata_sha256'; SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='intake_operation' AND column_name='source_metadata_sha256'); SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname='user' AND t.tgname='user_revoke_login_sessions_on_deactivation' AND NOT t.tgisinternal; SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname='sro_revoke_sessions_on_user_deactivation'; SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname='login_session' AND t.tgname='login_session_write_guard' AND NOT t.tgisinternal; SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname='sro_guard_login_session_write'; SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname='task_status_history' AND t.tgname='task_status_history_require_transition' AND NOT t.tgisinternal; SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname='sro_require_triggered_status_history_insert'; SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid WHERE t.typname='intake_source'; SELECT count(*) FROM pg_constraint WHERE conrelid='public.job_outbox'::regclass AND conname IN ('ck_job_outbox_safe_job_kind', 'ck_job_outbox_actor_scope'); SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='document' AND column_name='source_metadata');")"
if [[ "$adoption_signature_before_downgrade" != "$adoption_signature_after_downgrade" || \
      "$post_downgrade_state" != $'a6c2d8e41b90\nt\nt\nt\n3\n2\nt\n1\n1\nt\n3\n2\nt\n1\n1\nt\n2\n1\nt\n1\n1\n1\n1\n1\n1\nupload,batch,mailbox,folder,kad\n2\nf' ]]; then
  echo "Refused downgrade changed migrated data, revision or platform guards" >&2
  exit 1
fi
echo "Data-bearing downgrade refusal preserved all migrated rows and identity guards"

echo "Populated unversioned schema refusal, exact schema-drift comparison and isolated migration adoption passed"
echo "Disposable PostgreSQL backup/restore drill passed"

# Keep the claim probe last. Give each synthetic thread a bounded connection slot
# so slow runners test lease contention without timing out on the smaller app pool.
# Read-back still fails this drill if any synthetic task remains unclaimed.
DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
DB_POOL_SIZE="${SRO_LEASE_LOAD_WORKERS:-20}" DB_MAX_OVERFLOW=0 \
SRO_DISPOSABLE_DRILL=1 SRO_DRILL_CONTAINER_NAME="$container_name" \
  "$python_bin" scripts/dev/lease_claim_load_smoke.py

# Compare separate worker processes to the process-local pool probe above. Each
# child repeats the disposable-container identity gate and reports aggregates only.
if [[ "${SRO_LEASE_LOAD_MODE:-thread}" != "process" ]]; then
  DATABASE_URL="$database_url" APP_DEBUG=1 SRO_UPLOAD_DIR="$upload_dir" \
  SRO_DISPOSABLE_DRILL=1 SRO_DRILL_CONTAINER_NAME="$container_name" \
  SRO_LEASE_LOAD_MODE=process \
    "$python_bin" scripts/dev/lease_claim_load_smoke.py
fi
