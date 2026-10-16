#!/usr/bin/env bash
set -euo pipefail

commit="${1:-}"
base=/opt/sro-robot
release="$base/releases/$commit"
current="$base/current"
temporary_link="$base/.current.$commit"
maintenance=/run/sro-robot-maintenance
old_target="$(readlink -f "$current")"
checkpoint="/var/backups/sro-robot/release-${commit}"
switched=0
paused=0
old_preview_active=0
if systemctl is-active --quiet sro-robot-preview.service; then old_preview_active=1; fi
exec 8>/run/lock/sro-robot-release.lock
flock -n 8

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
if [[ "$(readlink -f "$release/.venv")" != "$base/venvs/$commit" || ! -x "$release/.venv/bin/python" ]]; then
    echo "A separately qualified release environment is required" >&2
    exit 2
fi
[[ "$(sha256sum "$release/requirements.txt" | cut -d ' ' -f 1)" == "$(cat "$release/.venv/.requirements-sha256")" ]]
"$release/.venv/bin/python" -m pip check >/dev/null
status_file="$(mktemp /run/sro-robot-release-status.XXXXXX)"
chmod 0600 "$status_file"
cleanup() { rm -f "$status_file" "$temporary_link"; }
ready() {
    for _ in $(seq 1 30); do
        if curl --fail --silent --max-time 5 http://127.0.0.1:8000/readyz --output /dev/null; then return 0; fi
        sleep 2
    done
    return 1
}
rollback() {
    local exit_code=$?
    trap - ERR
    set +e
    if (( paused )); then
        install -o root -g root -m 0644 /dev/null "$maintenance"
        systemctl stop sro-robot-preview.service sro-robot-ocr@1.service sro-robot-web.service
        if (( switched )); then
            if ! python3 "$release/scripts/ops/release_database.py" restore "$checkpoint"; then
                echo "Rollback database restore failed; ingress remains in maintenance" >&2
                cleanup
                exit 1
            fi
            ln -s "$old_target" "$temporary_link"
            mv -Tf "$temporary_link" "$current"
            install -o root -g root -m 0600 "$old_target/.env" /etc/sro-robot/migration-app.env
        fi
        systemctl start sro-robot-web.service sro-robot-ocr@1.service
        if (( old_preview_active )); then systemctl start sro-robot-preview.service; fi
        if ready; then
            rm -f "$maintenance"
            systemctl start sro-robot-health.timer
            echo "Release health check failed; pointer restored to $old_target" >&2
        else
            echo "Old release is not ready; ingress remains in maintenance" >&2
        fi
    fi
    cleanup
    exit "$exit_code"
}
trap cleanup EXIT
trap rollback ERR

# Preflight uses the running release's exact schema policy, not the candidate's.
cd "$current"
runuser -u srorobot -- env APP_DEBUG=0 SRO_UPLOAD_DIR="$base/storage/documents" \
    "$current/.venv/bin/python" -m app.cli ops-status >"$status_file" 2>&1
grep -Eq '^Recognizing tasks without a lease( \(require reconciliation\))?: 0$' "$status_file"
grep -Eq '^Queue depth: ocr=0 sro=0 sro_preview=0$' "$status_file"
grep -Eq '^Registered RQ workers: ocr=[0-9]+ sro=0 sro_preview=[0-9]+$' "$status_file"
grep -Eq '^RQ registry entries \(cleanup disabled\): ocr=started=0,deferred=0,scheduled=0,failed=[0-9]+ sro=started=0,deferred=0,scheduled=0,failed=[0-9]+ sro_preview=started=0,' "$status_file"
runuser -u srorobot -- env APP_DEBUG=0 SRO_UPLOAD_DIR="$base/storage/documents" "$current/.venv/bin/python" -m app.cli schema-check >/dev/null

# Nginx checks this flag; all public writes are closed before the final backup.
install -o root -g root -m 0644 /dev/null "$maintenance"
paused=1
systemctl stop sro-robot-health.timer sro-robot-health.service sro-robot-preview.service sro-robot-ocr@1.service sro-robot-web.service
# An admitted request could enqueue between preflight and shutdown. Check again
# with ingress closed and every application writer stopped before checkpointing.
runuser -u srorobot -- env APP_DEBUG=0 SRO_UPLOAD_DIR="$base/storage/documents" \
    "$current/.venv/bin/python" -m app.cli ops-status >"$status_file" 2>&1
grep -Eq '^Queue depth: ocr=0 sro=0 sro_preview=0$' "$status_file"
systemctl start sro-robot-backup.service
python3 "$release/scripts/ops/release_database.py" snapshot "$checkpoint"
install -o root -g root -m 0600 "$release/.env" /etc/sro-robot/migration-app.env
ln -s "$release" "$temporary_link"
mv -Tf "$temporary_link" "$current"
switched=1
cd "$release"
systemctl start sro-robot-migrate.service
systemctl start sro-robot-web.service
ready
systemctl start sro-robot-ocr@1.service sro-robot-preview.service
systemctl start sro-robot-health.service
systemctl start sro-robot-health.timer
rm -f "$maintenance"
switched=0
paused=0
echo "Deployed and ready: $commit"
