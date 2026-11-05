"""Synthetic one-shot HTTP boundary for the owned person's nested address."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from sro_robot.platform.client import (
    PERSON_ADDRESS_ADD_TARGET, PERSON_ADDRESS_CANCEL_TARGET,
    PERSON_ADDRESS_SAVE_SELECTOR, PERSON_ADDRESS_SAVE_TARGET, SroClient,
)
from sro_robot.platform.errors import SroError


BASE = "https://synthetic.invalid"
FORM = BASE + "/register/person_card.aspx?id=owned-opaque"
PATH = "/register/person_card.aspx?id=owned-opaque"


def _post(client, path, target, *, frame=None, argument=""):
    route = MagicMock()
    route.request = SimpleNamespace(
        method="POST", url=BASE + path,
        post_data=urlencode({"__EVENTTARGET": target, "__EVENTARGUMENT": argument}),
        frame=client.page.main_frame if frame is None else frame,
        response=lambda: SimpleNamespace(status=302),
    )
    client._guard_prepare_only_request(route)
    return route


class PersonAddressMutationGuardTests(unittest.TestCase):
    def client(self, *, operator_save=False):
        client = SroClient(
            cfg=SimpleNamespace(base_url=BASE + "/default.aspx"),
            prepare_only=True, owned_add_required=True,
            operator_save_enabled=operator_save,
        )
        client.page = MagicMock()
        client.page.url = FORM
        client.settle = MagicMock()
        return client

    def test_unqualified_address_actions_are_blocked(self):
        client = self.client()
        client._new_person_form_url = FORM
        with patch("sro_robot.platform.client.PERSON_ADDRESS_PREVIEW_QUALIFIED", False):
            with self.assertRaises(SroError):
                client.arm_owned_person_address_add()
            _post(client, PATH, PERSON_ADDRESS_ADD_TARGET).abort.assert_called_once_with("blockedbyclient")
            _post(client, PATH, PERSON_ADDRESS_CANCEL_TARGET).abort.assert_called_once_with("blockedbyclient")
            _post(client, PATH, PERSON_ADDRESS_SAVE_TARGET).abort.assert_called_once_with("blockedbyclient")

    def test_preview_add_cancel_exact_guid_main_frame_one_shot(self):
        client = self.client()
        client._new_person_form_url = FORM
        with patch("sro_robot.platform.client.PERSON_ADDRESS_PREVIEW_QUALIFIED", True):
            _post(client, PATH, PERSON_ADDRESS_ADD_TARGET).abort.assert_called_once_with("blockedbyclient")
            client.arm_owned_person_address_add()
            _post(client, PATH, PERSON_ADDRESS_ADD_TARGET, frame=object()).abort.assert_called_once_with("blockedbyclient")
            _post(client, PATH, PERSON_ADDRESS_ADD_TARGET).continue_.assert_called_once()
            _post(client, PATH, PERSON_ADDRESS_ADD_TARGET).abort.assert_called_once_with("blockedbyclient")
            client.arm_owned_person_address_cancel()
            _post(client, "/register/person_card.aspx?id=other", PERSON_ADDRESS_CANCEL_TARGET).abort.assert_called_once_with("blockedbyclient")
            _post(client, PATH, PERSON_ADDRESS_CANCEL_TARGET).continue_.assert_called_once()
            _post(client, PATH, PERSON_ADDRESS_CANCEL_TARGET).abort.assert_called_once_with("blockedbyclient")
            _post(client, PATH, PERSON_ADDRESS_SAVE_TARGET).abort.assert_called_once_with("blockedbyclient")

    def test_operator_nested_save_consumes_exact_post_before_network(self):
        client = self.client(operator_save=True)
        client.bind_owned_person_form(FORM)
        with patch("sro_robot.platform.client.PERSON_ADDRESS_PREVIEW_QUALIFIED", True):
            client.arm_operator_person_address_add()
            _post(client, PATH, PERSON_ADDRESS_ADD_TARGET).continue_.assert_called_once()
            calls = []

            def click():
                calls.append(_post(client, PATH, PERSON_ADDRESS_SAVE_TARGET))
                calls.append(_post(client, PATH, PERSON_ADDRESS_SAVE_TARGET))

            client.page.locator.return_value.click.side_effect = click
            client.click_operator_person_address_save()
            calls[0].continue_.assert_called_once()
            calls[1].abort.assert_called_once_with("blockedbyclient")
            client.page.locator.assert_called_once_with(PERSON_ADDRESS_SAVE_SELECTOR)
            with self.assertRaises(SroError):
                client.click_operator_person_address_save()

    def test_operator_editor_cancel_is_only_before_nested_save(self):
        client = self.client(operator_save=True)
        client.bind_owned_person_form(FORM)
        with patch("sro_robot.platform.client.PERSON_ADDRESS_PREVIEW_QUALIFIED", True):
            client.arm_operator_person_address_add()
            _post(client, PATH, PERSON_ADDRESS_ADD_TARGET).continue_.assert_called_once()
            client.arm_operator_person_address_cancel()
            _post(client, PATH, PERSON_ADDRESS_CANCEL_TARGET, argument="unexpected").abort.assert_called_once_with("blockedbyclient")
            _post(client, PATH, PERSON_ADDRESS_CANCEL_TARGET).continue_.assert_called_once()
            with self.assertRaises(SroError):
                client.click_operator_person_address_save()


if __name__ == "__main__":
    unittest.main()
