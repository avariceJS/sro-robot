"""The default SRO client must refuse state-changing controls."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
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

        def route_for(path: str, data: dict[str, str]):
            route = MagicMock()
            route.request = SimpleNamespace(
                method="POST",
                url="https://example.test" + path,
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


if __name__ == "__main__":
    unittest.main()
