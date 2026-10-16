"""Вход сотрудника в приложение.

Это пароль от нашей платформы, а не от СРО: пароль СРО нигде не хранится и
спрашивается отдельно при открытии браузерной сессии.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..db.models import MAX_USER_FIELD_CHARACTERS, LoginSession, User

_hasher = PasswordHasher()
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
# Make unknown and disabled accounts perform a current-parameter Argon2 check.
# Existing hashes with older parameters can still have different verification cost.
_DUMMY_PASSWORD_HASH = _hasher.hash(secrets.token_urlsafe(32))
_SESSION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class InactiveUser(RuntimeError):
    """A login raced with account deactivation or targeted an inactive account."""


def normalize_login(login: str) -> str:
    """«Иванова  М.И. » и «иванова м.и.» — один и тот же сотрудник."""
    return " ".join(login.split())


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Password must not be empty")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


def authenticate(db: Session, login: str, password: str) -> User | None:
    normalized = normalize_login(login)
    user = db.scalar(select(User).where(func.lower(User.login) == normalized.lower()))
    if not user or not user.is_active:
        verify_password(_DUMMY_PASSWORD_HASH, password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


def create_user(db: Session, login: str, password: str, full_name: str = "", sro_login: str = "") -> User:
    normalized = normalize_login(login)
    normalized_sro_login = normalize_login(sro_login) or normalized
    display_name = full_name or normalized
    if not normalized:
        raise ValueError("Login must not be empty")
    if len(normalized) > MAX_USER_FIELD_CHARACTERS:
        raise ValueError("Login exceeds the supported length")
    if len(normalized_sro_login) > MAX_USER_FIELD_CHARACTERS:
        raise ValueError("SRO login exceeds the supported length")
    if len(display_name) > MAX_USER_FIELD_CHARACTERS:
        raise ValueError("Full name exceeds the supported length")
    user = User(
        login=normalized,
        full_name=display_name,
        password_hash=hash_password(password),
        # По умолчанию логин в СРО совпадает с логином у нас: сотрудники
        # называются одинаково в обеих системах.
        sro_login=normalized_sro_login,
    )
    db.add(user)
    db.flush()
    return user


def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def resolve_sro_user(db: Session, sro_login: str) -> User:
    """Resolve a staff identity by the verified SRO login; never merge ambiguously."""
    normalized = normalize_login(sro_login)
    if not normalized or len(normalized) > MAX_USER_FIELD_CHARACTERS:
        raise ValueError("Invalid SRO identity")
    matches = list(
        db.scalars(
            select(User).where(User.sro_login == normalized)
        ).all()
    )
    if len(matches) > 1:
        raise ValueError("Ambiguous SRO identity")
    if matches:
        if not matches[0].is_active:
            raise InactiveUser
        return matches[0]
    by_login = db.scalar(
        select(User).where(func.lower(User.login) == normalized.lower())
    )
    if by_login is not None:
        if normalize_login(by_login.sro_login) not in {"", normalized}:
            raise ValueError("SRO identity conflicts with an existing staff account")
        if not by_login.is_active:
            raise InactiveUser
        by_login.sro_login = normalized
        return by_login
    user = User(
        login=normalized,
        full_name=normalized,
        # This invalid sentinel is not a password and is never accepted by SRO login.
        password_hash="!sro-external-auth-only!",
        sro_login=normalized,
    )
    db.add(user)
    db.flush()
    return user


def new_login_session(db: Session, user_id: int, bearer_token: str) -> LoginSession:
    """Persist a session only while holding the active account's row lock."""
    active_user_id = db.scalar(
        select(User.id)
        .where(User.id == user_id, User.is_active.is_(True))
        .with_for_update()
    )
    if active_user_id is None:
        raise InactiveUser
    token_sha256 = _session_token_sha256(bearer_token)
    if token_sha256 is None:
        raise ValueError("Invalid login session token")
    row = LoginSession(
        user_id=user_id,
        token_sha256=token_sha256,
        expires_at=datetime.now(timezone.utc)
        + timedelta(seconds=SESSION_MAX_AGE_SECONDS),
    )
    db.add(row)
    db.flush()
    return row


def attach_external_session(
    db: Session,
    row: LoginSession,
    storage_state: bytes,
    master_key: str,
    max_age_seconds: int,
) -> None:
    """Persist only authenticated encryption of one user's external browser state."""
    from .external_session import seal_storage_state

    now = datetime.now(timezone.utc)
    row.external_state_ciphertext = seal_storage_state(
        storage_state,
        master_key,
        row.user_id,
        row.id,
        "sroarbitr.sro-service.ru",
    )
    row.external_state_expires_at = min(
        row.expires_at, now + timedelta(seconds=max_age_seconds)
    )


def active_external_state(
    db: Session, user_id: int, master_key: str
) -> tuple[int, bytes] | None:
    """Return the newest unexpired session state belonging only to this user."""
    from .external_session import InvalidExternalSession, open_storage_state

    row = db.scalar(
        select(LoginSession)
        .where(
            LoginSession.user_id == user_id,
            LoginSession.revoked_at.is_(None),
            LoginSession.expires_at > func.clock_timestamp(),
            LoginSession.external_state_expires_at > func.clock_timestamp(),
            LoginSession.external_state_ciphertext.is_not(None),
        )
        .order_by(LoginSession.created_at.desc(), LoginSession.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    try:
        storage_state = open_storage_state(
            row.external_state_ciphertext,
            master_key,
            row.user_id,
            row.id,
            "sroarbitr.sro-service.ru",
        )
        return row.id, storage_state
    except InvalidExternalSession:
        row.external_state_ciphertext = None
        row.external_state_expires_at = datetime.now(timezone.utc)
        db.flush()
        return None


def expire_external_login_session(db: Session, user_id: int, login_session_id: int) -> bool:
    """Revoke only the app login whose externally issued session was rejected."""
    row = db.scalar(
        select(LoginSession).where(
            LoginSession.id == login_session_id,
            LoginSession.user_id == user_id,
            LoginSession.revoked_at.is_(None),
        )
    )
    if row is None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    row.external_state_ciphertext = None
    row.external_state_expires_at = datetime.now(timezone.utc)
    return True


def deactivate_user(db: Session, login: str) -> tuple[User | None, int]:
    """Disable one normalized login and revoke its active sessions atomically."""
    normalized = normalize_login(login)
    if not normalized:
        raise ValueError("Login must not be empty")
    user = db.scalar(
        select(User)
        .where(func.lower(User.login) == normalized.lower())
        .with_for_update()
    )
    if user is None:
        return None, 0
    revoked = db.execute(
        update(LoginSession)
        .where(
            LoginSession.user_id == user.id,
            LoginSession.revoked_at.is_(None),
            LoginSession.expires_at > func.clock_timestamp(),
        )
        .values(
            revoked_at=func.clock_timestamp(),
            external_state_ciphertext=None,
            external_state_expires_at=func.clock_timestamp(),
        )
    )
    user.is_active = False
    return user, int(revoked.rowcount or 0)


def login_session_is_active(db: Session, user_id: int, bearer_token: str) -> bool:
    token_sha256 = _session_token_sha256(bearer_token)
    if token_sha256 is None:
        return False
    return (
        db.scalar(
            select(LoginSession.id).where(
                LoginSession.user_id == user_id,
                LoginSession.token_sha256 == token_sha256,
                LoginSession.revoked_at.is_(None),
                LoginSession.expires_at > func.clock_timestamp(),
                LoginSession.external_state_expires_at > func.clock_timestamp(),
                LoginSession.external_state_ciphertext.is_not(None),
            )
        )
        is not None
    )


def revoke_login_session(db: Session, user_id: int, bearer_token: str) -> bool:
    """Revoke one still-active session; concurrent and repeated logout is a no-op."""
    token_sha256 = _session_token_sha256(bearer_token)
    if token_sha256 is None:
        return False
    result = db.execute(
        update(LoginSession)
        .where(
            LoginSession.user_id == user_id,
            LoginSession.token_sha256 == token_sha256,
            LoginSession.revoked_at.is_(None),
            LoginSession.expires_at > func.clock_timestamp(),
        )
        .values(revoked_at=func.clock_timestamp())
        .returning(LoginSession.id)
    )
    revoked_id = result.scalar_one_or_none()
    if revoked_id is None:
        return False
    row = db.get(LoginSession, revoked_id)
    if row is not None:
        row.external_state_ciphertext = None
        row.external_state_expires_at = datetime.now(timezone.utc)
    return True


def revoke_all_login_sessions(db: Session) -> int:
    """Invalidate every currently usable browser session in one transaction."""
    result = db.execute(
        update(LoginSession)
        .where(
            LoginSession.revoked_at.is_(None),
            LoginSession.expires_at > func.clock_timestamp(),
        )
        .values(
            revoked_at=func.clock_timestamp(),
            external_state_ciphertext=None,
            external_state_expires_at=func.clock_timestamp(),
        )
    )
    return int(result.rowcount or 0)


def _session_token_sha256(token: str) -> str | None:
    if not isinstance(token, str) or not _SESSION_TOKEN_PATTERN.fullmatch(token):
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()
