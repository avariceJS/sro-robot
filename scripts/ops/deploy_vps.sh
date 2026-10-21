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
if git -C "$repo_root" ls-tree -r --name-only "$commit" | grep -E '(^|/)\.env$' >/dev/null; then
    echo "Release source must not contain a private environment file" >&2
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
ssh "${ssh_args[@]}" "$vps_host" \
    "bash /opt/sro-robot/releases/$commit/scripts/ops/provision_vps_release.sh $commit"
