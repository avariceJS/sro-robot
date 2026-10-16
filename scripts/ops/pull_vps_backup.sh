#!/bin/bash
set -euo pipefail
umask 077

remote_host="${SRO_BACKUP_HOST:-root@85.192.30.98}"
ssh_key="${SRO_BACKUP_SSH_KEY:-$HOME/.ssh/sro-office-codex}"
destination_root="${SRO_BACKUP_DEST:-$HOME/Library/Application Support/SRO Robot/recovery}"
backup_root='/var/backups/sro-robot'
ssh_args=(-i "$ssh_key" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes)

if [[ ! -f "$ssh_key" || -L "$ssh_key" ]]; then
    echo 'backup pull failed: SSH identity is missing or unsafe' >&2
    exit 2
fi
if [[ -L "$destination_root" ]]; then
    echo 'backup pull failed: destination must not be a symlink' >&2
    exit 2
fi
mkdir -p "$destination_root"
chmod 0700 "$destination_root"

backup_name="$(ssh "${ssh_args[@]}" "$remote_host" \
    "find '$backup_root' -mindepth 1 -maxdepth 1 -type d -name 'daily-*' -printf '%f\\n' | sort | tail -n 1")"
if [[ ! "$backup_name" =~ ^daily-[0-9]{8}T[0-9]{6}Z$ ]]; then
    echo 'backup pull failed: no valid completed daily backup found' >&2
    exit 1
fi
remote_dir="$backup_root/$backup_name"

ssh "${ssh_args[@]}" "$remote_host" \
    "test \"\$(stat -c '%a:%U:%G' '$remote_dir')\" = '700:root:root' && cd '$remote_dir' && for artifact in SHA256SUMS postgres.dump redis.rdb documents.tar.gz; do test -f \"\$artifact\" && test ! -L \"\$artifact\" || exit 1; done && test \"\$(wc -l < SHA256SUMS)\" -eq 3 && grep -Eq '^[a-f0-9]{64}  postgres.dump$' SHA256SUMS && grep -Eq '^[a-f0-9]{64}  redis.rdb$' SHA256SUMS && grep -Eq '^[a-f0-9]{64}  documents.tar.gz$' SHA256SUMS && sha256sum --check --status SHA256SUMS"
remote_manifest_sha="$(ssh "${ssh_args[@]}" "$remote_host" \
    "sha256sum '$remote_dir/SHA256SUMS'" | awk '{print $1}')"
if [[ ! "$remote_manifest_sha" =~ ^[a-f0-9]{64}$ ]]; then
    echo 'backup pull failed: remote manifest hash is invalid' >&2
    exit 1
fi

final_dir="$destination_root/$backup_name"
if [[ -e "$final_dir" ]]; then
    if [[ -L "$final_dir" || ! -d "$final_dir" ]]; then
        echo 'backup pull failed: existing destination is not a regular directory' >&2
        exit 1
    fi
    local_manifest_sha="$(shasum -a 256 "$final_dir/SHA256SUMS" | awk '{print $1}')"
    if [[ "$local_manifest_sha" != "$remote_manifest_sha" ]]; then
        echo 'backup pull failed: existing backup manifest differs from VPS' >&2
        exit 1
    fi
    (cd "$final_dir" && shasum -a 256 -c SHA256SUMS >/dev/null)
    echo "backup pull verified: $backup_name (already present)"
    exit 0
fi

stage="$(mktemp -d "$destination_root/.${backup_name}.partial.XXXXXX")"
cleanup() {
    if [[ -n "${stage:-}" && -d "$stage" ]]; then
        python3 - "$stage" "$destination_root" "$backup_name" <<'PY'
import shutil, sys
from pathlib import Path
candidate, parent, backup = map(Path, sys.argv[1:])
assert candidate.parent == parent
assert candidate.name.startswith(f'.{backup.name}.partial.')
shutil.rmtree(candidate)
PY
    fi
}
trap cleanup EXIT

for artifact in SHA256SUMS postgres.dump redis.rdb documents.tar.gz; do
    scp -pq "${ssh_args[@]}" "$remote_host:$remote_dir/$artifact" "$stage/"
done
(cd "$stage" && shasum -a 256 -c SHA256SUMS >/dev/null)
if [[ "$(shasum -a 256 "$stage/SHA256SUMS" | awk '{print $1}')" != "$remote_manifest_sha" ]]; then
    echo 'backup pull failed: copied manifest hash differs from VPS' >&2
    exit 1
fi
chmod 0600 "$stage"/*
chmod 0700 "$stage"
mv "$stage" "$final_dir"
stage=''
trap - EXIT
echo "backup pull verified: $backup_name"
