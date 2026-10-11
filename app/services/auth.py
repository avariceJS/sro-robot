"""Вход сотрудника в приложение.

Это пароль от нашей платформы, а не от СРО: пароль СРО нигде не хранится и
спрашивается отдельно при открытии браузерной сессии.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import User

_hasher = PasswordHasher()
# Keep unknown and disabled accounts on the same Argon2 verification path as
# active accounts so login response timing does not expose account existence.
_DUMMY_PASSWORD_HASH = _hasher.hash(secrets.token_urlsafe(32))


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
    user = User(
        login=normalized,
        full_name=full_name or normalized,
        password_hash=hash_password(password),
        # По умолчанию логин в СРО совпадает с логином у нас: сотрудники
        # называются одинаково в обеих системах.
        sro_login=normalize_login(sro_login) or normalized,
    )
    db.add(user)
    db.flush()
    return user


def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)
