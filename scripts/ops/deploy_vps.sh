#!/usr/bin/env bash
set -euo pipefail
set -o pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tag="${1:-}"
vps_host="${SRO_VPS_HOST:?Set SRO_VPS_HOST to the verified current production host}"
ssh_key="${SRO_VPS_SSH_KEY:-$HOME/.ssh/sro-office-codex}"

if [[ ! "$tag" =~ ^sro-v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Usage: scripts/ops/deploy_vps.sh sro-vMAJOR.MINOR.PATCH" >&2
    exit 2
fi
if [[ -n "$(git -C "$repo_root" status --porcelain --untracked-files=all)" ]]; then
    echo "Refusing deployment from a dirty worktree" >&2
    exit 2
fi
if [[ "$(git -C "$repo_root" cat-file -t "refs/tags/$tag" 2>/dev/null || true)" != tag ]]; then
    echo "Release must be an annotated tag" >&2
    exit 2
fi
commit="$(git -C "$repo_root" rev-parse "$tag^{commit}")"
head="$(git -C "$repo_root" rev-parse HEAD)"
if [[ "$head" != "$commit" ]]; then
    echo "Check out the exact tagged release commit before deployment" >&2
    exit 2
fi
git -C "$repo_root" fetch --quiet origin main
if [[ "$commit" != "$(git -C "$repo_root" rev-parse origin/main)" ]]; then
    echo "Release tag must point exactly to the published origin/main tip" >&2
    exit 2
fi
if ! python3 "$repo_root/scripts/ops/check_ssh_identity.py" "$ssh_key"; then
    echo "Dedicated VPS SSH identity is unavailable or unsafe" >&2
    exit 2
fi

cd "$repo_root"
SRO_ROBOT_PYTHON="${SRO_ROBOT_PYTHON:-$repo_root/.venv/bin/python}" \
    scripts/dev/verify.sh

ssh_args=(
    -i "$ssh_key"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=yes
    -o ConnectTimeout=25
    -o ServerAliveInterval=10
    -o ServerAliveCountMax=3
)
git archive --format=tar "$commit" | ssh "${ssh_args[@]}" "$vps_host" \
    "bash -c 'set -euo pipefail; release=/opt/sro-robot/releases/$commit; test ! -e \"\$release\"; install -d -o root -g root -m 0755 \"\$release\"; tar -xf - -C \"\$release\"; printf \"%s\\n\" $commit > \"\$release/.release-commit\"'"
ssh "${ssh_args[@]}" "$vps_host" bash -s -- "$commit" <<'REMOTE'
set -euo pipefail
commit="$1"
base=/opt/sro-robot
release="$base/releases/$commit"
venv="$base/venvs/$commit"
preview_was_enabled=0
if systemctl is-enabled --quiet sro-robot-preview.service; then preview_was_enabled=1; fi
install -d -m 0755 "$base/venvs" "$base/browsers"
install -d -o srorobot -g srorobot -m 0700 "$base/storage/previews" "$base/storage/browser-profiles"
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
REMOTE
