#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script as root on the VPS." >&2
    exit 1
fi

interface="${SRO_DNS_INTERFACE:-enp0s3}"
dns_tls_server="${SRO_DNS_TLS_SERVER:-8.8.8.8#dns.google}"
stub_resolv_conf="/run/systemd/resolve/stub-resolv.conf"
backup_dir="/root/sro-robot-ops-backups/resolver"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"

systemctl is-active --quiet systemd-resolved
systemctl is-enabled --quiet systemd-resolved
test -s "$stub_resolv_conf"
install -d -o root -g root -m 0700 "$backup_dir"

if [[ -L /etc/resolv.conf ]]; then
    current_resolv_target="$(readlink -f /etc/resolv.conf)"
    if [[ "$current_resolv_target" != "$stub_resolv_conf" ]]; then
        echo "/etc/resolv.conf is a nonstandard symlink; refusing to replace it." >&2
        exit 2
    fi
elif [[ -f /etc/resolv.conf ]]; then
    resolv_backup="$backup_dir/resolv.conf-$timestamp"
    cp -a /etc/resolv.conf "$resolv_backup"
    temporary_link="/etc/.resolv.conf.sro-robot-$$"
    trap 'if [[ -L "${temporary_link:-}" ]]; then unlink "$temporary_link"; fi' EXIT
    ln -s "$stub_resolv_conf" "$temporary_link"
    mv -Tf "$temporary_link" /etc/resolv.conf
    unset temporary_link
    echo "Saved previous resolver file: $resolv_backup"
else
    echo "/etc/resolv.conf is missing or not a regular file; refusing to replace it." >&2
    exit 2
fi

python3 - "$backup_dir" "$timestamp" <<'PY'
from pathlib import Path
import os
import re
import shutil
import sys
import tempfile

path = Path("/etc/gai.conf")
backup_dir = Path(sys.argv[1])
timestamp = sys.argv[2]
text = path.read_text()
matches = list(re.finditer(
    r"(?m)^(?P<prefix>\s*precedence\s+::ffff:0:0/96\s+)(?P<value>\d+)(?P<suffix>\s*(?:#.*)?)$",
    text,
))
if len(matches) > 1:
    raise SystemExit("Multiple IPv4 precedence entries found; refusing to edit gai.conf.")
if not matches:
    raise SystemExit(0)
match = matches[0]
value = int(match.group("value"))
if value == 35:
    raise SystemExit(0)
if value != 100:
    raise SystemExit("Unexpected IPv4 precedence value; refusing to edit gai.conf.")

backup = backup_dir / f"gai.conf-{timestamp}"
shutil.copy2(path, backup)
updated = text[:match.start()] + match.group("prefix") + "35" + match.group("suffix") + text[match.end():]
fd, temporary = tempfile.mkstemp(prefix=".gai.conf.sro-robot-", dir="/etc")
try:
    with os.fdopen(fd, "w") as stream:
        stream.write(updated)
    os.chmod(temporary, path.stat().st_mode & 0o777)
    os.chown(temporary, path.stat().st_uid, path.stat().st_gid)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print(f"Saved previous address preference: {backup}")
PY

dropin_dir="/etc/systemd/system/certbot.service.d"
dropin="$dropin_dir/10-sro-office-dns-over-tls.conf"
install -d -o root -g root -m 0755 "$dropin_dir"
temporary_dropin="$(mktemp "$dropin_dir/.10-sro-office-dns-over-tls.XXXXXX")"
trap 'if [[ -f "${temporary_dropin:-}" ]]; then unlink "$temporary_dropin"; fi' EXIT
cat > "$temporary_dropin" <<EOF
[Unit]
Requires=systemd-resolved.service
After=systemd-resolved.service

[Service]
ExecStartPre=/usr/bin/resolvectl dns $interface $dns_tls_server
ExecStartPre=/usr/bin/resolvectl dnsovertls $interface yes
EOF
chown root:root "$temporary_dropin"
chmod 0644 "$temporary_dropin"
mv -f "$temporary_dropin" "$dropin"
unset temporary_dropin

systemctl daemon-reload
systemd-analyze verify certbot.service
resolvectl dns "$interface" "$dns_tls_server"
resolvectl dnsovertls "$interface" yes
resolvectl flush-caches
getent ahostsv4 api.cloudflare.com >/dev/null
getent ahostsv4 acme-staging-v02.api.letsencrypt.org >/dev/null
curl -fsS --max-time 10 https://api.cloudflare.com/client/v4/ips >/dev/null
curl -fsS --max-time 10 https://acme-staging-v02.api.letsencrypt.org/directory >/dev/null
systemctl is-enabled --quiet certbot.timer
systemctl is-active --quiet certbot.timer

echo "Certbot DNS-over-TLS preflight configured and verified."
