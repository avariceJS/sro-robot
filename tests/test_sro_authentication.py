"""The SRO login adapter allows one credential post and reports no page data."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import MagicMock, patch

from sro_robot.platform.authentication import (
    SroLoginError,
    authenticate_sro_credentials,
)
from sro_robot.platform.client import SroClient
from sro_robot.platform.errors import SroError


class _Locator:
    def __init__(self, selector: str, context: "_Context") -> None:
        self.selector = selector
        self.context = context

    def count(self) -> int:
        return 1

    def fill(self, value: str) -> None:
        self.context.filled[self.selector] = value

    def click(self) -> None:
        route = SimpleNamespace(
            request=SimpleNamespace(
                url="https://sroarbitr.sro-service.ru/default.aspx",
                method="POST",
                post_data=urlencode({"Login1$LoginButton": "Вход"}),
            ),
            continue_=MagicMock(),
            abort=MagicMock(),
        )
        self.context.route_handler(route)
        self.context.login_route = route

    def inner_text(self, timeout: int) -> str:
        return "Вы вошли как Synthetic Operator"


class _Page:
    def __init__(self, context: "_Context") -> None:
        self.context = context

    def locator(self, selector: str) -> _Locator:
        return _Locator(selector, self.context)

    def goto(self, url: str, **_kwargs) -> None:
        self.context.visited_url = url

    def wait_for_load_state(self, _state=None, **_kwargs) -> None:
        return None

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


class _Context:
    def __init__(self) -> None:
        self.filled: dict[str, str] = {}
        self.route_handler = None
        self.login_route = None
        self.visited_url = ""
        self.closed = False

    def set_default_timeout(self, _timeout: int) -> None:
        return None

    def route(self, _pattern: str, handler) -> None:
        self.route_handler = handler

    def new_page(self) -> _Page:
        return _Page(self)

    def cookies(self) -> list[dict[str, str]]:
        return [{"domain": "sroarbitr.sro-service.ru", "value": "synthetic-cookie"}]

    def storage_state(self) -> dict:
        return {"cookies": self.cookies(), "origins": []}

    def close(self) -> None:
        self.closed = True


class SroAuthenticationTests(unittest.TestCase):
    def test_one_ordinary_login_post_returns_only_browser_state(self) -> None:
        context = _Context()
        browser = MagicMock()
        browser.new_context.return_value = context
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=MagicMock(return_value=browser)))
        manager = MagicMock()
        manager.__enter__.return_value = playwright
        cfg = SimpleNamespace(
            base_url="https://sroarbitr.sro-service.ru/default.aspx",
            selectors={},
        )

        with patch("sro_robot.platform.authentication.sync_playwright", return_value=manager):
            result = authenticate_sro_credentials(
                "synthetic-login", "synthetic-password", cfg
            )

        self.assertEqual(result.sro_login, "synthetic-login")
        self.assertIn(b"sroarbitr.sro-service.ru", result.storage_state)
        self.assertNotIn(b"synthetic-password", result.storage_state)
        self.assertEqual(context.filled["#Login1_UserName"], "synthetic-login")
        self.assertEqual(context.filled["#Login1_Password"], "synthetic-password")
        self.assertTrue(context.login_route.continue_.called)
        self.assertTrue(context.closed)
        browser.close.assert_called_once()

    def test_non_https_identity_endpoint_is_rejected_before_browser_start(self) -> None:
        cfg = SimpleNamespace(base_url="http://sroarbitr.sro-service.ru/default.aspx")
        with patch("sro_robot.platform.authentication.sync_playwright") as start:
            with self.assertRaises(SroLoginError) as error:
                authenticate_sro_credentials("synthetic-login", "synthetic-password", cfg)
        self.assertEqual(error.exception.reason, "changed")
        start.assert_not_called()

    def test_authenticated_marker_with_unexpected_cookie_domain_fails_closed(self) -> None:
        context = _Context()
        browser = MagicMock()
        browser.new_context.return_value = context
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=MagicMock(return_value=browser)))
        manager = MagicMock()
        manager.__enter__.return_value = playwright
        cfg = SimpleNamespace(
            base_url="https://sroarbitr.sro-service.ru/default.aspx",
            selectors={},
        )

        with (
            patch.object(_Context, "cookies", return_value=[{"domain": "sro-service.ru"}]),
            patch("sro_robot.platform.authentication.sync_playwright", return_value=manager),
        ):
            with self.assertRaises(SroLoginError) as error:
                authenticate_sro_credentials(
                    "synthetic-login", "synthetic-password", cfg
                )

        self.assertEqual(error.exception.reason, "changed")
        self.assertTrue(context.closed)

    def test_invalid_failure_reason_cannot_carry_external_page_data(self) -> None:
        with self.assertRaises(ValueError):
            SroLoginError("synthetic-secret-value")

    def test_client_clears_decrypted_session_even_when_context_close_fails(self) -> None:
        events: list[str] = []

        class _Context:
            def close(self) -> None:
                events.append("context")
                raise RuntimeError("synthetic close failure")

        class _Browser:
            def close(self) -> None:
                events.append("browser")

        class _Playwright:
            def stop(self) -> None:
                events.append("playwright")

        client = SroClient(session_state=b"synthetic-state")
        client._ctx = _Context()
        client._browser = _Browser()
        client._pw = _Playwright()
        client.page = object()

        with self.assertRaisesRegex(RuntimeError, "synthetic close failure"):
            client.__exit__(None, None, None)

        self.assertEqual(events, ["context", "browser", "playwright"])
        self.assertIsNone(client.session_state)
        self.assertIsNone(client.page)
        self.assertIsNone(client._ctx)
        self.assertIsNone(client._browser)
        self.assertIsNone(client._pw)

    def test_expired_injected_session_fails_without_legacy_credential_fallback(self) -> None:
        class _Page:
            url = "https://sroarbitr.sro-service.ru/default.aspx"

            def content(self) -> str:
                return "SRO login form"

            def goto(self, _url: str, **_kwargs) -> None:
                return None

        client = SroClient(
            cfg=SimpleNamespace(
                base_url="https://sroarbitr.sro-service.ru/default.aspx",
                login="legacy-global-login",
                password="legacy-global-password",
            ),
            session_state=b"synthetic-session-state",
        )
        client.page = _Page()
        client.settle = lambda *_args: None

        with self.assertRaisesRegex(SroError, "сессия СРО истекла"):
            client.ensure_logged_in()


if __name__ == "__main__":
    unittest.main()
