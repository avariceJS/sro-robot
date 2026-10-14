#!/usr/bin/env bash

# Read-only inventory for the Ubuntu 24.04 cloud host. This script never reads
# secret values, starts/stops services, changes files, migrates, backs up, or
# submits/retries work.

set -uo pipefail

readonly APP_ROOT=/opt/sro-robot
readonly SERVICE_USER=srorobot

if (( EUID != 0 )); then
  printf 'Run this inventory as root.\n' >&2
  exit 2
fi

printf 'SRO Robot cloud preflight (read-only)\n'
printf 'host=%s\n' "$(hostname -f 2>/dev/null || hostname)"
printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

section() {
  printf '\n[%s]\n' "$1"
}

run_check() {
  local label=$1
  shift
  local output status
  if output=$("$@" 2>&1); then
    status=0
  else
    status=$?
  fi
  printf '%s (exit=%s)\n' "$label" "$status"
  if [[ -n "$output" ]]; then
    printf '%s\n' "$output"
  fi
}

show_path_metadata() {
  local label=$1
  local path=$2
  if [[ -e "$path" || -L "$path" ]]; then
    local output status
    if output=$(stat -c 'owner=%U group=%G mode=%a type=%F' -- "$path" 2>&1); then
      status=0
    else
      status=$?
    fi
    printf '%s (exit=%s): %s\n' "$label" "$status" "$output"
  else
    printf '%s: missing\n' "$label"
  fi
}

section 'Repository'
if [[ -d "$APP_ROOT/.git" ]]; then
  run_check 'git HEAD' git -C "$APP_ROOT" rev-parse HEAD
  run_check 'git status' env GIT_OPTIONAL_LOCKS=0 git -C "$APP_ROOT" status --short --branch
else
  printf 'repository metadata unavailable\n'
fi

section 'Private file and storage metadata (values are never read)'
show_path_metadata '.env' "$APP_ROOT/.env"
show_path_metadata 'document directory' "$APP_ROOT/storage/documents"
run_check 'service account UID/GID' id "$SERVICE_USER"

section 'Service state'
for unit in \
  sro-robot-infra.service \
  sro-robot-web.service \
  sro-robot-ocr.service \
  sro-robot-ocr@1.service \
  nginx.service \
  docker.service; do
  if systemctl is-active --quiet "$unit"; then
    printf '%s=active\n' "$unit"
  else
    printf '%s=inactive-or-failed\n' "$unit"
  fi
done

section 'Listening sockets (addresses only; no process arguments)'
if command -v ss >/dev/null 2>&1; then
  ss -H -ltn 2>/dev/null | awk '$4 ~ /:(80|443|8000|55432|56379)$/ {print $4}' | sort -u
else
  printf 'ss unavailable\n'
fi

section 'Versions and host capacity'
run_check 'operating system' sh -c '. /etc/os-release 2>/dev/null; printf "%s %s\n" "${PRETTY_NAME:-unknown}" "${VERSION_ID:-unknown}"'
run_check 'kernel' uname -sr
run_check 'CPU count' nproc
run_check 'memory' free -h
run_check 'filesystem capacity' df -h "$APP_ROOT"
run_check 'Python' "$APP_ROOT/.venv/bin/python" --version
run_check 'Docker Compose' docker compose version
run_check 'Nginx' nginx -v
run_check 'Tesseract' sh -c 'tesseract --version 2>/dev/null | head -n 1'
run_check 'Poppler' sh -c 'pdftoppm -v 2>&1 | head -n 1'

section 'Application diagnostics'
printf 'app_checks=not_run\n'
cat <<'NOTICE'
Skipped: app.cli imports production settings, whose startup helper applies
fchmod(0700) to the document directory, so invocation is not strictly
read-only. Once that filesystem effect is accepted, run these separately as
srorobot with APP_DEBUG=0:
  runuser -u srorobot -- env APP_DEBUG=0 /opt/sro-robot/.venv/bin/python -m app.cli ops-status
  runuser -u srorobot -- env APP_DEBUG=0 /opt/sro-robot/.venv/bin/python -m app.cli db-role-check
  runuser -u srorobot -- env APP_DEBUG=0 /opt/sro-robot/.venv/bin/python -m app.cli schema-check
NOTICE

printf '\nInventory complete; no service or application command was started.\n'
