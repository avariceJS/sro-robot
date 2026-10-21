#!/usr/bin/env bash
set -euo pipefail

commit="${1:-}"
if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Invalid release commit" >&2
    exit 2
fi
if (( EUID != 0 )); then
    echo "Run release provisioning as root" >&2
    exit 2
fi

exec 9>/run/lock/sro-robot-provision.lock
if ! flock -n 9; then
    echo "Another release provisioning operation is active" >&2
    exit 2
fi

base=/opt/sro-robot
release="$base/releases/$commit"
venv="$base/venvs/$commit"
if ! python3 "$release/scripts/ops/release_preflight.py" --base "$base" --commit "$commit"; then
    exit 2
fi

preview_was_enabled=0
if systemctl is-enabled --quiet sro-robot-preview.service; then preview_was_enabled=1; fi
install -d -m 0755 "$base/venvs" "$base/browsers"
install -d -o srorobot -g srorobot -m 0700 "$base/storage/previews" "$base/storage/browser-profiles"
if [[ -e "$venv" || -L "$venv" ]]; then
    echo "Release virtual environment already exists; refusing replacement" >&2
    exit 2
fi
"$base/.venv/bin/python" -m venv "$venv"
"$venv/bin/python" -m pip install --require-hashes -r "$release/requirements-bootstrap.txt"
"$venv/bin/python" -m pip install --require-hashes -r "$release/requirements.txt"
"$venv/bin/python" -m pip check
PLAYWRIGHT_BROWSERS_PATH="$base/browsers" "$venv/bin/python" -m playwright install --with-deps chromium
sha256sum "$release/requirements.txt" | cut -d ' ' -f 1 > "$venv/.requirements-sha256"
ln -s "$venv" "$release/.venv"
install -o srorobot -g srorobot -m 0600 "$base/current/.env" "$release/.env"
chown -R root:root "$release"
chmod -R a-w "$release"
chown srorobot:srorobot "$release/.env"
chmod 0600 "$release/.env"
install -o root -g root -m 0750 "$release/scripts/ops/activate_vps_release.sh" /usr/local/sbin/sro-robot-activate-release
for unit in sro-robot-web.service sro-robot-ocr@.service sro-robot-preview.service sro-robot-migrate.service sro-robot-backup.service sro-robot-backup.timer sro-robot-health.service sro-robot-health.timer; do
    install -o root -g root -m 0644 "$release/deploy/cloud/systemd/$unit" "/etc/systemd/system/$unit"
done
nginx_site="$(readlink -f /etc/nginx/sites-enabled/sro-office.conf)"
[[ "$nginx_site" == /etc/nginx/* ]]
cp -p "$nginx_site" "/var/backups/sro-robot/nginx-before-$commit.conf"
restore_proxy() {
    local code=$?
    trap - ERR
    if (( ! preview_was_enabled )); then systemctl disable sro-robot-preview.service; fi
    # A failed database rollback deliberately leaves ingress closed. Restoring
    # a legacy proxy without the maintenance check would reopen it unsafely.
    if [[ ! -e /run/sro-robot-maintenance ]]; then
        cp -p "/var/backups/sro-robot/nginx-before-$commit.conf" "$nginx_site"
        nginx -t && systemctl reload nginx
    fi
    exit "$code"
}
trap restore_proxy ERR
sed 's/__SRO_DOMAIN__/sro-office.ru/g' "$release/deploy/cloud/nginx.conf.template" > "$nginx_site"
if ! nginx -t; then
    cp -p "/var/backups/sro-robot/nginx-before-$commit.conf" "$nginx_site"
    exit 1
fi
systemctl reload nginx
systemctl daemon-reload
systemctl enable sro-robot-web.service sro-robot-ocr@1.service sro-robot-preview.service sro-robot-backup.timer sro-robot-health.timer
systemctl start --now sro-robot-backup.timer sro-robot-health.timer
/usr/local/sbin/sro-robot-activate-release "$commit"
trap - ERR
