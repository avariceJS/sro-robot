from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from starlette.requests import Request
from starlette.testclient import TestClient

from app.api.deps import current_user
from app.db.base import get_session
from app.main import app
from app.api import routes_tasks


def _request(request_id: str) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/upload",
            "raw_path": b"/upload",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("robot.example", 443),
            "state": {"request_id": request_id},
        }
    )


class TaskRequestCorrelationTests(unittest.TestCase):
    def test_upload_http_request_id_reaches_creation_event(self) -> None:
        db = MagicMock()
        document = SimpleNamespace(id=3)
        task = SimpleNamespace(id=8)

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(routes_tasks.tasks, "store_document", return_value=document),
            patch.object(routes_tasks.tasks, "create_task", return_value=task) as create_task,
            patch.object(routes_tasks, "enqueue_recognize"),
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/upload",
                        files={"files": ("private.pdf", io.BytesIO(b"synthetic local test file"), "application/pdf")},
                        headers={
                            "Origin": "http://testserver",
                            "X-Request-ID": "d" * 32,
                        },
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 303)
        request_id = response.headers["x-request-id"]
        self.assertRegex(request_id, r"^[0-9a-f]{32}$")
        self.assertNotEqual(request_id, "d" * 32)
        create_task.assert_called_once_with(db, document, user_id=23, request_id=request_id)
        db.commit.assert_called_once_with()

    def test_kad_http_request_id_reaches_creation_event(self) -> None:
        db = MagicMock()
        task = SimpleNamespace(id=9)

        def fake_session():
            yield db

        app.dependency_overrides[get_session] = fake_session
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=23)

        with (
            patch.object(routes_tasks.tasks, "create_kad_task", return_value=task) as create_kad_task,
            patch.object(routes_tasks, "enqueue_kad_recognize"),
        ):
            try:
                with TestClient(app, base_url="http://testserver") as client:
                    response = client.post(
                        "/case-search",
                        data={"case_number": "А99-12345/2099"},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
            finally:
                app.dependency_overrides.pop(get_session, None)
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 303)
        request_id = response.headers["x-request-id"]
        self.assertRegex(request_id, r"^[0-9a-f]{32}$")
        create_kad_task.assert_called_once_with(
            db,
            "А99-12345/2099",
            23,
            request_id=request_id,
        )
        db.commit.assert_called_once_with()

    def test_invalid_or_untrusted_request_state_is_not_propagated(self) -> None:
        for value in ("user-controlled", "b" * 31, "B" * 32, None):
            with self.subTest(value=value):
                request = _request(value) if value is not None else _request("c" * 32)
                if value is None:
                    request.scope["state"].pop("request_id")
                else:
                    request.scope["state"]["request_id"] = value
                self.assertIsNone(routes_tasks._server_request_id(request))


if __name__ == "__main__":
    unittest.main()
