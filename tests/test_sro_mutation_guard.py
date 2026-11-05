"""The default SRO client must refuse state-changing controls."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from sro_robot.platform.client import SroClient, PERSON_SAVE_TARGET, PERSON_SAVE_SELECTOR
from sro_robot.platform.errors import SroError
from sro_robot.platform.registry import Registry


class SroMutationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SroClient(cfg=SimpleNamespace(), dry_run=True)
        self.client.page = MagicMock()

    def test_registry_search_clicks_explicit_search_instead_of_default_submit(self):
        client = MagicMock()
        search = MagicMock()
        button = MagicMock()
        client.el.side_effect = lambda key: {"search_input": search, "search_button": button}[key]
        client.page.locator.return_value.count.return_value = 0
        self.assertEqual(Registry(client, "/register/person_list.aspx").search("synthetic"), [])
        search.fill.assert_called_once_with("synthetic")
        search.press.assert_not_called()
        button.click.assert_called_once()

    def test_prepare_guard_allows_only_explicit_person_registry_search(self):
        client = SroClient(cfg=SimpleNamespace(base_url="https://example.test/default.aspx"), prepare_only=True)
        target = "ctl00$pContent$ctrl_search$BtnSearch"
        cases = (
            ("/register/person_list.aspx", {"__EVENTTARGET": target}, True),
            ("/register/person_list.aspx", {"__EVENTTARGET": target, "ctl00$pContent$BtnAddPerson": "Add"}, False),
            ("/register/person_list.aspx", {"__EVENTTARGET": target, "__EVENTARGUMENT": "unexpected"}, False),
            ("/register/person_card.aspx", {"__EVENTTARGET": target}, False),
        )
        for path, payload, allowed in cases:
            with self.subTest(path=path, payload=payload):
                route = MagicMock()
                route.request = SimpleNamespace(method="POST", url="https://example.test" + path,
                                                post_data=urlencode(payload))
                client._guard_prepare_only_request(route)
                if allowed:
                    route.continue_.assert_called_once()
                    route.abort.assert_not_called()
                else:
                    route.abort.assert_called_once_with("blockedbyclient")

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

    def operator_client(self):
        client = SroClient(
            cfg=SimpleNamespace(base_url="https://example.test/default.aspx"),
            prepare_only=True, operator_save_enabled=True,
        )
        client.page = MagicMock()
        client.page.url = "https://example.test/register/person_card.aspx"
        client.settle = MagicMock()
        return client

    @staticmethod
    def save_route(client, values=None, *, url=None, frame=None):
        route = MagicMock()
        route.request = SimpleNamespace(
            method="POST", url=url or client.page.url,
            post_data=urlencode(values or {"__EVENTTARGET": PERSON_SAVE_TARGET}),
            frame=client.page.main_frame if frame is None else frame,
            response=lambda: SimpleNamespace(status=302),
        )
        client._guard_prepare_only_request(route)
        return route

    def test_operator_save_requires_prepare_guard_and_explicit_arming(self):
        with self.assertRaisesRegex(SroError, "guarded"):
            SroClient(cfg=SimpleNamespace(), operator_save_enabled=True)
        client = self.operator_client()
        with self.assertRaisesRegex(SroError, "not authorized"):
            client.click_operator_person_save()
        with self.assertRaisesRegex(SroError, "permanently blocked"):
            client.click_save(anchor_key="address_kind_select")
        client.page.locator.assert_not_called()
        self.save_route(client).abort.assert_called_once_with("blockedbyclient")

    def test_identified_form_requires_exact_blank_form_binding_from_add(self):
        client = self.operator_client()
        client.page.url += "?id=synthetic-new-id"
        with self.assertRaises(SroError):
            client.arm_operator_person_save(client.page.url)
        client._new_person_form_url = client.page.url
        client.arm_operator_person_save(client.page.url)
        self.assertEqual(client._operator_save_url, client.page.url)

    def test_add_binding_does_not_allow_another_identified_form(self):
        client = self.operator_client()
        client._new_person_form_url = client.page.url + "?id=synthetic-new-id"
        client.page.url += "?id=other-id"
        with self.assertRaises(SroError):
            client.arm_operator_person_save(client.page.url)

    def test_single_exact_save_request_is_burned_before_network_and_not_rearmed(self):
        for payload in (
            {"__EVENTTARGET": PERSON_SAVE_TARGET},
            {"__EVENTTARGET": "", PERSON_SAVE_TARGET: "Сохранить"},
        ):
            with self.subTest(payload=payload):
                client = self.operator_client()
                client.arm_operator_person_save(client.page.url)
                routes = []

                def save_click():
                    routes.append(self.save_route(client, payload))
                    routes.append(self.save_route(client, payload))

                client.page.locator.return_value.click.side_effect = save_click
                client.click_operator_person_save()
                routes[0].continue_.assert_called_once()
                routes[1].abort.assert_called_once_with("blockedbyclient")
                client.page.locator.assert_called_once_with(PERSON_SAVE_SELECTOR)
                with self.assertRaises(SroError):
                    client.click_operator_person_save()
                with self.assertRaises(SroError):
                    client.arm_operator_person_save(client.page.url)
                self.assertFalse(client.allow_registry_mutations)

    def test_save_timeout_consumes_attempt_without_retry(self):
        client = self.operator_client()
        client.arm_operator_person_save(client.page.url)
        client.page.locator.return_value.click.side_effect = RuntimeError("synthetic timeout")
        with self.assertRaises(RuntimeError):
            client.click_operator_person_save()
        with self.assertRaises(SroError):
            client.click_operator_person_save()
        self.save_route(client).abort.assert_called_once_with("blockedbyclient")
        client.page.locator.return_value.click.assert_called_once()

    def test_click_without_save_post_is_unknown_and_cannot_be_retried(self):
        client = self.operator_client()
        client.arm_operator_person_save(client.page.url)
        with self.assertRaisesRegex(SroError, "неизвестен"):
            client.click_operator_person_save()
        with self.assertRaises(SroError):
            client.click_operator_person_save()

    def test_failed_save_response_is_unknown_without_second_request(self):
        client = self.operator_client()
        client.arm_operator_person_save(client.page.url)
        routes = []
        def save_click():
            # Feed a real-shaped request through the guard; only its response
            # status differs from a successful server postback.
            route = self.save_route(client)
            route.request.response = lambda: SimpleNamespace(status=500)
            routes.append(route)
        client.page.locator.return_value.click.side_effect = save_click
        with self.assertRaisesRegex(SroError, "неизвестен"):
            client.click_operator_person_save()
        self.assertEqual(len(routes), 1)
        with self.assertRaises(SroError):
            client.click_operator_person_save()

    def test_operator_save_rejects_existing_card_or_changed_url(self):
        client = self.operator_client()
        for suffix in ("?id=123", "?ID=123", "?Id=123", "?id=", "?ID=", "#card"):
            client.page.url = "https://example.test/register/person_card.aspx" + suffix
            with self.subTest(suffix=suffix), self.assertRaises(SroError):
                client.arm_operator_person_save(client.page.url)
        client.page.url = "https://example.test/register/person_card.aspx"
        client.arm_operator_person_save(client.page.url)
        client.page.url += "?id=123"
        with self.assertRaises(SroError):
            client.click_operator_person_save()
        client.page.locator.assert_not_called()

    def test_armed_save_blocks_nested_or_ambiguous_posts_and_other_urls(self):
        client = self.operator_client()
        client.arm_operator_person_save(client.page.url)
        routes = []
        def save_click():
            for values in (
                {"__EVENTTARGET": "ctl00$pContent$BtnAddressSave"},
                {"__EVENTTARGET": PERSON_SAVE_TARGET, "ctl00$pContent$BtnAddressSave": "Save"},
                [("__EVENTTARGET", PERSON_SAVE_TARGET), ("__EVENTTARGET", PERSON_SAVE_TARGET)],
                [(PERSON_SAVE_TARGET, "Save"), (PERSON_SAVE_TARGET, "Save")],
                {"__EVENTTARGET": PERSON_SAVE_TARGET, "__EVENTARGUMENT": "other"},
            ):
                routes.append(self.save_route(client, values))
            routes.append(self.save_route(client, url=client.page.url + "?id=123"))
            routes.append(self.save_route(client, frame=object()))
        client.page.locator.return_value.click.side_effect = save_click
        with self.assertRaisesRegex(SroError, "неизвестен"):
            client.click_operator_person_save()
        for route in routes:
            route.abort.assert_called_once_with("blockedbyclient")
            route.continue_.assert_not_called()

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
        close_with_save_button = route_for(
            "/register/person_card.aspx",
            {"__EVENTTARGET": "ctl00$pContent$BtnItemPersonCancel", PERSON_SAVE_TARGET: "Save"},
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
        assignment_add_get = route_for(
            "/register/assignment_card.aspx", {}, method="GET"
        )
        assignment_empty_id_get = route_for(
            "/register/assignment_card.aspx?id=", {}, method="GET"
        )
        existing_assignment_get = route_for(
            "/register/assignment_card.aspx?id=123", {}, method="GET"
        )
        assignment_post = route_for(
            "/register/assignment_list.aspx",
            {"__EVENTTARGET": "ctl00$pContent$BtnAdd"},
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
            close_with_save_button,
            duplicate_event_targets,
            same_origin_delete,
            assignment_add_get,
            assignment_empty_id_get,
            assignment_post,
            cross_origin_post,
            cross_origin_put,
        ):
            route.abort.assert_called_once_with("blockedbyclient")
            route.continue_.assert_not_called()
        for route in (get_read, existing_assignment_get, cross_origin_asset):
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
