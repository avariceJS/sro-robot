#!/usr/bin/env bash
set -euo pipefail

commit="${1:-}"
base=/opt/sro-robot
release="$base/releases/$commit"
current="$base/current"
temporary_link="$base/.current.$commit"
old_target="$(readlink "$current")"
switched=0

if [[ ! "$commit" =~ ^[0-9a-f]{40}$ || ! -d "$release" ]]; then
    echo "Invalid or missing release directory" >&2
    exit 2
fi
if [[ "$(cat "$release/.release-commit")" != "$commit" || ! -f "$release/.env" ]]; then
    echo "Release metadata or private app environment is missing" >&2
    exit 2
fi
if [[ "$(stat -c %u "$release")" != 0 || "$(stat -c %U:%G:%a "$release/.env")" != "srorobot:srorobot:600" ]]; then
    echo "Release ownership or environment permissions are unsafe" >&2
    exit 2
fi
if ! cmp -s "$base/current/requirements.txt" "$release/requirements.txt"; then
    echo "Dependency changes require a separately qualified release environment" >&2
    exit 2
fi
if [[ ! -x "$release/.venv/bin/python" ]]; then
    echo "Release environment is incomplete" >&2
    exit 2
fi
cd "$release"

status_file="$(mktemp /run/sro-robot-release-status.XXXXXX)"
chmod 0600 "$status_file"
cleanup() { rm -f "$status_file" "$temporary_link"; }
rollback() {
    local exit_code=$?
    trap - ERR
    if (( switched )); then
        systemctl stop sro-robot-ocr@1.service sro-robot-web.service || true
        ln -s "$old_target" "$temporary_link"
        mv -Tf "$temporary_link" "$current"
        systemctl start sro-robot-web.service
        systemctl start sro-robot-ocr@1.service
        echo "Release health check failed; pointer restored to $old_target" >&2
    fi
    cleanup
    exit "$exit_code"
}
trap cleanup EXIT
trap rollback ERR

systemctl start sro-robot-backup.service
runuser -u srorobot -- env APP_DEBUG=0 \
    SRO_UPLOAD_DIR="$base/storage/documents" \
    "$release/.venv/bin/python" -m app.cli ops-status >"$status_file" 2>&1 || true
grep -Eq '^Recognizing tasks without a lease: 0$' "$status_file"
grep -Eq '^Queue depth: ocr=0 sro=0 sro_preview=[0-9]+$' "$status_file"
grep -Eq '^Registered RQ workers: ocr=[0-9]+ sro=0 sro_preview=0$' "$status_file"
grep -Eq '^RQ registry entries \(cleanup disabled\): ocr=started=0,deferred=0,scheduled=0,failed=[0-9]+ ' "$status_file"
runuser -u srorobot -- env APP_DEBUG=0 \
    SRO_UPLOAD_DIR="$base/storage/documents" \
    "$release/.venv/bin/python" -m app.cli db-role-check >/dev/null
runuser -u srorobot -- env APP_DEBUG=0 \
    SRO_UPLOAD_DIR="$base/storage/documents" \
    "$release/.venv/bin/python" -m app.cli schema-check >/dev/null

install -o root -g root -m 0600 "$release/.env" /etc/sro-robot/migration-app.env
systemctl stop sro-robot-ocr@1.service sro-robot-web.service
ln -s "$release" "$temporary_link"
mv -Tf "$temporary_link" "$current"
switched=1
systemctl start sro-robot-migrate.service
systemctl start sro-robot-web.service
ready=0
for _ in $(seq 1 30); do
    if curl --fail --silent --max-time 5 http://127.0.0.1:8000/readyz --output /dev/null; then
        ready=1
        break
    fi
    sleep 2
done
(( ready == 1 ))
systemctl start sro-robot-ocr@1.service
systemctl start sro-robot-health.service
echo "Deployed and ready: $commit"
switched=0
