#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_bin="${SRO_ROBOT_PYTHON:-$repo_root/.venv/bin/python}"
postgres_name="sro-robot-demo-postgres"
redis_name="sro-robot-demo-redis"
network_name="sro-robot-demo-net"
postgres_port="${SRO_DEMO_POSTGRES_PORT:-55433}"
redis_port="${SRO_DEMO_REDIS_PORT:-56380}"
web_port="${SRO_DEMO_WEB_PORT:-8000}"
demo_tmp=""
worker_pid=""
web_pid=""
network_created=0
postgres_created=0
redis_created=0

cleanup() {
    set +e
    if [[ -n "$web_pid" ]]; then
        kill -TERM "$web_pid" 2>/dev/null
        wait "$web_pid" 2>/dev/null
    fi
    if [[ -n "$worker_pid" ]]; then
        kill -TERM "$worker_pid" 2>/dev/null
        wait "$worker_pid" 2>/dev/null
    fi
    if [[ "$postgres_created" == 1 ]]; then docker rm -f "$postgres_name" >/dev/null 2>&1; fi
    if [[ "$redis_created" == 1 ]]; then docker rm -f "$redis_name" >/dev/null 2>&1; fi
    if [[ "$network_created" == 1 ]]; then docker network rm "$network_name" >/dev/null 2>&1; fi
    if [[ -n "$demo_tmp" ]]; then
        rm -rf -- "$demo_tmp"
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -x "$python_bin" ]]; then
    echo "Python environment not found: $python_bin" >&2
    exit 2
fi
if [[ ! -t 0 ]]; then
    echo "Run this command in an interactive terminal so the demo password can be entered privately" >&2
    exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "Docker must be installed and running for the isolated demo" >&2
    exit 2
fi
if ! command -v tesseract >/dev/null 2>&1 || ! command -v pdftotext >/dev/null 2>&1 \
    || ! command -v pdftoppm >/dev/null 2>&1; then
    echo "Install local Tesseract with Russian data and Poppler before the demo" >&2
    exit 2
fi
if ! command -v nc >/dev/null 2>&1; then
    echo "The netcat (nc) command is required to check local demo ports" >&2
    exit 2
fi
for resource in "container:$postgres_name" "container:$redis_name" "network:$network_name"; do
    kind="${resource%%:*}"
    name="${resource#*:}"
    if [[ "$kind" == container ]] && docker container inspect "$name" >/dev/null 2>&1; then
        echo "Demo resource already exists; refusing to touch it: $resource" >&2
        exit 2
    elif [[ "$kind" == network ]] && docker network inspect "$name" >/dev/null 2>&1; then
        echo "Demo resource already exists; refusing to touch it: $resource" >&2
        exit 2
    fi
done

demo_tmp="$(mktemp -d "${TMPDIR:-/tmp}/sro-robot-demo.XXXXXX")"
chmod 700 "$demo_tmp"
export APP_DEBUG=1
export DATABASE_URL="postgresql+psycopg://sro_demo@127.0.0.1:${postgres_port}/sro_demo"
unset MIGRATION_DATABASE_URL
export REDIS_URL="redis://127.0.0.1:${redis_port}/0"
export SRO_UPLOAD_DIR="$demo_tmp/documents"
export OCR_TASK_LEASES_ENABLED=0
export SRO_LOGIN=""
export SRO_PASSWORD=""
export PYTHONDONTWRITEBYTECODE=1

"$python_bin" - <<'PY'
from app.db.base import engine
from app.settings import settings
print("Application configuration loaded for the local demo")
engine.dispose()
PY

docker network create "$network_name" >/dev/null
network_created=1
docker run --name "$postgres_name" --network "$network_name" --rm -d \
    -e POSTGRES_USER=sro_demo \
    -e POSTGRES_DB=sro_demo \
    -e POSTGRES_HOST_AUTH_METHOD=trust \
    --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=1g \
    -p "127.0.0.1:${postgres_port}:5432" \
    postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea \
    >/dev/null
postgres_created=1
docker run --name "$redis_name" --network "$network_name" --rm -d \
    -p "127.0.0.1:${redis_port}:6379" \
    redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499 \
    redis-server --save '' --appendonly no \
    >/dev/null
redis_created=1

ready=0
for _ in $(seq 1 45); do
    if docker exec "$postgres_name" pg_isready -U sro_demo -d sro_demo >/dev/null 2>&1 \
        && docker exec "$redis_name" redis-cli ping 2>/dev/null | rg -q '^PONG$' \
        && nc -z 127.0.0.1 "$postgres_port" >/dev/null 2>&1 \
        && nc -z 127.0.0.1 "$redis_port" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
if [[ "$ready" != 1 ]]; then
    echo "The isolated demo database or queue did not become ready" >&2
    exit 2
fi

cd "$repo_root"
"$python_bin" -m app.cli init-db
"$python_bin" -m app.cli create-user demo --name 'Демо-сотрудник'
"$python_bin" scripts/dev/create_demo_document.py out/DEMO_ONLY_synthetic_ruling.pdf
"$python_bin" scripts/dev/run_worker.py ocr sro &
worker_pid="$!"
"$python_bin" -m uvicorn app.main:app --host 127.0.0.1 --port "$web_port" --no-access-log &
web_pid="$!"

ready=0
for _ in $(seq 1 45); do
    if curl --silent --fail "http://127.0.0.1:${web_port}/readyz" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
if [[ "$ready" != 1 ]]; then
    echo "The demo web app did not become ready; check the service output above" >&2
    exit 2
fi

cat <<EOF

Local demo is ready at http://127.0.0.1:${web_port}/
Login: demo
Use the temporary password entered above.
Synthetic scan: ${repo_root}/out/DEMO_ONLY_synthetic_ruling.pdf
PostgreSQL and Redis are isolated, in-memory, and bound to loopback.
Press Ctrl+C to stop the app and remove the temporary database and queue.
EOF

wait "$web_pid"
