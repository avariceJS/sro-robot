#!/usr/bin/env bash
set -euo pipefail
set -o pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tag="${1:-}"
vps_host="${SRO_VPS_HOST:-root@85.192.30.98}"
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
if ! git -C "$repo_root" merge-base --is-ancestor "$commit" origin/main; then
    echo "Release tag must point to a commit already published on origin/main" >&2
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
)
git archive --format=tar "$commit" | ssh "${ssh_args[@]}" "$vps_host" \
    "bash -c 'set -euo pipefail; release=/opt/sro-robot/releases/$commit; test ! -e \"\$release\"; install -d -o root -g root -m 0755 \"\$release\"; tar -xf - -C \"\$release\"; ln -s /opt/sro-robot/.venv \"\$release/.venv\"; install -o srorobot -g srorobot -m 0600 /opt/sro-robot/current/.env \"\$release/.env\"; printf \"%s\\\\n\" $commit > \"\$release/.release-commit\"; chown -R root:root \"\$release\"; chmod -R a-w \"\$release\"; chown srorobot:srorobot \"\$release/.env\"; chmod 0600 \"\$release/.env\"'"
ssh "${ssh_args[@]}" "$vps_host" \
    "set -e; release=/opt/sro-robot/releases/$commit; install -o root -g root -m 0750 \"\$release/scripts/ops/activate_vps_release.sh\" /usr/local/sbin/sro-robot-activate-release; for unit in sro-robot-web.service sro-robot-ocr@.service sro-robot-migrate.service sro-robot-backup.service sro-robot-backup.timer sro-robot-health.service sro-robot-health.timer; do install -o root -g root -m 0644 \"\$release/deploy/cloud/systemd/\$unit\" \"/etc/systemd/system/\$unit\"; done; systemctl daemon-reload; systemctl enable sro-robot-web.service sro-robot-ocr@1.service sro-robot-backup.timer sro-robot-health.timer; /usr/local/sbin/sro-robot-activate-release '$commit'; systemctl start --now sro-robot-backup.timer sro-robot-health.timer"
