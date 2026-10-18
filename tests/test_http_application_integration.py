from __future__ import annotations

import unittest
from datetime import datetime
from collections.abc import Iterator
from contextlib import contextmanager
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from starlette.testclient import TestClient
from starlette.requests import Request
from starlette.middleware.sessions import SessionMiddleware
from fastapi.routing import APIRoute

from app.api.deps import SESSION_TOKEN_KEY, SESSION_USER_KEY, current_user
from app.api.routes_tasks import _dashboard_filters, _supported_upload_filename
from app.db.base import get_session
from app.db.models import IntakeSource, Task, TaskStatus, User
from app import main
from app.db.schema import EXPECTED_SCHEMA_REVISION, _RUNTIME_FUNCTIONS, _RUNTIME_GUARDS
from app.main import app
from app.services.auth import hash_password
from app.services import auth
from app.observability import access_logger
from app.security import (
    ActionRequestAdmissionMiddleware,
    LoginAttemptLimiterUnavailable,
    LoginAttemptLimiter,
    UploadAdmissionMiddleware,
)
from sro_robot.platform.authentication import SroLoginError


class FakeSession:
    """Minimal in-memory session for HTTP boundary tests; it never opens the DB."""

    def __init__(self, user: User | None = None) -> None:
        self.user = user
        self.get_calls: list[tuple[type, int]] = []
        self.scalar_calls = 0
        self.events = []
        self.commit_calls = 0
        self.rollback_calls = 0

    def add(self, event) -> None:
        self.events.append(event)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

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

    def query(self, _model: type):
        session = self
        class Query:
            def filter_by(self, **_filters):
                return self

            def one_or_none(self):
                return getattr(session, "internal_card", None)

        return Query()


class FakeResult:
    def __init__(self, values: list[str] | None = None) -> None:
        self.values = values or []

    def scalars(self):
        return self

    def all(self) -> list[str]:
        return self.values

    def first(self):
        return self.values[0] if self.values else None

    def scalar(self):
        value = self.first()
        return value[0] if isinstance(value, (tuple, list)) and value else value


def _fake_runtime_guard_rows():
    def function_source(name: str) -> str:
        check, expected = _RUNTIME_FUNCTIONS[name]
        if check == "exact":
            return expected
        if check == "fragments":
            return " ".join(expected)
        migration_name = {
            "sro_validate_event_insert": "e75a2c8d190b_require_audit_event_attribution.py",
            "sro_enforce_task_status_transition": "3b6a9f2d41c8_guard_task_status_transitions.py",
            "sro_revoke_sessions_on_user_deactivation": (
                "e3c1d5a7b904_revoke_sessions_for_inactive_accounts.py"
            ),
            "sro_guard_login_session_write": (
                "1fc64da92b1e_require_active_account_for_login_session.py"
            ),
            "sro_require_triggered_status_history_insert": (
                "b67d4c91a20e_require_triggered_status_history_inserts.py"
            ),
        }[name]
        migration_root = Path(__file__).resolve().parents[1] / "migrations/versions"
        migration_path = migration_root / migration_name
        migration = migration_path.read_text(encoding="utf-8")
        if name == "sro_validate_event_insert":
            source = migration.split("AS $$", 1)[1].split("$$", 1)[0]
            anchor = "    END CASE;\n\n    IF requires_actor AND NEW.user_id IS NULL THEN"
            replacement = "    END CASE;\n\n    IF NEW.kind NOT IN ('session_started', 'session_login_failed', 'session_ended') THEN\n        requires_task := true;\n    END IF;\n\n    IF requires_actor AND NEW.user_id IS NULL THEN"
            if source.count(anchor) != 1:
                raise AssertionError("audit event function source anchor is ambiguous")
            return source.replace(anchor, replacement, 1)
        return migration.split("AS $$", 1)[1].split("$$", 1)[0]

    rows = []
    for (table, name), (trigger_type, function_name, fragment) in _RUNTIME_GUARDS.items():
        rows.append(
            (
                table,
                name,
                trigger_type,
                "O",
                False,
                function_name,
                "public",
                f"CREATE TRIGGER {name} {fragment} FOR EACH ROW EXECUTE FUNCTION public.{function_name}()",
                function_source(function_name),
                "plpgsql",
                ["search_path=pg_catalog"],
                False,
            )
        )
    return rows


class FakeDatabaseConnection:
    def __init__(
        self,
        lease_columns: list[str] | None = None,
        schema_revisions: list[str] | None = None,
        metadata_constraints: list[tuple[str, bool, str]] | None = None,
    ) -> None:
        self.dialect = SimpleNamespace(name="sqlite")
        self.lease_columns = lease_columns or []
        self.schema_revisions = (
            [EXPECTED_SCHEMA_REVISION] if schema_revisions is None else schema_revisions
        )
        self.metadata_constraints = (
            [
                (
                    "c",
                    True,
                    "CHECK (source_metadata_sha256 IS NULL OR "
                    "source_metadata_sha256 ~ '^[0-9a-f]{64}$')",
                )
            ]
            if metadata_constraints is None
            else metadata_constraints
        )

    def execute(self, statement):
        if "FROM pg_trigger" in str(statement):
            return FakeResult(_fake_runtime_guard_rows())
        if "FROM pg_constraint" in str(statement):
            if "ck_debtor_card_draft_entity_kind" in str(statement):
                return FakeResult([
                    (
                        "ck_debtor_card_draft_entity_kind", "c", True,
                        "CHECK (((entity_kind)::text = ANY ((ARRAY['person'::character varying, "
                        "'legal_entity'::character varying, 'sole_proprietor'::character varying])::text[])))",
                    ),
                    (
                        "ck_debtor_card_draft_inn_length", "c", True,
                        "CHECK (((inn IS NULL) OR (((entity_kind)::text = 'person'::text) AND "
                        "(length((inn)::text) = 12)) OR (((entity_kind)::text = "
                        "'legal_entity'::text) AND (length((inn)::text) = 10)) OR "
                        "(((entity_kind)::text = 'sole_proprietor'::text) AND "
                        "(length((inn)::text) = 12))))",
                    ),
                ])
            if "ck_debtor_card_draft_preparation_state" in str(statement):
                return FakeResult([(
                    "c", True,
                    "CHECK (preparation_state IN ('draft', 'prepared', 'reviewed'))",
                )])
            if "assignment_draft" in str(statement):
                return FakeResult([
                    ("uq_assignment_draft_source_task", "u", True, "UNIQUE (source_task_id)"),
                    ("fk_assignment_draft_source_task", "f", True,
                     "FOREIGN KEY (source_task_id) REFERENCES task(id)"),
                ])
            return FakeResult(self.metadata_constraints)
        if "FROM pg_attribute" in str(statement) and "debtor_card_draft" in str(statement):
            return FakeResult([True])
        if "information_schema.columns" in str(statement):
            return FakeResult(self.lease_columns)
        if "FROM alembic_version" in str(statement):
            return FakeResult(self.schema_revisions)
        return FakeResult()


class FakeDatabaseEngine:
    def __init__(
        self,
        lease_columns: list[str] | None = None,
        schema_revisions: list[str] | None = None,
        metadata_constraints: list[tuple[str, bool, str]] | None = None,
    ) -> None:
        self.connection = FakeDatabaseConnection(
            lease_columns, schema_revisions, metadata_constraints
        )

    @contextmanager
    def connect(self):
        yield self.connection


class FakeRedisClient:
    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


def _api_routes(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _api_routes(included.routes)


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
        self.started_registry_patcher = patch.object(
            main,
            "StartedJobRegistry",
            side_effect=lambda _name, connection: SimpleNamespace(
                get_job_count=lambda cleanup=False: 0
            ),
        )
        self.queue_mock = self.queue_patcher.start()
        self.worker_mock = self.worker_patcher.start()
        self.started_registry_mock = self.started_registry_patcher.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.queue_patcher.stop()
        self.worker_patcher.stop()
        self.started_registry_patcher.stop()
        app.dependency_overrides.pop(get_session, None)

    def test_session_cookie_policy_is_explicit_on_the_application(self) -> None:
        from app.main import SESSION_MAX_AGE_SECONDS

        middleware = next(
            item for item in app.user_middleware if item.cls is SessionMiddleware
        )
        self.assertEqual(middleware.kwargs["max_age"], SESSION_MAX_AGE_SECONDS)
        self.assertEqual(middleware.kwargs["same_site"], "lax")
        self.assertIs(middleware.kwargs["https_only"], not main.settings.debug)

    def test_upload_body_limits_are_wired_into_the_application(self) -> None:
        middleware = next(
            item for item in app.user_middleware if item.cls is UploadAdmissionMiddleware
        )
        self.assertEqual(
            middleware.kwargs["max_request_bytes"], main.settings.upload_max_request_bytes
        )
        self.assertEqual(
            middleware.kwargs["max_concurrent_requests"],
            main.settings.upload_max_concurrent_requests,
        )
        self.assertEqual(
            middleware.kwargs["body_read_timeout_seconds"],
            main.settings.upload_body_read_timeout_seconds,
        )

    def test_action_form_limits_are_wired_after_origin_validation(self) -> None:
        middleware_order = [middleware.cls for middleware in app.user_middleware]
        self.assertLess(
            middleware_order.index(main.SameOriginMutationMiddleware),
            middleware_order.index(ActionRequestAdmissionMiddleware),
        )
        middleware = next(
            item for item in app.user_middleware if item.cls is ActionRequestAdmissionMiddleware
        )
        self.assertEqual(
            middleware.kwargs["max_request_bytes"], main.settings.action_max_request_bytes
        )
        self.assertEqual(
            middleware.kwargs["max_concurrent_requests"],
            main.settings.action_max_concurrent_requests,
        )
        self.assertEqual(
            middleware.kwargs["body_read_timeout_seconds"],
            main.settings.action_body_read_timeout_seconds,
        )

    def test_login_page_returns_security_and_private_cache_headers(self) -> None:
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertRegex(response.headers["x-request-id"], r"^[0-9a-f]{32}$")
        self.assertEqual(self.session.scalar_calls, 0)

    def test_rejected_sro_login_retains_identifier_without_password(self) -> None:
        with (
            patch(
                "app.api.routes_auth.authenticate_sro_credentials",
                side_effect=SroLoginError("rejected", stage="submit"),
            ),
            patch("app.api.routes_auth.login_attempt_limiter", LoginAttemptLimiter()),
        ):
            response = self.client.post(
                "/login",
                data={"login": "  synthetic operator  ", "password": "synthetic-secret"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 401)
        self.assertIn('value="synthetic operator"', response.text)
        self.assertNotIn("synthetic-secret", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.session.events[-1].kind, "session_login_failed")
        self.assertEqual(self.session.events[-1].payload, {"request_id": response.headers["x-request-id"]})

    def test_authenticated_login_and_logout_are_http_session_transitions(self) -> None:
        synthetic_state = b'{"cookies":[{"domain":"sroarbitr.sro-service.ru"}],"origins":[]}'
        with (
            patch(
                "app.api.routes_auth.authenticate_sro_credentials",
                return_value=SimpleNamespace(storage_state=synthetic_state, sro_login=self.user.sro_login),
            ),
            patch("app.api.routes_auth.auth.resolve_sro_user", return_value=self.user),
            patch("app.api.routes_auth.auth.attach_external_session"),
        ):
            login = self.client.post(
                "/login",
                data={"login": self.user.login, "password": "synthetic-integration-password"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )

        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/")
        self.assertIn("session", self.client.cookies)
        session_cookie = login.headers["set-cookie"].lower()
        self.assertIn("httponly", session_cookie)
        self.assertIn("samesite=lax", session_cookie)
        self.assertIn("max-age=28800", session_cookie)
        if not main.settings.debug:
            self.assertIn("secure", session_cookie)
        self.assertEqual(self.session.events[-1].kind, "session_started")
        self.assertEqual(self.session.events[-1].user_id, self.user.id)
        self.assertEqual(self.session.events[-1].payload, {"request_id": login.headers["x-request-id"]})
        self.assertTrue(any(hasattr(row, "token_sha256") for row in self.session.events))

        with patch("app.api.routes_auth.auth.revoke_login_session", return_value=True) as revoke:
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
        revoke.assert_called_once()
        self.assertEqual(revoke.call_args.args[1], self.user.id)
        self.assertRegex(revoke.call_args.args[2], r"^[A-Za-z0-9_-]{43}$")

    def test_login_racing_with_account_deactivation_does_not_issue_a_cookie(self) -> None:
        with (
            patch(
                "app.api.routes_auth.authenticate_sro_credentials",
                return_value=SimpleNamespace(storage_state=b"synthetic", sro_login=self.user.sro_login),
            ),
            patch("app.api.routes_auth.auth.resolve_sro_user", return_value=self.user),
            patch(
                "app.api.routes_auth.auth.new_login_session",
                side_effect=auth.InactiveUser,
            ),
            patch(
                "app.api.routes_auth.login_attempt_limiter",
                LoginAttemptLimiter(),
            ),
        ):
            response = self.client.post(
                "/login",
                data={
                    "login": self.user.login,
                    "password": "synthetic-integration-password",
                },
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 401)
        self.assertNotIn(SESSION_USER_KEY, self.client.cookies)
        self.assertEqual(self.session.rollback_calls, 1)
        self.assertEqual(self.session.commit_calls, 1)
        self.assertEqual([event.kind for event in self.session.events], ["session_login_failed"])
        self.assertIsNone(self.session.events[0].user_id)
        self.assertNotIn(self.user.login, repr(self.session.events[0].payload))

    def test_verified_login_does_not_issue_cookie_when_external_state_cannot_be_stored(self) -> None:
        with (
            patch(
                "app.api.routes_auth.authenticate_sro_credentials",
                return_value=SimpleNamespace(storage_state=b"synthetic", sro_login=self.user.sro_login),
            ),
            patch("app.api.routes_auth.auth.resolve_sro_user", return_value=self.user),
            patch(
                "app.api.routes_auth.auth.attach_external_session",
                side_effect=ValueError("synthetic encryption setup failure"),
            ),
            patch("app.api.routes_auth.login_attempt_limiter", LoginAttemptLimiter()),
        ):
            response = self.client.post(
                "/login",
                data={"login": self.user.login, "password": "synthetic-integration-password"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("session", self.client.cookies)
        self.assertEqual(self.session.rollback_calls, 1)
        self.assertEqual(len(self.session.events), 1)
        self.assertFalse(hasattr(self.session.events[0], "kind"))

    def test_concurrent_first_login_reloads_identity_after_unique_index_race(self) -> None:
        with (
            patch(
                "app.api.routes_auth.authenticate_sro_credentials",
                return_value=SimpleNamespace(
                    storage_state=b"synthetic-state", sro_login=self.user.sro_login
                ),
            ),
            patch(
                "app.api.routes_auth.auth.resolve_sro_user",
                side_effect=[
                    IntegrityError("synthetic", {}, RuntimeError("unique index")),
                    self.user,
                ],
            ) as resolve,
            patch("app.api.routes_auth.auth.attach_external_session"),
            patch("app.api.routes_auth.login_attempt_limiter", LoginAttemptLimiter()),
        ):
            response = self.client.post(
                "/login",
                data={"login": self.user.login, "password": "synthetic-integration-password"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(self.session.rollback_calls, 1)
        self.assertIn("session", self.client.cookies)

    def test_failed_login_is_audited_without_credentials_or_attempted_identity(self) -> None:
        with (
            patch(
                "app.api.routes_auth.authenticate_sro_credentials",
                side_effect=SroLoginError("rejected"),
            ),
            patch("app.api.routes_auth.login_attempt_limiter", LoginAttemptLimiter()),
        ):
            response = self.client.post(
                "/login",
                data={"login": "synthetic-attempted-identity", "password": "synthetic-secret-value"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.session.events), 1)
        event = self.session.events[0]
        self.assertEqual(event.kind, "session_login_failed")
        self.assertIsNone(event.task_id)
        self.assertIsNone(event.user_id)
        self.assertEqual(event.payload, {"request_id": response.headers["x-request-id"]})
        audit_record = f"{event.message} {event.payload!r}"
        self.assertNotIn("synthetic-attempted-identity", audit_record)
        self.assertNotIn("synthetic-secret-value", audit_record)
        self.assertEqual(self.session.commit_calls, 1)

    def test_repeated_login_attempts_are_throttled_before_authentication_and_audit(self) -> None:
        limiter = LoginAttemptLimiter(max_attempts=2, window_seconds=60)
        with (
            patch(
                "app.api.routes_auth.authenticate_sro_credentials",
                side_effect=SroLoginError("rejected"),
            ) as authenticate,
            patch("app.api.routes_auth.login_attempt_limiter", limiter),
        ):
            first = self.client.post(
                "/login",
                data={"login": "Synthetic Operator", "password": "wrong"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )
            second = self.client.post(
                "/login",
                data={"login": "synthetic  operator", "password": "wrong"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )
            blocked = self.client.post(
                "/login",
                data={"login": "synthetic operator", "password": "wrong"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )

        self.assertEqual(
            (first.status_code, second.status_code, blocked.status_code),
            (401, 401, 429),
        )
        self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)
        self.assertEqual(authenticate.call_count, 2)
        self.assertEqual(self.session.commit_calls, 2)
        self.assertEqual(len(self.session.events), 2)

    def test_unavailable_shared_login_limiter_fails_closed_before_authentication(self) -> None:
        limiter = MagicMock()
        limiter.reserve.side_effect = LoginAttemptLimiterUnavailable(
            "synthetic redis connection detail"
        )
        with (
            patch("app.api.routes_auth.authenticate_sro_credentials") as authenticate,
            patch("app.api.routes_auth.login_attempt_limiter", limiter),
            self.assertLogs("app.api.routes_auth", level="WARNING") as captured,
        ):
            response = self.client.post(
                "/login",
                data={"login": "synthetic operator", "password": "synthetic secret"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "1")
        self.assertNotIn("synthetic redis connection detail", response.text)
        self.assertNotIn("synthetic secret", response.text)
        self.assertNotIn("synthetic redis connection detail", " ".join(captured.output))
        authenticate.assert_not_called()
        self.assertEqual(self.session.commit_calls, 0)
        self.assertEqual(self.session.events, [])

    def test_shared_login_limiter_completion_failure_does_not_audit_bad_password(self) -> None:
        limiter = MagicMock()
        limiter.reserve.return_value = (b"synthetic-attempt-token", None)
        limiter.finish.side_effect = LoginAttemptLimiterUnavailable(
            "synthetic redis completion detail"
        )
        with (
            patch(
                "app.api.routes_auth.authenticate_sro_credentials",
                side_effect=SroLoginError("rejected"),
            ) as authenticate,
            patch("app.api.routes_auth.login_attempt_limiter", limiter),
        ):
            response = self.client.post(
                "/login",
                data={"login": "synthetic operator", "password": "synthetic secret"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "1")
        authenticate.assert_called_once()
        self.assertEqual(self.session.commit_calls, 0)
        self.assertEqual(self.session.events, [])

    def test_authentication_backend_error_releases_login_reservation(self) -> None:
        limiter = LoginAttemptLimiter(max_attempts=1, window_seconds=60)
        client = TestClient(app, raise_server_exceptions=False)
        try:
            with (
                patch(
                    "app.api.routes_auth.authenticate_sro_credentials",
                    side_effect=[RuntimeError("synthetic browser outage"), SroLoginError("rejected")],
                ) as authenticate,
                patch("app.api.routes_auth.login_attempt_limiter", limiter),
            ):
                failed_backend = client.post(
                    "/login",
                    data={"login": "synthetic operator", "password": "wrong"},
                    headers={"Origin": "http://testserver"},
                    follow_redirects=False,
                )
                actual_bad_password = client.post(
                    "/login",
                    data={"login": "synthetic operator", "password": "wrong"},
                    headers={"Origin": "http://testserver"},
                    follow_redirects=False,
                )
        finally:
            client.close()

        self.assertEqual((failed_backend.status_code, actual_bad_password.status_code), (503, 401))
        self.assertEqual(authenticate.call_count, 2)
        self.assertEqual(self.session.commit_calls, 1)
        self.assertEqual(len(self.session.events), 1)

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

    def test_dashboard_cursor_requires_one_complete_timezone_aware_key(self) -> None:
        def request_for(query: str) -> Request:
            return Request({
                "type": "http", "method": "GET", "path": "/debtors/intake",
                "query_string": query.encode(), "headers": [], "scheme": "http",
                "server": ("testserver", 80), "client": ("testclient", 1),
                "root_path": "",
            })

        valid = _dashboard_filters(request_for(
            "status=failed&before_created_at=2026-10-18T10%3A00%3A00%2B00%3A00&before_id=42"
        ))
        self.assertEqual(valid, (
            None, "failed", 1,
            ("before", datetime.fromisoformat("2026-10-18T10:00:00+00:00"), 42),
        ))
        maximum_operation = _dashboard_filters(request_for("intake=2147483647"))
        self.assertEqual(maximum_operation, ([2_147_483_647], None, 1, None))
        for query in (
            "intake=2147483648",
            "intake=9999999999999999999",
            "intake=0",
            "before_id=42",
            "before_created_at=2026-10-18T10%3A00%3A00&before_id=42",
            "before_created_at=bad&before_id=42",
            "before_created_at=2026-10-18T10%3A00%3A00%2B00%3A00&before_id=2147483648",
            "before_created_at=2026-10-18T10%3A00%3A00%2B00%3A00&before_id=42&page=2",
            "before_created_at=2026-10-18T10%3A00%3A00%2B00%3A00&before_id=42&after_created_at=2026-10-18T10%3A00%3A00%2B00%3A00&after_id=42",
        ):
            with self.subTest(query=query), self.assertRaises(HTTPException):
                _dashboard_filters(request_for(query))

    def test_task_page_launches_kad_in_the_employee_browser(self) -> None:
        app.dependency_overrides[current_user] = lambda: self.user
        with (
            patch("app.api.routes_tasks.bulk.bulk_task_context", return_value={
                "task_rows": [],
                "counts": {
                    "total": 0, "queued": 0, "processing": 0, "prepared": 0,
                    "needs_review": 0, "failed": 0, "other": 0,
                },
                "should_poll": False, "page": 1, "pages": 1,
                "oldest_queued_age_seconds": None,
            }),
        ):
            try:
                response = self.client.get("/debtors/intake", follow_redirects=False)
            finally:
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 200)
        keys = re.findall(r'name="idempotency_key" value="([0-9a-f]+)"', response.text)
        self.assertEqual(len(keys), 1)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{48}", key) for key in keys))
        self.assertIn('id="kad-browser-search"', response.text)
        html_ids = re.findall(r'\bid="([^"]+)"', response.text)
        self.assertEqual(len(html_ids), len(set(html_ids)))
        self.assertIn('<form id="document-upload-form"', response.text)
        self.assertIn('src="http://testserver/static/staff-controls.js" defer', response.text)
        self.assertNotRegex(response.text, r"<script>\s*[^<]")
        self.assertIn('action="http://testserver/kad-helper"', response.text)
        self.assertIn('kad-helper-launch.js', response.text)
        self.assertNotIn('action="http://testserver/case-search"', response.text)

    def test_upload_form_matches_server_supported_ocr_extensions(self) -> None:
        app.dependency_overrides[current_user] = lambda: self.user
        with patch("app.api.routes_tasks.bulk.bulk_task_context", return_value={
            "task_rows": [],
            "counts": {"total": 0, "queued": 0, "processing": 0, "prepared": 0,
                       "needs_review": 0, "failed": 0, "other": 0},
            "should_poll": False, "page": 1, "pages": 1,
            "oldest_queued_age_seconds": None,
        }):
            try:
                response = self.client.get("/debtors/intake")
            finally:
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 200)
        self.assertIn('accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.bmp,.webp"', response.text)
        self.assertTrue(_supported_upload_filename("Synthetic.PDF"))
        self.assertTrue(_supported_upload_filename("Synthetic.webp"))
        self.assertFalse(_supported_upload_filename("Synthetic.heic"))
        self.assertFalse(_supported_upload_filename("Synthetic.gif"))

    def test_upload_route_refuses_unsupported_type_before_creating_intake(self) -> None:
        app.dependency_overrides[current_user] = lambda: self.user
        with patch("app.api.routes_tasks.tasks.begin_intake_operation") as begin_operation:
            try:
                response = self.client.post(
                    "/upload",
                    data={"idempotency_key": "a" * 48},
                    files=[("files", ("synthetic.heic", b"synthetic", "image/heic"))],
                    headers={"Origin": "http://testserver"},
                )
            finally:
                app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 422)
        self.assertIn("Поддерживаются PDF", response.json()["detail"])
        begin_operation.assert_not_called()

    def test_task_list_uses_one_batch_poll_and_stops_when_all_rows_are_terminal(self) -> None:
        def dashboard(active: bool):
            task = SimpleNamespace(
                id=901, status=TaskStatus.recognizing if active else TaskStatus.needs_review,
                case_number=None, intake_item=None,
                document=SimpleNamespace(filename="synthetic-order.pdf", source=IntakeSource.upload),
            )
            return {
                "task_rows": [{
                    "task": task, "summary": "Synthetic Person", "status_ru": "Распознаётся",
                    "internal_card_id": 45 if active else None,
                    "internal_card_state": "prepared" if active else None,
                    "preparation_reason": "", "possible_card_id": None,
                }],
                "counts": {
                    "total": 1, "queued": 0, "processing": 1 if active else 0,
                    "prepared": 0, "needs_review": 0 if active else 1, "failed": 0, "other": 0,
                },
                "should_poll": active, "page": 1, "pages": 2 if active else 1,
                "oldest_queued_age_seconds": 75 if active else None,
                "previous_cursor": None,
                "next_cursor": (datetime.fromisoformat("2026-10-18T10:00:00+00:00"), 902)
                if active else None,
            }
        app.dependency_overrides[current_user] = lambda: self.user
        try:
            with patch("app.api.routes_tasks.bulk.bulk_task_context", return_value=dashboard(True)):
                page = self.client.get("/debtors/intake?intake=51&status=processing", follow_redirects=False)
            with patch("app.api.routes_tasks.bulk.bulk_task_context", return_value=dashboard(False)):
                stopped = self.client.get(
                    "/tasks/status-list?intake=51&status=processing",
                    headers={"HX-Request": "true"}, follow_redirects=False,
                )
        finally:
            app.dependency_overrides.pop(current_user, None)

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text.count("hx-get="), 1)
        self.assertIn('id="task-dashboard"', page.text)
        self.assertIn('hx-get="http://testserver/tasks/status-list?intake=51&amp;status=processing"', page.text)
        self.assertIn(
            'href="http://testserver/debtors/intake?intake=51&amp;status=processing'
            '&amp;before_created_at=2026-10-18T10%3A00%3A00%2B00%3A00&amp;before_id=902"',
            page.text,
        )
        self.assertIn("synthetic-order.pdf", page.text)
        self.assertIn("upload", page.text)
        self.assertIn("Всего", page.text)
        self.assertIn("Самое старое задание ожидает обработки", page.text)
        self.assertIn("1 мин.", page.text)
        self.assertNotIn("1 мин..", page.text)
        self.assertIn('href="http://testserver/debtors/45"', page.text)
        self.assertNotIn("/tasks/901/status", page.text)
        self.assertEqual(stopped.status_code, 286)
        self.assertNotIn("hx-get=", stopped.text)
        self.assertIn("/tasks/901", stopped.text)

    def test_review_page_contains_review_token_and_internal_card_action(self) -> None:
        task = SimpleNamespace(
            id=904,
            status=TaskStatus.needs_review,
            scenario="person_card",
            dry_run=True,
            error="",
            screenshots=[],
            browser_decision="",
            recognized={"_internal_preparation_v1": {"state": "needs_review", "reason": "debtor_role_uncertain"}},
            document=SimpleNamespace(filename="synthetic-review.pdf"),
        )
        app.dependency_overrides[current_user] = lambda: self.user
        try:
            with (
                patch("app.api.routes_tasks._require_task", return_value=task),
                patch("app.api.routes_tasks.fields.review_fields", return_value=[]),
                patch("app.api.routes_tasks.tasks.status_label", return_value="Проверить"),
                patch("app.api.routes_tasks.tasks.review_token", return_value="synthetic-review-token"),
            ):
                response = self.client.get("/tasks/904", follow_redirects=False)
        finally:
            app.dependency_overrides.pop(current_user, None)
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="review_token" value="synthetic-review-token"', response.text)
        self.assertIn("Подтвердить и подготовить внутреннюю карточку", response.text)
        self.assertIn("Карточка не подготовлена автоматически", response.text)
        self.assertIn("Не удалось надёжно связать ФИО с ролью должника", response.text)

    def test_queued_task_does_not_offer_review_actions_before_ocr(self) -> None:
        task = SimpleNamespace(
            id=906, status=TaskStatus.new, scenario="person_card", dry_run=True,
            error="", screenshots=[], browser_decision="", recognized={},
            document=SimpleNamespace(filename="synthetic-queued.pdf"),
        )
        review_field = SimpleNamespace(
            attr="last_name", title="Фамилия", value="Synthetic", confidence=0.8,
            warning="", evidence_status="recognized", required=False, choices=(),
            source_variant=None, source_start=None, source_end=None,
            source_excerpt="", uncertain=False,
        )
        app.dependency_overrides[current_user] = lambda: self.user
        try:
            with (
                patch("app.api.routes_tasks._require_task", return_value=task),
                patch("app.api.routes_tasks.fields.review_fields", return_value=[review_field]),
                patch("app.api.routes_tasks.tasks.status_label", return_value="В очереди"),
                patch("app.api.routes_tasks.tasks.review_token", return_value="synthetic-review-token"),
            ):
                response = self.client.get("/tasks/906", follow_redirects=False)
                task.status = TaskStatus.recognizing
                recognizing_response = self.client.get("/tasks/906", follow_redirects=False)
        finally:
            app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('formaction="http://testserver/tasks/906/save"', response.text)
        self.assertNotIn('formaction="http://testserver/tasks/906/approve"', response.text)
        self.assertIn('name="last_name" id="last_name" value="Synthetic" disabled', response.text)
        self.assertIn('hx-get="http://testserver/tasks/906/status?since=new"', response.text)
        self.assertIn("Документ ожидает распознавания", response.text)
        self.assertIn('href="http://testserver/tasks/906/confirm-reject"', response.text)

        self.assertEqual(recognizing_response.status_code, 200)
        self.assertNotIn('formaction="http://testserver/tasks/906/save"', recognizing_response.text)
        self.assertIn('name="last_name" id="last_name" value="Synthetic" disabled', recognizing_response.text)
        self.assertIn("Документ распознаётся", recognizing_response.text)

    def test_review_page_links_an_existing_internal_card(self) -> None:
        task = SimpleNamespace(
            id=905, status=TaskStatus.needs_review, scenario="person_card", dry_run=True,
            error="", screenshots=[], browser_decision="", recognized={},
            document=SimpleNamespace(filename="synthetic-prepared.pdf"),
        )
        self.session.internal_card = SimpleNamespace(id=88)
        app.dependency_overrides[current_user] = lambda: self.user
        try:
            with (
                patch("app.api.routes_tasks._require_task", return_value=task),
                patch("app.api.routes_tasks.fields.review_fields", return_value=[]),
                patch("app.api.routes_tasks.tasks.status_label", return_value="Проверить"),
                patch("app.api.routes_tasks.tasks.review_token", return_value="synthetic-review-token"),
            ):
                response = self.client.get("/tasks/905", follow_redirects=False)
        finally:
            app.dependency_overrides.pop(current_user, None)
            del self.session.internal_card
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="http://testserver/debtors/88"', response.text)
        self.assertNotIn("Подтвердить и подготовить внутреннюю карточку", response.text)

    def test_bulk_dashboard_rejects_unbounded_or_malformed_filters(self) -> None:
        app.dependency_overrides[current_user] = lambda: self.user
        try:
            responses = [
                self.client.get("/debtors/intake?intake=" + ",".join(str(number) for number in range(1, 102))),
                self.client.get("/debtors/intake?intake=1,1"),
                self.client.get("/debtors/intake?intake=0"),
                self.client.get("/debtors/intake?status=anything"),
                self.client.get("/debtors/intake?page=0"),
            ]
        finally:
            app.dependency_overrides.pop(current_user, None)
        self.assertEqual([response.status_code for response in responses], [422] * 5)

    def test_home_opens_business_worklist_instead_of_processing_dashboard(self) -> None:
        app.dependency_overrides[current_user] = lambda: self.user
        try:
            response = self.client.get("/", follow_redirects=False)
        finally:
            app.dependency_overrides.pop(current_user, None)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/debtors")

    def test_terminal_task_status_response_uses_htmx_poll_stop_code(self) -> None:
        terminal_task = SimpleNamespace(
            id=903,
            status=TaskStatus.needs_review,
            error=None,
            record_url=None,
        )
        app.dependency_overrides[current_user] = lambda: self.user
        try:
            with (
                patch("app.api.routes_tasks._require_task", return_value=terminal_task),
                patch("app.api.routes_tasks.tasks.status_label", return_value="Проверить"),
            ):
                response = self.client.get(
                    "/tasks/903/status", headers={"HX-Request": "true"}, follow_redirects=False
                )
                plain_response = self.client.get("/tasks/903/status", follow_redirects=False)
        finally:
            app.dependency_overrides.pop(current_user, None)

        self.assertEqual(response.status_code, 286)
        self.assertIn('data-task-state="needs_review"', response.text)
        self.assertEqual(plain_response.status_code, 200)

    def test_task_status_refreshes_page_when_polled_state_changes(self) -> None:
        task = SimpleNamespace(id=907, status=TaskStatus.awaiting_preview, error="", record_url="")
        app.dependency_overrides[current_user] = lambda: self.user
        try:
            with (
                patch("app.api.routes_tasks._require_task", return_value=task),
                patch("app.api.routes_tasks.tasks.status_label", return_value="Ожидает preview"),
            ):
                entered_preview = self.client.get(
                    "/tasks/907/status?since=writing",
                    headers={"HX-Request": "true"}, follow_redirects=False,
                )
                unchanged = self.client.get(
                    "/tasks/907/status?since=awaiting_preview",
                    headers={"HX-Request": "true"}, follow_redirects=False,
                )
                invalid_baseline = self.client.get(
                    "/tasks/907/status?since=unknown",
                    headers={"HX-Request": "true"}, follow_redirects=False,
                )
                task.status = TaskStatus.needs_review
                completed_ocr = self.client.get(
                    "/tasks/907/status?since=awaiting_preview",
                    headers={"HX-Request": "true"}, follow_redirects=False,
                )
        finally:
            app.dependency_overrides.pop(current_user, None)

        self.assertEqual(entered_preview.status_code, 200)
        self.assertEqual(entered_preview.headers.get("HX-Refresh"), "true")
        self.assertIsNone(unchanged.headers.get("HX-Refresh"))
        self.assertIsNone(invalid_baseline.headers.get("HX-Refresh"))
        self.assertEqual(completed_ocr.status_code, 286)
        self.assertEqual(completed_ocr.headers.get("HX-Refresh"), "true")

    def test_every_task_route_redirects_unauthenticated_requests_before_task_lookup(self) -> None:
        task_routes = [
            route
            for route in _api_routes(app.routes)
            if "tasks" in (route.tags or [])
        ]
        self.assertTrue(task_routes, "the task router must be registered")

        for route in task_routes:
            for method in route.methods or []:
                path = re.sub(r"\{[^}]+\}", "901", route.path)
                with self.subTest(method=method, path=route.path):
                    response = self.client.request(
                        method,
                        path,
                        headers={"Origin": "http://testserver"} if method == "POST" else None,
                        follow_redirects=False,
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
        self.assertEqual(response.json()["checks"]["document_storage"], "ok")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_readyz_fails_closed_when_intake_metadata_constraint_drifts(self) -> None:
        invalid_constraints = (
            [],
            [
                (
                    "c",
                    True,
                    "CHECK (source_metadata_sha256 IS NULL OR "
                    "source_metadata_sha256 ~ '^[0-9a-f]{64}$' OR true)",
                )
            ],
        )
        for constraints in invalid_constraints:
            with self.subTest(constraints=constraints):
                with (
                    patch.object(
                        main,
                        "engine",
                        FakeDatabaseEngine(metadata_constraints=constraints),
                    ),
                    patch.object(main.settings, "ocr_task_leases_enabled", False),
                    patch.object(main.Redis, "from_url", return_value=FakeRedisClient()),
                ):
                    response = self.client.get("/readyz")

            self.assertEqual(response.status_code, 503)
            self.assertEqual(
                response.json()["checks"]["database_guards"],
                "unavailable",
            )
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

    def test_readyz_returns_http_503_when_document_storage_is_unavailable(self) -> None:
        with (
            patch.object(main, "_document_storage_is_ready", return_value=False),
            patch.object(main, "engine", FakeDatabaseEngine()),
            patch.object(main.settings, "ocr_task_leases_enabled", False),
            patch.object(main.Redis, "from_url", return_value=FakeRedisClient()),
            self.assertLogs(main.logger, level="WARNING") as captured,
        ):
            response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"]["document_storage"], "unavailable")
        self.assertIn(
            "dependency=document_storage error_type=DocumentStorageUnavailable",
            captured.output[0],
        )
        self.assertNotIn("storage/documents", response.text)

    def test_readyz_returns_http_503_for_outdated_or_unversioned_database(self) -> None:
        for revisions in ([], ["old-revision"], [EXPECTED_SCHEMA_REVISION, "unexpected"]):
            with self.subTest(revisions=revisions):
                with (
                    patch.object(main, "engine", FakeDatabaseEngine(schema_revisions=revisions)),
                    patch.object(main.settings, "ocr_task_leases_enabled", False),
                    patch.object(main.Redis, "from_url", return_value=FakeRedisClient()),
                ):
                    response = self.client.get("/readyz")

                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["checks"]["database_schema"], "unavailable")
                self.assertNotIn("old-revision", response.text)

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

    def test_readyz_detects_started_job_when_waiting_queue_is_empty(self) -> None:
        self.queue_mock.side_effect = [
            SimpleNamespace(name="ocr", count=0),
            SimpleNamespace(name="sro", count=0),
        ]
        self.started_registry_mock.side_effect = lambda name, connection: SimpleNamespace(
            get_job_count=lambda cleanup=False: 1 if name == "ocr" else 0
        )
        self.worker_mock.all.return_value = []
        with (
            patch.object(main, "engine", FakeDatabaseEngine()),
            patch.object(main.settings, "ocr_task_leases_enabled", False),
            patch.object(main.Redis, "from_url", return_value=FakeRedisClient()),
        ):
            response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"]["queue_workers"], "unavailable")

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
