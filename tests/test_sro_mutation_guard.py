"""The default SRO client must refuse state-changing controls."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from sro_robot.platform.client import SroClient
from sro_robot.platform.errors import SroError
from sro_robot.platform.registry import Registry


class SroMutationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SroClient(cfg=SimpleNamespace(), dry_run=True)
        self.client.page = MagicMock()

    def test_add_save_and_close_are_blocked_by_default(self) -> None:
        actions = (
            lambda: Registry(self.client, "/list").click_add(),
            self.client.click_save,
            self.client.click_close,
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaisesRegex(SroError, "disabled"):
                    action()
        self.client.page.locator.assert_not_called()
        self.client.page.get_by_text.assert_not_called()

    def test_add_like_text_is_blocked_before_browser_access(self) -> None:
        with self.assertRaisesRegex(SroError, "disabled"):
            self.client.click_text("Добавить адрес")
        self.client.page.locator.assert_not_called()
        self.client.page.get_by_text.assert_not_called()

    def test_prepare_only_allows_new_card_open_and_close_but_never_save(self) -> None:
        client = SroClient(cfg=SimpleNamespace(), dry_run=True, prepare_only=True)
        client.require_registry_mutations("Add")
        client.require_registry_mutations("Close")
        for action in ("Save", "Add address", "Delete", "Update"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(SroError, "permanently blocked"):
                    client.require_registry_mutations(action)

    def test_prepare_only_save_control_is_rejected_before_browser_access(self) -> None:
        client = SroClient(cfg=SimpleNamespace(), dry_run=True, prepare_only=True)
        client.page = MagicMock()
        with self.assertRaisesRegex(SroError, "permanently blocked"):
            client.click_save()
        client.page.locator.assert_not_called()

    def test_prepare_only_http_guard_allows_only_add_close_and_login_postbacks(self) -> None:
        client = SroClient(
            cfg=SimpleNamespace(base_url="https://example.test/default.aspx"),
            prepare_only=True,
        )

        def route_for(
            path: str,
            data,
            *,
            method: str = "POST",
            origin: str = "https://example.test",
        ):
            route = MagicMock()
            route.request = SimpleNamespace(
                method=method,
                url=origin + path,
                post_data=urlencode(data),
            )
            client._guard_prepare_only_request(route)
            return route

        add = route_for(
            "/register/person_list.aspx",
            {"__EVENTTARGET": "ctl00$pContent$BtnAddPerson"},
        )
        close = route_for(
            "/register/person_card.aspx",
            {"__EVENTTARGET": "ctl00$pContent$BtnItemPersonCancel"},
        )
        login = route_for(
            "/default.aspx",
            {"Login1$UserName": "operator", "Login1$LoginButton": "Вход"},
        )
        save = route_for(
            "/register/person_card.aspx",
            {"__EVENTTARGET": "ctl00$pContent$BtnItemPersonOk"},
        )

        for route in (add, close, login):
            route.continue_.assert_called_once()
            route.abort.assert_not_called()
        save.abort.assert_called_once_with("blockedbyclient")
        save.continue_.assert_not_called()

        event_login = route_for(
            "/default.aspx",
            {"__EVENTTARGET": "ctl00$Login1$LoginButton"},
        )
        other = route_for(
            "/default.aspx",
            {"__EVENTTARGET": "ctl00$pContent$BtnItemPersonOk"},
        )
        login_with_other_event = route_for(
            "/default.aspx",
            {
                "__EVENTTARGET": "ctl00$pContent$BtnItemPersonOk",
                "Login1$LoginButton": "Вход",
            },
        )
        duplicate_event_targets = route_for(
            "/register/person_list.aspx",
            [
                ("__EVENTTARGET", "ctl00$pContent$BtnAddPerson"),
                ("__EVENTTARGET", "ctl00$pContent$BtnItemPersonOk"),
            ],
        )
        get_read = route_for(
            "/register/person_card.aspx", {}, method="GET"
        )
        cross_origin_asset = route_for(
            "/assets/site.css", {}, method="GET", origin="https://static.example.test"
        )
        same_origin_delete = route_for(
            "/register/person_card.aspx", {}, method="DELETE"
        )
        cross_origin_post = route_for(
            "/api/person/save", {}, origin="https://api.example.test"
        )
        cross_origin_put = route_for(
            "/api/person/save", {}, method="PUT", origin="https://api.example.test"
        )
        event_login.continue_.assert_called_once()
        event_login.abort.assert_not_called()
        other.abort.assert_called_once_with("blockedbyclient")
        for route in (
            login_with_other_event,
            duplicate_event_targets,
            same_origin_delete,
            cross_origin_post,
            cross_origin_put,
        ):
            route.abort.assert_called_once_with("blockedbyclient")
            route.continue_.assert_not_called()
        for route in (get_read, cross_origin_asset):
            route.continue_.assert_called_once()
            route.abort.assert_not_called()

    def test_missing_credentials_keep_the_window_open_until_manual_login(self) -> None:
        client = SroClient(
            cfg=SimpleNamespace(
                login="",
                password="",
                base_url="https://example.test/default.aspx",
                timeout_ms=1000,
                selectors={},
            ),
            prepare_only=True,
        )
        client.page = MagicMock()
        client.settle = MagicMock()
        client.is_logged_in = MagicMock(side_effect=[False, False, False, True])
        client.page.is_closed.return_value = False
        with patch("sro_robot.platform.client.time.sleep") as sleep:
            client.ensure_logged_in()
        client.page.goto.assert_called_once()
        client.page.locator.assert_not_called()
        sleep.assert_called_once_with(1)

    def test_credentials_are_submitted_once_before_waiting(self) -> None:
        client = SroClient(
            cfg=SimpleNamespace(
                login="synthetic-user",
                password="synthetic-secret-value",
                base_url="https://example.test/default.aspx",
                timeout_ms=1000,
                selectors={},
            ),
            prepare_only=True,
        )
        client.page = MagicMock()
        client.settle = MagicMock()
        client.fill = MagicMock()
        client.el = MagicMock()
        client.is_logged_in = MagicMock(side_effect=[False, False, False, True])
        client.page.is_closed.return_value = False
        with patch("sro_robot.platform.client.time.sleep"):
            client.ensure_logged_in()
        self.assertEqual(client.fill.call_count, 2)
        client.el.return_value.click.assert_called_once()

    def test_login_timeout_message_does_not_include_the_password(self) -> None:
        client = SroClient(
            cfg=SimpleNamespace(
                login="synthetic-user",
                password="synthetic-secret-value",
                base_url="https://example.test/default.aspx",
                timeout_ms=1000,
                selectors={},
            ),
            prepare_only=True,
        )
        client.manual_login_timeout_s = 0
        client.page = MagicMock()
        client.settle = MagicMock()
        client.fill = MagicMock()
        client.el = MagicMock()
        client.is_logged_in = MagicMock(return_value=False)
        client.page.is_closed.return_value = False
        with self.assertRaises(SroError) as raised:
            client.ensure_logged_in()
        self.assertNotIn("synthetic-secret-value", str(raised.exception))
        self.assertIn("открытом окне", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
