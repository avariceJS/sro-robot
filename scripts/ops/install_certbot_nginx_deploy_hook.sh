#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script as root on the VPS." >&2
    exit 1
fi

lineage="/etc/letsencrypt/live/sro-office.ru"
renewal_config="/etc/letsencrypt/renewal/sro-office.ru.conf"
hook_dir="/etc/letsencrypt/renewal-hooks/deploy"
hook="$hook_dir/50-sro-office-nginx-reload"

test -s "$renewal_config"
grep -q '^authenticator = dns-cloudflare$' "$renewal_config"
test -s "$lineage/fullchain.pem"
test -s "$lineage/privkey.pem"
systemctl is-active --quiet nginx

install -d -o root -g root -m 0755 "$hook_dir"
temporary="$(mktemp "$hook_dir/.50-sro-office-nginx-reload.XXXXXX")"
trap 'if [[ -f "${temporary:-}" ]]; then unlink "$temporary"; fi' EXIT
cat > "$temporary" <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail

expected_lineage="/etc/letsencrypt/live/sro-office.ru"
[[ "${RENEWED_LINEAGE:-}" == "$expected_lineage" ]] || exit 0
[[ " ${RENEWED_DOMAINS:-} " == *" sro-office.ru "* ]] || exit 0

nginx -t
systemctl reload nginx
HOOK
chown root:root "$temporary"
chmod 0750 "$temporary"
mv -f "$temporary" "$hook"
unset temporary

bash -n "$hook"
test "$(stat -c '%a:%U:%G' "$hook")" = '750:root:root'
echo "Certbot deploy hook installed for the sro-office.ru lineage."
