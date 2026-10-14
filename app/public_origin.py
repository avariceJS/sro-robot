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
