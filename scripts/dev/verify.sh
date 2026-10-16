#!/usr/bin/env bash

# Run the local deterministic suite and disposable PostgreSQL migration/restore drill.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_bin="${SRO_ROBOT_PYTHON:-$repo_root/.venv/bin/python}"

cd "$repo_root"
if [[ ! -x "$python_bin" ]]; then
  echo "Python executable not found: $python_bin" >&2
  exit 2
fi

APP_DEBUG=1 \
APP_SECRET_KEY='synthetic-verification-session-key-0000000000000000' \
DATABASE_URL='postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro' \
MIGRATION_DATABASE_URL='postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/sro' \
REDIS_URL='redis://synthetic:synthetic@127.0.0.1:1/0' \
    "$python_bin" -m unittest discover -s tests -v
"$python_bin" -m compileall -q app sro_robot tests
node --test tests/*.mjs
git diff --check
bash -n scripts/dev/postgres-restore-drill.sh
bash -n scripts/dev/postgres-runtime-role-smoke.sh
bash scripts/dev/postgres-runtime-role-smoke.sh
bash scripts/dev/postgres-restore-drill.sh

echo "SRO Robot local verification passed"
