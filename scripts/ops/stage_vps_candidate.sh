#!/usr/bin/env bash
# Stage a pinned develop source archive for isolated rehearsal only. Never activates it.
set -euo pipefail
umask 077

expected_commit="${1:-}"
if [[ ! "$expected_commit" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Usage: stage_vps_candidate.sh EXPECTED_DEVELOP_FULL_COMMIT" >&2
    exit 2
fi
if (( EUID != 0 )); then
    echo "Run as root" >&2
    exit 2
fi

key=/root/.ssh/sro-robot-deploy
known_hosts=/root/.ssh/known_hosts
repo=git@github.com:avariceJS/sro-robot.git
backup_root=/var/backups/sro-robot
stage_root="$backup_root/candidate-staging"
if [[ ! -f "$key" || -L "$key" || "$(stat -c %u "$key")" != 0 ]]; then
    echo "Dedicated root GitHub identity is unavailable or unsafe" >&2
    exit 2
fi
key_mode="$(stat -c %a "$key")"
if (( (8#$key_mode & 077) != 0 )); then
    echo "Dedicated root GitHub identity permissions are unsafe" >&2
    exit 2
fi
if [[ ! -f "$known_hosts" || -L "$known_hosts" || ! -d "$backup_root" || -L "$backup_root" ]]; then
    echo "Pinned GitHub known hosts and private backup root are required" >&2
    exit 2
fi
if [[ "$(stat -c %u "$known_hosts")" != 0 || "$(stat -c %u "$backup_root")" != 0 ]]; then
    echo "GitHub known hosts and backup root must be root-owned" >&2
    exit 2
fi
known_hosts_mode="$(stat -c %a "$known_hosts")"
if (( (8#$known_hosts_mode & 022) != 0 )); then
    echo "GitHub known-hosts file must not be group/other writable" >&2
    exit 2
fi
backup_mode="$(stat -c %a "$backup_root")"
if (( (8#$backup_mode & 077) != 0 )); then
    echo "Backup root must remain private" >&2
    exit 2
fi
if [[ -L "$stage_root" ]]; then
    echo "Candidate staging root cannot be a symlink" >&2
    exit 2
fi
install -d -o root -g root -m 0700 "$stage_root"
archive="$stage_root/candidate-$expected_commit.tar"
manifest="$stage_root/candidate-$expected_commit.sha256"
if [[ -e "$archive" || -L "$archive" || -e "$manifest" || -L "$manifest" ]]; then
    echo "Candidate staging artifact already exists; refusing replacement" >&2
    exit 2
fi

work="$backup_root/.candidate-stage-$expected_commit-$$"
if [[ -e "$work" || -L "$work" ]]; then
    echo "Private staging workspace already exists; refusing replacement" >&2
    exit 2
fi
trap 'rm -rf -- "$work"' EXIT
mkdir -m 0700 "$work"
git init --quiet --bare "$work/repo.git"
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -i $key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$known_hosts -o GlobalKnownHostsFile=/etc/ssh/ssh_known_hosts -o ConnectTimeout=25 -o ServerAliveInterval=10 -o ServerAliveCountMax=3"
git --git-dir="$work/repo.git" fetch --quiet --no-tags "$repo" \
    "+refs/heads/develop:refs/remotes/origin/develop"
remote_develop="$(git --git-dir="$work/repo.git" rev-parse refs/remotes/origin/develop)"
if [[ "$remote_develop" != "$expected_commit" ]]; then
    echo "Pinned candidate must equal fetched develop tip" >&2
    exit 2
fi
if git --git-dir="$work/repo.git" ls-tree -r --name-only "$expected_commit" \
    | grep -E '(^|/)\.env$' >/dev/null; then
    echo "Candidate source contains a private environment filename" >&2
    exit 2
fi
git --git-dir="$work/repo.git" archive --format=tar "$expected_commit" > "$work/candidate.tar"
chmod 0600 "$work/candidate.tar"
tree="$(git --git-dir="$work/repo.git" rev-parse "$expected_commit^{tree}")"
archive_hash="$(sha256sum "$work/candidate.tar" | cut -d ' ' -f 1)"
printf '%s  %s\n' "$archive_hash" "$(basename "$archive")" > "$work/candidate.sha256"
chmod 0600 "$work/candidate.sha256"
mv -- "$work/candidate.tar" "$archive"
mv -- "$work/candidate.sha256" "$manifest"
printf 'candidate_stage=source_only commit=%s tree=%s archive_sha256=%s\n' \
    "$expected_commit" "$tree" "$archive_hash"
