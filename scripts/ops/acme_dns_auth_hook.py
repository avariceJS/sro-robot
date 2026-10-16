#!/usr/bin/env python3
"""Certbot manual-auth hook for the delegated sro-office.ru ACME DNS label."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ACCOUNT_FILE = Path("/etc/letsencrypt/acme-dns-sro-office.json")
DOMAIN = "sro-office.ru"
ACME_DNS_UPDATE = "https://auth.acme-dns.io/update"
DNS_OVER_HTTPS = "https://cloudflare-dns.com/dns-query"
PROPAGATION_TIMEOUT_SECONDS = 300
PROPAGATION_INTERVAL_SECONDS = 5


def load_account() -> dict[str, str]:
    info = ACCOUNT_FILE.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise RuntimeError("unsafe account file ownership or permissions")
    account = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
    if not all(account.get(key) for key in ("username", "password", "subdomain", "fulldomain")):
        raise RuntimeError("incomplete account file")
    if not account["fulldomain"].endswith(".auth.acme-dns.io"):
        raise RuntimeError("unexpected delegated DNS target")
    return account


def publish(account: dict[str, str], validation: str) -> None:
    body = json.dumps({"subdomain": account["subdomain"], "txt": validation}).encode()
    request = Request(
        ACME_DNS_UPDATE,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Api-User": account["username"],
            "X-Api-Key": account["password"],
        },
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("acme-dns update rejected")


def visible(validation: str) -> bool:
    query = urlencode({"name": f"_acme-challenge.{DOMAIN}", "type": "TXT"})
    request = Request(
        f"{DNS_OVER_HTTPS}?{query}",
        headers={"Accept": "application/dns-json"},
    )
    with urlopen(request, timeout=10) as response:
        result = json.load(response)
    return any(
        item.get("type") == 16
        and item.get("data", "").strip('"') == validation
        for item in result.get("Answer", [])
    )


def main() -> int:
    if os.environ.get("CERTBOT_DOMAIN", "").lower().rstrip(".") != DOMAIN:
        print("ACME hook refused an unexpected domain", file=sys.stderr)
        return 2
    validation = os.environ.get("CERTBOT_VALIDATION", "")
    if not validation or len(validation) > 256 or any(c.isspace() for c in validation):
        print("ACME hook received an invalid validation value", file=sys.stderr)
        return 2
    try:
        account = load_account()
        publish(account, validation)
        deadline = time.monotonic() + PROPAGATION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if visible(validation):
                print("Delegated ACME TXT record is publicly visible")
                return 0
            time.sleep(PROPAGATION_INTERVAL_SECONDS)
    except (OSError, ValueError, URLError, RuntimeError) as exc:
        print(f"ACME DNS hook failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("ACME TXT propagation timed out", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
