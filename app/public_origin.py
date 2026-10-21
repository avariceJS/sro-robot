"""Validate the explicitly configured office website, never a request header."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def public_app_origin(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    # ASCII DNS names keep Python, browser URL.origin and extension match patterns
    # consistent. International names must be configured in their ASCII form.
    if not isinstance(value, str) or not value.isascii() or any(
        char.isspace() or ord(char) < 32 for char in value
    ):
        raise ValueError("APP_PUBLIC_ORIGIN must be one canonical HTTPS DNS origin")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise ValueError("APP_PUBLIC_ORIGIN must be one canonical HTTPS DNS origin") from None
    labels = host.split(".")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port is not None
        or len(host) > 253
        or len(labels) < 2
        or not any(char.isalpha() for char in labels[-1])
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
        or host.endswith((".localhost", ".local", ".internal", ".lan", ".test"))
        or host == "home.arpa"
        or host.endswith(".home.arpa")
        or value != f"https://{host}"
    ):
        raise ValueError("APP_PUBLIC_ORIGIN must be one canonical HTTPS DNS origin")
    return value


def kad_store_listing_url(value: str | None, *, browser: str) -> str | None:
    """Accept only a canonical HTTPS listing on the named official store."""
    if value is None or value == "":
        return None
    hosts = {
        "chrome": "chromewebstore.google.com",
        "edge": "microsoftedge.microsoft.com",
        "firefox": "addons.mozilla.org",
    }
    expected_host = hosts.get(browser)
    if not expected_host or not isinstance(value, str) or not value.isascii():
        raise ValueError(f"KAD {browser} store URL must be a canonical official HTTPS listing")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise ValueError(f"KAD {browser} store URL must be a canonical official HTTPS listing") from None
    parts = parsed.path[1:].split("/") if parsed.path.startswith("/") else []
    path_is_canonical = (
        bool(parts) and all(part not in {"", ".", ".."} for part in parts)
        and "%" not in parsed.path and "\\" not in parsed.path
        and all(re.fullmatch(r"[a-z0-9-]+", part) for part in parts)
    )
    if browser == "chrome":
        path_ok = path_is_canonical and len(parts) in {2, 3} and parts[0] == "detail" and re.fullmatch(r"[a-p]{32}", parts[-1]) is not None
    elif browser == "edge":
        path_ok = path_is_canonical and len(parts) in {3, 4} and parts[:2] == ["addons", "detail"] and re.fullmatch(r"[a-p]{32}", parts[-1]) is not None
    else:
        path_ok = path_is_canonical and len(parts) == 3 and parts[:2] == ["firefox", "addon"] and re.fullmatch(r"[a-z0-9-]+", parts[-1]) is not None
    if (
        parsed.scheme != "https" or host != expected_host or port is not None
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or not path_ok
        or value != f"https://{host}{parsed.path}"
    ):
        raise ValueError(f"KAD {browser} store URL must be a canonical official HTTPS listing")
    return value
