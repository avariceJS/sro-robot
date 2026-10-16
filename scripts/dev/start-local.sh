#!/usr/bin/env bash
set -u

node scripts/dev/start-local.mjs &
launcher_pid=$!

forward_signal() {
  signal="$1"
  trap '' INT TERM
  kill -s "$signal" "$launcher_pid" 2>/dev/null || true
  while true; do
    wait "$launcher_pid"
    status=$?
    if ! kill -0 "$launcher_pid" 2>/dev/null; then
      exit "$status"
    fi
    sleep 0.2 || true
  done
}

trap 'forward_signal INT' INT
trap 'forward_signal TERM' TERM
trap 'forward_signal HUP' HUP
wait "$launcher_pid"
status=$?
trap - INT TERM HUP
exit "$status"
