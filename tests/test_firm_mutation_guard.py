"""The firm preview HTTP guard admits no unapproved registry write."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from sro_robot.platform.client import (
    FIRM_ADDRESS_ADD_TARGET, FIRM_ADDRESS_CANCEL_TARGET,
    FIRM_ADDRESS_SAVE_SELECTOR, FIRM_ADDRESS_SAVE_TARGET,
    FIRM_SAVE_SELECTOR, FIRM_SAVE_TARGET, SroClient,
)
from sro_robot.platform.errors import SroError


BASE = "https://synthetic.invalid"
FORM = BASE + "/register/firm_card.aspx?id=fresh-opaque"


def _route(client, path, payload, *, frame=None):
    route = MagicMock()
    route.request = SimpleNamespace(
        method="POST", url=BASE + path, post_data=urlencode(payload),
        frame=client.page.main_frame if frame is None else frame,
        response=lambda: SimpleNamespace(status=302),
    )
    client._guard_prepare_only_request(route)
    return route


class FirmMutationGuardTests(unittest.TestCase):
    def setUp(self):
        self.qualification = patch("sro_robot.platform.client.FIRM_PREVIEW_QUALIFIED", True)
        self.qualification.start()

    def tearDown(self):
        self.qualification.stop()

    def client(self, *, operator_save_enabled=True, owned_add_required=True):
        client = SroClient(
            cfg=SimpleNamespace(base_url=BASE + "/default.aspx"),
            prepare_only=True, operator_save_enabled=operator_save_enabled,
            owned_add_required=owned_add_required,
        )
        client.page = MagicMock()
        client.page.url = FORM
        client.settle = MagicMock()
        return client

    def test_only_firm_search_add_close_are_allowed_without_save(self):
        client = self.client()
        search = "ctl00$pContent$ctrl_search$BtnSearch"
        add = "ctl00$pContent$BtnAddFirm"
        close = "ctl00$pContent$BtnItemFirmCancel"
        _route(client, "/register/firm_list.aspx", {"__EVENTTARGET": search}).continue_.assert_called_once()
        add_client = self.client(operator_save_enabled=False, owned_add_required=True)
        _route(add_client, "/register/firm_list.aspx", {"__EVENTTARGET": add}).abort.assert_called_once_with("blockedbyclient")
        add_client.arm_owned_firm_add()
        _route(add_client, "/register/firm_list.aspx", {"__EVENTTARGET": add}).continue_.assert_called_once()
        _route(add_client, "/register/firm_list.aspx", {"__EVENTTARGET": add}).abort.assert_called_once_with("blockedbyclient")
        with self.assertRaises(SroError):
            add_client.arm_owned_firm_add()
        _route(client, "/register/firm_card.aspx?id=fresh-opaque", {"__EVENTTARGET": close}).abort.assert_called_once_with("blockedbyclient")
        client._new_firm_form_url = FORM
        _route(client, "/register/firm_card.aspx?id=fresh-opaque", {"__EVENTTARGET": close}).continue_.assert_called_once()
        for path, payload in (
            ("/register/firm_card.aspx?id=fresh-opaque", {"__EVENTTARGET": FIRM_SAVE_TARGET}),
            ("/register/firm_card.aspx?id=fresh-opaque", {"__EVENTTARGET": "ctl00$pContent$BtnOkItemFirmFinder"}),
            ("/register/firm_list.aspx", {"__EVENTTARGET": search, "ctl00$pContent$BtnAddFirm": "Add"}),
            ("/register/firm_list.aspx", {"__EVENTTARGET": add, "__EVENTARGUMENT": "unexpected"}),
        ):
            with self.subTest(payload=payload):
                _route(client, path, payload).abort.assert_called_once_with("blockedbyclient")

    def test_unqualified_firm_add_is_blocked_before_network(self):
        client = self.client()
        with patch("sro_robot.platform.client.FIRM_PREVIEW_QUALIFIED", False):
            _route(client, "/register/firm_list.aspx", {
                "__EVENTTARGET": "ctl00$pContent$BtnAddFirm",
            }).abort.assert_called_once_with("blockedbyclient")
            client._new_firm_form_url = FORM
            with self.assertRaises(SroError):
                client.arm_operator_firm_save(FORM)
            client._operator_save_url = FORM
            client._operator_save_target = FIRM_SAVE_TARGET
            client._operator_save_request_pending = True
            _route(client, "/register/firm_card.aspx?id=fresh-opaque", {
                "__EVENTTARGET": FIRM_SAVE_TARGET,
            }).abort.assert_called_once_with("blockedbyclient")

    def test_firm_save_requires_exact_add_bound_identified_form(self):
        client = self.client()
        with self.assertRaises(SroError):
            client.arm_operator_firm_save(FORM)
        client.bind_owned_firm_form(FORM)
        client.arm_operator_firm_save(FORM)
        self.assertEqual(client._operator_save_target, FIRM_SAVE_TARGET)
        other = self.client()
        other.bind_owned_firm_form(FORM)
        other.page.url = BASE + "/register/firm_card.aspx?id=existing"
        with self.assertRaises(SroError):
            other.arm_operator_firm_save(other.page.url)

    def test_owned_preview_close_is_bound_to_same_guid_and_main_frame(self):
        client = self.client(operator_save_enabled=False)
        client.bind_owned_firm_preview(FORM)
        close = {"__EVENTTARGET": "ctl00$pContent$BtnItemFirmCancel"}
        _route(client, "/register/firm_card.aspx?id=fresh-opaque", close).continue_.assert_called_once()
        _route(client, "/register/firm_card.aspx?id=other", close).abort.assert_called_once_with("blockedbyclient")
        _route(client, "/register/firm_card.aspx?id=fresh-opaque", close,
               frame=object()).abort.assert_called_once_with("blockedbyclient")

    def test_nested_address_add_cancel_are_one_shot_and_save_stays_blocked(self):
        client = self.client(operator_save_enabled=False)
        client.bind_owned_firm_preview(FORM)
        add = {"__EVENTTARGET": FIRM_ADDRESS_ADD_TARGET}
        cancel = {"__EVENTTARGET": FIRM_ADDRESS_CANCEL_TARGET}
        path = "/register/firm_card.aspx?id=fresh-opaque"
        _route(client, path, add).abort.assert_called_once_with("blockedbyclient")
        client.arm_owned_firm_address_add()
        _route(client, path, add).continue_.assert_called_once()
        _route(client, path, add).abort.assert_called_once_with("blockedbyclient")
        _route(client, path, {"__EVENTTARGET": "ctl00$pContent$TabContainer1$TabPanel6$ctrl_contact1$BtnItemAddressOk"}).abort.assert_called_once_with("blockedbyclient")
        client.arm_owned_firm_address_cancel()
        _route(client, path, cancel).continue_.assert_called_once()
        _route(client, path, cancel).abort.assert_called_once_with("blockedbyclient")

    def test_operator_nested_address_save_is_exact_one_shot_then_top_save_remains_separate(self):
        client = self.client()
        client.bind_owned_firm_form(FORM)
        path = "/register/firm_card.aspx?id=fresh-opaque"
        add = {"__EVENTTARGET": FIRM_ADDRESS_ADD_TARGET}
        save = {"__EVENTTARGET": FIRM_ADDRESS_SAVE_TARGET}
        _route(client, path, add).abort.assert_called_once_with("blockedbyclient")
        _route(client, path, save).abort.assert_called_once_with("blockedbyclient")
        client.arm_operator_firm_address_add()
        _route(client, path, add).continue_.assert_called_once()
        routes = []

        def click():
            routes.append(_route(client, path, save))
            routes.append(_route(client, path, save))

        client.page.locator.return_value.click.side_effect = click
        client.click_operator_firm_address_save()
        routes[0].continue_.assert_called_once()
        routes[1].abort.assert_called_once_with("blockedbyclient")
        client.page.locator.assert_called_once_with(FIRM_ADDRESS_SAVE_SELECTOR)
        with self.assertRaises(SroError):
            client.click_operator_firm_address_save()
        with self.assertRaises(SroError):
            client.arm_operator_firm_address_cancel()
        client.arm_operator_firm_save(FORM)
        self.assertEqual(client._operator_save_target, FIRM_SAVE_TARGET)

    def test_operator_editor_can_cancel_only_before_nested_save(self):
        client = self.client()
        client.bind_owned_firm_form(FORM)
        path = "/register/firm_card.aspx?id=fresh-opaque"
        client.arm_operator_firm_address_add()
        _route(client, path, {"__EVENTTARGET": FIRM_ADDRESS_ADD_TARGET}).continue_.assert_called_once()
        client.arm_operator_firm_address_cancel()
        _route(client, path, {"__EVENTTARGET": FIRM_ADDRESS_CANCEL_TARGET}).continue_.assert_called_once()
        with self.assertRaises(SroError):
            client.click_operator_firm_address_save()

    def test_one_exact_firm_save_is_consumed_before_network(self):
        client = self.client()
        client.bind_owned_firm_form(FORM)
        client.arm_operator_firm_save(FORM)
        routes = []

        def click():
            payload = {"__EVENTTARGET": FIRM_SAVE_TARGET}
            routes.append(_route(client, "/register/firm_card.aspx?id=fresh-opaque", payload))
            routes.append(_route(client, "/register/firm_card.aspx?id=fresh-opaque", payload))

        client.page.locator.return_value.click.side_effect = click
        client.click_operator_firm_save()
        routes[0].continue_.assert_called_once()
        routes[1].abort.assert_called_once_with("blockedbyclient")
        client.page.locator.assert_called_once_with(FIRM_SAVE_SELECTOR)
        with self.assertRaises(SroError):
            client.click_operator_firm_save()

    def test_armed_firm_save_rejects_nested_and_cross_form_posts(self):
        client = self.client()
        client.bind_owned_firm_form(FORM)
        client.arm_operator_firm_save(FORM)
        client._operator_save_request_pending = True
        for path, payload in (
            ("/register/firm_card.aspx?id=fresh-opaque", {"__EVENTTARGET": "ctl00$pContent$BtnOkItemFirmFinder"}),
            ("/register/firm_card.aspx?id=fresh-opaque", {"__EVENTTARGET": FIRM_SAVE_TARGET, "ctl00$pContent$BtnOkItemFirmFinder": "Save"}),
            ("/register/person_card.aspx?id=fresh-opaque", {"__EVENTTARGET": FIRM_SAVE_TARGET}),
            ("/register/firm_card.aspx?id=other", {"__EVENTTARGET": FIRM_SAVE_TARGET}),
        ):
            with self.subTest(path=path, payload=payload):
                _route(client, path, payload).abort.assert_called_once_with("blockedbyclient")


if __name__ == "__main__":
    unittest.main()
