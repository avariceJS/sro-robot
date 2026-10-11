from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.api import deps
from app.services import auth
from app.services.auth import hash_password
from starlette.requests import Request


def _request_with_session(session: dict) -> Request:
    return Request({
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("robot.example", 80),
        "session": session,
    })


class AuthSessionTests(unittest.TestCase):
    def test_password_hashing_rejects_empty_password(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            hash_password("")

    def test_deleted_user_clears_stale_session_before_authentication_failure(self) -> None:
        request = _request_with_session({deps.SESSION_USER_KEY: 17, "other": "value"})

        with patch.object(deps.auth, "get_user", return_value=None):
            with self.assertRaises(deps.NotAuthenticated):
                deps.current_user(request, MagicMock())

        self.assertEqual(request.session, {})

    def test_deactivated_user_clears_stale_session_before_authentication_failure(self) -> None:
        request = _request_with_session({deps.SESSION_USER_KEY: 23})
        user = SimpleNamespace(is_active=False)

        with patch.object(deps.auth, "get_user", return_value=user):
            with self.assertRaises(deps.NotAuthenticated):
                deps.current_user(request, MagicMock())

        self.assertEqual(request.session, {})

    def test_successful_login_rehashes_password_when_parameters_change(self) -> None:
        user = SimpleNamespace(login="operator", is_active=True, password_hash="old-hash")
        db = MagicMock()
        db.scalar.return_value = user

        with (
            patch.object(auth, "verify_password", return_value=True),
            patch.object(auth.PasswordHasher, "check_needs_rehash", return_value=True),
            patch.object(auth, "hash_password", return_value="current-hash") as rehash,
        ):
            authenticated = auth.authenticate(db, "operator", "correct password")

        self.assertIs(authenticated, user)
        self.assertEqual(user.password_hash, "current-hash")
        rehash.assert_called_once_with("correct password")

    def test_failed_login_never_rehashes_or_changes_stored_hash(self) -> None:
        user = SimpleNamespace(login="operator", is_active=True, password_hash="old-hash")
        db = MagicMock()
        db.scalar.return_value = user

        with (
            patch.object(auth, "verify_password", return_value=False),
            patch.object(auth.PasswordHasher, "check_needs_rehash") as needs_rehash,
            patch.object(auth, "hash_password") as rehash,
        ):
            authenticated = auth.authenticate(db, "operator", "incorrect password")

        self.assertIsNone(authenticated)
        self.assertEqual(user.password_hash, "old-hash")
        needs_rehash.assert_not_called()
        rehash.assert_not_called()

    def test_unknown_and_disabled_accounts_use_a_dummy_password_verification(self) -> None:
        for user in (None, SimpleNamespace(is_active=False, password_hash="stored-hash")):
            with self.subTest(user_exists=user is not None):
                db = MagicMock()
                db.scalar.return_value = user
                with patch.object(auth, "verify_password", return_value=False) as verify:
                    authenticated = auth.authenticate(db, "unknown-or-disabled", "synthetic-password")

                self.assertIsNone(authenticated)
                verify.assert_called_once_with(auth._DUMMY_PASSWORD_HASH, "synthetic-password")


if __name__ == "__main__":
    unittest.main()
