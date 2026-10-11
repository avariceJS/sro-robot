from __future__ import annotations

import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from starlette.testclient import TestClient

from app.api.deps import SESSION_USER_KEY
from app.db.base import get_session
from app.db.models import Task, User
from app import main
from app.main import app
from app.services.auth import hash_password
from app.observability import access_logger


class FakeSession:
    """Minimal in-memory session for HTTP boundary tests; it never opens the DB."""

    def __init__(self, user: User | None = None) -> None:
        self.user = user
        self.get_calls: list[tuple[type, int]] = []
        self.scalar_calls = 0
        self.events = []
        self.commit_calls = 0

    def add(self, event) -> None:
        self.events.append(event)

    def commit(self) -> None:
        self.commit_calls += 1

    def scalar(self, _statement):
        self.scalar_calls += 1
        return self.user

    def get(self, model: type, object_id: int):
        self.get_calls.append((model, object_id))
        if model is User and self.user and self.user.id == object_id:
            return self.user
        if model is Task:
            return None
        return None


class FakeResult:
    def __init__(self, values: list[str] | None = None) -> None:
        self.values = values or []

    def scalars(self):
        return self

    def all(self) -> list[str]:
        return self.values


class FakeDatabaseConnection:
    def __init__(self, lease_columns: list[str] | None = None) -> None:
        self.lease_columns = lease_columns or []

    def execute(self, statement):
        if "information_schema.columns" in str(statement):
            return FakeResult(self.lease_columns)
        return FakeResult()


class FakeDatabaseEngine:
    def __init__(self, lease_columns: list[str] | None = None) -> None:
        self.connection = FakeDatabaseConnection(lease_columns)

    @contextmanager
    def connect(self):
        yield self.connection


class FakeRedisClient:
    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


class HTTPApplicationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user = User(
            id=73,
            login="synthetic-integration-user",
            full_name="Synthetic Integration User",
            password_hash=hash_password("synthetic-integration-password"),
            sro_login="synthetic-integration-user",
            is_active=True,
        )
        self.session = FakeSession(self.user)

        def fake_session() -> Iterator[FakeSession]:
            yield self.session

        app.dependency_overrides[get_session] = fake_session
        self.queue_patcher = patch.object(
            main,
            "Queue",
            side_effect=lambda name, connection: SimpleNamespace(name=name, count=0),
        )
        self.worker_patcher = patch.object(main, "Worker")
        self.queue_mock = self.queue_patcher.start()
        self.worker_mock = self.worker_patcher.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.queue_patcher.stop()
        self.worker_patcher.stop()
        app.dependency_overrides.pop(get_session, None)

    def test_login_page_returns_security_and_private_cache_headers(self) -> None:
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertRegex(response.headers["x-request-id"], r"^[0-9a-f]{32}$")
        self.assertEqual(self.session.scalar_calls, 0)

    def test_authenticated_login_and_logout_are_http_session_transitions(self) -> None:
        login = self.client.post(
            "/login",
            data={"login": self.user.login, "password": "synthetic-integration-password"},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/")
        self.assertIn("session", self.client.cookies)
        self.assertEqual(self.session.events[-1].kind, "session_started")
        self.assertEqual(self.session.events[-1].user_id, self.user.id)
        self.assertEqual(self.session.events[-1].payload, {"request_id": login.headers["x-request-id"]})

        logout = self.client.post(
            "/logout",
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

        self.assertEqual(logout.status_code, 303)
        self.assertEqual(logout.headers["location"], "http://testserver/login")
        self.assertNotIn("session", self.client.cookies)
        self.assertNotIn(SESSION_USER_KEY, self.client.cookies)
        self.assertEqual(self.session.events[-1].kind, "session_ended")
        self.assertEqual(self.session.events[-1].user_id, self.user.id)
        self.assertEqual(self.session.events[-1].payload, {"request_id": logout.headers["x-request-id"]})
        self.assertEqual(self.session.commit_calls, 2)

    def test_logout_without_an_active_employee_does_not_create_anonymous_audit_actor(self) -> None:
        self.client.post(
            "/logout",
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

        self.assertEqual(self.session.events, [])
        self.assertEqual(self.session.commit_calls, 0)

    def test_unauthenticated_task_page_redirects_before_task_lookup(self) -> None:
        response = self.client.get("/tasks/901", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "http://testserver/login")
        self.assertEqual(self.session.get_calls, [])

    def test_every_task_route_redirects_unauthenticated_requests_before_task_lookup(self) -> None:
        requests = [
            ("GET", "/", {}),
            ("POST", "/upload", {"files": {"files": ("synthetic.pdf", b"%PDF-1.4 synthetic", "application/pdf")}}),
            ("GET", "/tasks/901", {}),
            ("POST", "/tasks/901/save", {"data": {}}),
            ("POST", "/tasks/901/approve", {"data": {}}),
            ("POST", "/tasks/901/run", {}),
            ("GET", "/tasks/901/confirm-save", {}),
            ("POST", "/tasks/901/decision", {"data": {"decision": "save"}}),
            ("GET", "/tasks/901/shot/0", {}),
            ("GET", "/tasks/901/status", {}),
        ]

        for method, path, kwargs in requests:
            with self.subTest(method=method, path=path):
                response = self.client.request(
                    method,
                    path,
                    headers={"Origin": "http://testserver"} if method == "POST" else None,
                    follow_redirects=False,
                    **kwargs,
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "http://testserver/login")
        self.assertEqual(self.session.get_calls, [])

    def test_access_log_uses_route_template_and_omits_request_path_and_query(self) -> None:
        with self.assertLogs(access_logger, level="INFO") as captured:
            response = self.client.get(
                "/tasks/901?token=synthetic-query-secret",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertIn("route=/tasks/{task_id}", captured.output[0])
        self.assertRegex(captured.output[0], r"status=303 duration_ms=\d+\.\d")
        self.assertIn(response.headers["x-request-id"], captured.output[0])
        self.assertNotIn("/tasks/901", captured.output[0])
        self.assertNotIn("synthetic-query-secret", captured.output[0])

    def test_cross_origin_mutation_is_rejected_before_login_handler(self) -> None:
        response = self.client.post(
            "/login",
            data={"login": self.user.login, "password": "synthetic-integration-password"},
            headers={"Origin": "https://attacker.invalid"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.session.scalar_calls, 0)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_readyz_checks_lease_schema_and_applies_http_security_headers(self) -> None:
        columns = [
            "task_id",
            "owner_token",
            "job_kind",
            "attempt",
            "generation",
            "lease_until",
            "updated_at",
        ]
        with (
            patch.object(main, "engine", FakeDatabaseEngine(columns)),
            patch.object(main.settings, "ocr_task_leases_enabled", True),
            patch.object(main.Redis, "from_url", return_value=FakeRedisClient()),
        ):
            response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checks"]["ocr_lease_schema"], "ok")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertRegex(response.headers["x-request-id"], r"^[0-9a-f]{32}$")

    def test_readyz_returns_http_503_for_missing_lease_schema_without_details(self) -> None:
        with (
            patch.object(main, "engine", FakeDatabaseEngine([])),
            patch.object(main.settings, "ocr_task_leases_enabled", True),
            patch.object(main.Redis, "from_url", return_value=FakeRedisClient()),
        ):
            response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"]["ocr_lease_schema"], "unavailable")
        self.assertNotIn("information_schema", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_readyz_returns_http_503_when_queued_work_has_no_worker(self) -> None:
        self.queue_mock.side_effect = [
            SimpleNamespace(name="ocr", count=1),
            SimpleNamespace(name="sro", count=0),
        ]
        self.worker_mock.all.return_value = []
        with (
            patch.object(main, "engine", FakeDatabaseEngine()),
            patch.object(main.settings, "ocr_task_leases_enabled", False),
            patch.object(main.Redis, "from_url", return_value=FakeRedisClient()),
        ):
            response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"]["queue_workers"], "unavailable")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertRegex(response.headers["x-request-id"], r"^[0-9a-f]{32}$")
        self.assertNotIn("ocr", response.text)

    def test_readyz_warning_log_correlates_with_response_request_id(self) -> None:
        class FailingRedisClient(FakeRedisClient):
            def ping(self) -> bool:
                raise OSError("redis://synthetic-user:synthetic-secret@cache.invalid")

        redis_client = FailingRedisClient()
        with (
            patch.object(main, "engine", FakeDatabaseEngine()),
            patch.object(main.settings, "ocr_task_leases_enabled", False),
            patch.object(main.Redis, "from_url", return_value=redis_client),
            self.assertLogs(main.logger, level="WARNING") as captured,
        ):
            response = self.client.get("/readyz")

        request_id = response.headers["x-request-id"]
        self.assertEqual(response.status_code, 503)
        self.assertIn(f"request_id={request_id} dependency=redis error_type=OSError", captured.output[0])
        self.assertNotIn("synthetic-secret", " ".join(captured.output))
        self.assertNotIn("cache.invalid", " ".join(captured.output))


if __name__ == "__main__":
    unittest.main()
