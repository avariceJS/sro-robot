#!/usr/bin/env bash
# Root-side alternative transport for the existing checked-in local release gate.
# This script fetches only an already-published annotated release tag and main.
set -euo pipefail
umask 022

tag="${1:-}"
expected_commit="${2:-}"
if [[ ! "$tag" =~ ^sro-v[0-9]+\.[0-9]+\.[0-9]+$ || ! "$expected_commit" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Usage: deploy_vps_pull.sh sro-vMAJOR.MINOR.PATCH EXPECTED_FULL_COMMIT" >&2
    exit 2
fi
if (( EUID != 0 )); then
    echo "Run as root" >&2
    exit 2
fi

exec 8>/run/lock/sro-robot-pull.lock
if ! flock -n 8; then
    echo "Another VPS pull deployment is active" >&2
    exit 2
fi

base=/opt/sro-robot
backup_root=/var/backups/sro-robot
key=/root/.ssh/sro-robot-deploy
known_hosts=/root/.ssh/known_hosts
repo=git@github.com:avariceJS/sro-robot.git
release="$base/releases/$expected_commit"
if [[ -e "$release" || -L "$release" ]]; then
    echo "Release directory already exists; refusing replacement" >&2
    exit 2
fi
if [[ ! -f "$key" || -L "$key" || "$(stat -c %u "$key")" != 0 ]]; then
    echo "Dedicated root GitHub identity is unavailable or unsafe" >&2
    exit 2
fi
key_mode="$(stat -c %a "$key")"
if (( (8#$key_mode & 077) != 0 )); then
    echo "Dedicated root GitHub identity permissions are unsafe" >&2
    exit 2
fi
if [[ ! -f "$known_hosts" || -L "$known_hosts" ]]; then
    echo "Pinned GitHub known-hosts file is required" >&2
    exit 2
fi
if [[ "$(stat -c %u "$known_hosts")" != 0 ]]; then
    echo "GitHub known-hosts file must be root-owned" >&2
    exit 2
fi
known_hosts_mode="$(stat -c %a "$known_hosts")"
if (( (8#$known_hosts_mode & 022) != 0 )); then
    echo "GitHub known-hosts file must not be group/other writable" >&2
    exit 2
fi
if [[ ! -d "$base/releases" || -L "$base/releases" || ! -d "$backup_root" || -L "$backup_root" ]]; then
    echo "Existing release and private backup roots are required" >&2
    exit 2
fi
if [[ "$(stat -c %u "$base/releases")" != 0 || "$(stat -c %u "$backup_root")" != 0 ]]; then
    echo "Release and backup roots must be root-owned" >&2
    exit 2
fi
if (( (8#$(stat -c %a "$backup_root") & 077) != 0 )); then
    echo "Backup root must remain private" >&2
    exit 2
fi

work="$backup_root/.release-pull-$expected_commit-$$"
stage="$base/releases/.incoming-$expected_commit-$$"
stage_created=0
if [[ -e "$work" || -L "$work" || -e "$stage" || -L "$stage" ]]; then
    echo "Private pull workspace already exists; refusing replacement" >&2
    exit 2
fi
mkdir -m 0700 "$work"
cleanup() {
    if (( stage_created )); then rm -rf -- "$stage"; fi
    rm -rf -- "$work"
}
trap cleanup EXIT
git init --quiet --bare "$work/repo.git"
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -i $key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$known_hosts -o GlobalKnownHostsFile=/etc/ssh/ssh_known_hosts -o ConnectTimeout=25 -o ServerAliveInterval=10 -o ServerAliveCountMax=3"
git --git-dir="$work/repo.git" fetch --quiet --no-tags "$repo" \
    "+refs/heads/main:refs/remotes/origin/main" \
    "+refs/tags/$tag:refs/tags/$tag"

if [[ "$(git --git-dir="$work/repo.git" cat-file -t "refs/tags/$tag")" != tag ]]; then
    echo "Release tag must be annotated" >&2
    exit 2
fi
tag_commit="$(git --git-dir="$work/repo.git" rev-parse "refs/tags/$tag^{commit}")"
main_commit="$(git --git-dir="$work/repo.git" rev-parse refs/remotes/origin/main)"
if [[ "$tag_commit" != "$expected_commit" || "$main_commit" != "$expected_commit" ]]; then
    echo "Expected commit, annotated tag, and fetched main tip must match" >&2
    exit 2
fi

if git --git-dir="$work/repo.git" ls-tree -r --name-only "$expected_commit" \
    | grep -E '(^|/)\.env$' >/dev/null; then
    echo "Source tree contains a private environment filename" >&2
    exit 2
fi
mkdir -m 0700 "$stage"
stage_created=1
git --git-dir="$work/repo.git" archive --format=tar "$expected_commit" \
    | tar -xf - -C "$stage"
printf '%s\n' "$expected_commit" > "$stage/.release-commit"
chown -R root:root "$stage"
chmod -R a+rX,go-w "$stage"
chmod 0755 "$stage"
mv -T -- "$stage" "$release"
stage_created=0
trap 'rm -rf -- "$work"' EXIT

echo "release_source=verified tag=$tag commit=$expected_commit"
bash "$release/scripts/ops/provision_vps_release.sh" "$expected_commit"
