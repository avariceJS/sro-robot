from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.db.models import LoginSession, User
from app.services import auth


class FakeSession:
    """Model only user/session rows needed by the authentication service."""

    def __init__(self, *, active: bool = True) -> None:
        self.user = SimpleNamespace(id=73, login="synthetic-operator", is_active=active)
        self.session = SimpleNamespace(
            id=1,
            user_id=73,
            token_sha256=auth._session_token_sha256("s" * 43),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            revoked_at=None,
        )
        self.added = []

    def scalar(self, statement):
        description = statement.column_descriptions[0]
        entity = description["entity"]
        expression = description["expr"]
        if entity is User:
            if expression is User:
                return self.user
            return self.user.id if self.user.is_active else None
        if entity is LoginSession:
            return self.session.id if self.session.revoked_at is None else None
        raise AssertionError(f"unexpected scalar entity: {entity!r}")

    def execute(self, statement):
        if statement.table.name != "login_session":
            raise AssertionError("unexpected update target")
        if self.session.revoked_at is None:
            self.session.revoked_at = datetime.now(timezone.utc)
            return SimpleNamespace(rowcount=1)
        return SimpleNamespace(rowcount=0)

    def add(self, row) -> None:
        row.id = 1
        self.added.append(row)
        self.session = row

    def get(self, model, user_id):
        return self.user if model is User and user_id == self.user.id else None


def control() -> None:
    db = FakeSession()
    token = "a" * 43
    row = auth.new_login_session(db, 73, token)
    assert row.token_sha256 == auth._session_token_sha256(token)
    assert auth.login_session_is_active(db, 73, token)
    print("PPV_REACHED", flush=True)
    print("active_account_session=issued_and_readable")


def exploit() -> None:
    db = FakeSession()
    token = "s" * 43
    assert auth.login_session_is_active(db, 73, token)
    deactivate = getattr(auth, "deactivate_user", None)
    if callable(deactivate):
        deactivate(db, db.user.login)
    else:
        # Before the command existed, account status could be toggled while
        # the session row stayed active; current_user rejected it only until
        # the same account was reactivated.
        db.user.is_active = False
    db.user.is_active = True

    reactivated_cookie_is_rejected = (
        auth.login_session_is_active(db, 73, token) is False
        or db.get(User, 73).is_active is False
    )
    print("PPV_REACHED", flush=True)
    assert reactivated_cookie_is_rejected, "pre-deactivation browser session became valid again"
    print("pre_deactivation_session_after_reactivation=rejected")


def variant() -> None:
    db = FakeSession(active=False)
    blocked = False
    try:
        auth.new_login_session(db, 73, "b" * 43)
    except auth.InactiveUser:
        blocked = True
    print("PPV_REACHED", flush=True)
    assert blocked, "login session was issued for a deactivated user"
    print("inactive_account_session_issue=blocked")


def behavior() -> None:
    result = auth.normalize_login("  Synthetic   Operator  ")
    assert result == "Synthetic Operator"
    print(f"normalized_login={result}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: session_deactivation.py control|exploit|variant|behavior")
    modes = {
        "control": control,
        "exploit": exploit,
        "variant": variant,
        "behavior": behavior,
    }
    try:
        modes[sys.argv[1]]()
    except KeyError:
        raise SystemExit("unknown validation mode")
