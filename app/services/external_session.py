"""Encrypted, short-lived SRO browser state bound to one local login session."""

from __future__ import annotations

import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_MAGIC = b"SRO1"
_NONCE_BYTES = 12
_MAX_STATE_BYTES = 1_000_000


class InvalidExternalSession(ValueError):
    """The encrypted or serialized SRO browser state is invalid."""


def validate_storage_state(storage_state: bytes, allowed_domain: str) -> bytes:
    """Validate and canonicalize Playwright state without exposing cookie values."""
    if not isinstance(storage_state, bytes) or not 1 <= len(storage_state) <= _MAX_STATE_BYTES:
        raise InvalidExternalSession("SRO session state is outside the supported size")
    try:
        value = json.loads(storage_state)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidExternalSession("SRO session state is invalid") from exc
    if not isinstance(value, dict) or set(value) - {"cookies", "origins"}:
        raise InvalidExternalSession("SRO session state has unsupported fields")
    cookies = value.get("cookies")
    origins = value.get("origins", [])
    if not isinstance(cookies, list) or not cookies:
        raise InvalidExternalSession("SRO session has no cookies")
    if not isinstance(origins, list):
        raise InvalidExternalSession("SRO session origins are invalid")
    expected = allowed_domain.casefold().lstrip(".")
    if any(
        not isinstance(cookie, dict)
        or not isinstance(cookie.get("domain"), str)
        or cookie["domain"].casefold().lstrip(".") != expected
        for cookie in cookies
    ):
        raise InvalidExternalSession("SRO session cookie domain does not match")
    if not any(cookie.get("domain", "").casefold().lstrip(".") == expected for cookie in cookies):
        raise InvalidExternalSession("SRO session has no cookies for the expected domain")
    expected_origin = f"https://{expected}"
    if any(
        not isinstance(origin, dict)
        or not isinstance(origin.get("origin"), str)
        or origin["origin"].casefold().rstrip("/") != expected_origin
        or not isinstance(origin.get("localStorage", []), list)
        for origin in origins
    ):
        raise InvalidExternalSession("SRO session contains an unexpected origin")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _cipher(master_key: str) -> AESGCM:
    if not isinstance(master_key, str) or len(master_key) < 32:
        raise ValueError("Application secret is too short for external session encryption")
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"sro-robot/external-browser-state/v1",
    ).derive(master_key.encode("utf-8"))
    return AESGCM(key)


def seal_storage_state(
    storage_state: bytes,
    master_key: str,
    user_id: int,
    login_session_id: int,
    allowed_domain: str,
) -> bytes:
    canonical = validate_storage_state(storage_state, allowed_domain)
    nonce = os.urandom(_NONCE_BYTES)
    aad = f"sro-session:{user_id}:{login_session_id}:v1".encode("ascii")
    return _MAGIC + nonce + _cipher(master_key).encrypt(nonce, canonical, aad)


def open_storage_state(
    ciphertext: bytes,
    master_key: str,
    user_id: int,
    login_session_id: int,
    allowed_domain: str,
) -> bytes:
    if not isinstance(ciphertext, bytes) or len(ciphertext) <= len(_MAGIC) + _NONCE_BYTES:
        raise InvalidExternalSession("SRO session state is invalid")
    if not ciphertext.startswith(_MAGIC):
        raise InvalidExternalSession("SRO session state version is unsupported")
    nonce = ciphertext[len(_MAGIC) : len(_MAGIC) + _NONCE_BYTES]
    encrypted = ciphertext[len(_MAGIC) + _NONCE_BYTES :]
    aad = f"sro-session:{user_id}:{login_session_id}:v1".encode("ascii")
    try:
        plaintext = _cipher(master_key).decrypt(nonce, encrypted, aad)
    except InvalidTag as exc:
        raise InvalidExternalSession("SRO session state authentication failed") from exc
    return validate_storage_state(plaintext, allowed_domain)
