#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
vps_host="${SRO_VPS_HOST:?Set SRO_VPS_HOST to the verified current production host}"
ssh_key="${SRO_VPS_SSH_KEY:-$HOME/.ssh/sro-office-codex}"

python3 "$repo_root/scripts/ops/check_ssh_identity.py" "$ssh_key"
printf 'Paste the zone-scoped Cloudflare DNS API token (input hidden): ' >&2
IFS= read -r -s token
printf '\n' >&2
if [[ -z "$token" || "$token" == *[[:space:]]* ]]; then
    unset token
    echo 'A non-empty token without whitespace is required.' >&2
    exit 2
fi
trap 'unset token' EXIT

remote_script='
set -euo pipefail
IFS= read -r token
test -n "$token"
umask 077
install -d -o root -g root -m 0700 /etc/letsencrypt
temporary="$(mktemp /etc/letsencrypt/.cloudflare.ini.XXXXXX)"
trap '\''rm -f "$temporary"; unset token'\'' EXIT
printf '\''dns_cloudflare_api_token = %s\n'\'' "$token" > "$temporary"
chown root:root "$temporary"
chmod 0600 "$temporary"
mv -f "$temporary" /etc/letsencrypt/cloudflare.ini
unset token
printf '\''Cloudflare credentials installed root-only.\n'\''
'
printf -v quoted_remote '%q' "$remote_script"
printf '%s\n' "$token" | ssh \
    -i "$ssh_key" \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=yes \
    "$vps_host" \
    "bash -c $quoted_remote"

unset token
