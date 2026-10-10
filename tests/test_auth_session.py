from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.api import deps
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


if __name__ == "__main__":
    unittest.main()
