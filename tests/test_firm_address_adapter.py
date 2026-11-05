"""Synthetic checks for the owned firm address preview and Cancel boundary."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sro_robot.platform.errors import SroError
from sro_robot.platform.firm_address import (
    address_field_values, preview_address, preview_person_address,
    save_address, save_person_address, verify_saved_address,
    verify_saved_person_address, verify_person_address_blank,
)


class _Element:
    def __init__(self, client, key):
        self.client, self.key = client, key

    def click(self):
        self.client.actions.append(self.key)
        if self.key in {"firm_address_add", "person_address_add"}:
            self.client.editor_open = True
            self.client.values.update({
                "firm_addr_build": "дом", "firm_addr_box": "корпус",
                "firm_addr_room": "офис",
            })
        elif self.key in {"firm_address_cancel", "person_address_cancel"}:
            self.client.editor_open = False
            self.client.values.clear()

    def wait_for(self, *, state, timeout):
        assert state == "visible" and timeout == 1000
        assert self.client.editor_open if self.key in {"firm_address_type", "address_kind_select"} else True

    def locator(self, selector):
        if selector.startswith("xpath="):
            return SimpleNamespace(
                count=lambda: self.client.address_rows,
                first=SimpleNamespace(inner_text=lambda: self.client.address_row_text),
            )
        if selector == "option:checked":
            return SimpleNamespace(inner_text=lambda: self.client.selected_type)
        raise AssertionError(selector)

    def input_value(self):
        return self.client.values.get(self.key, "")

    def scroll_into_view_if_needed(self):
        pass

    def fill(self, value):
        self.client.actions.append("fill:" + self.key)
        self.client.values[self.key] = value


class _Client:
    prepare_only = True
    owned_add_required = True
    operator_save_enabled = False

    def __init__(self):
        url = "https://synthetic.invalid/register/firm_card.aspx?id=owned"
        self.page = SimpleNamespace(url=url)
        self.cfg = SimpleNamespace(base_url="https://synthetic.invalid/default.aspx", timeout_ms=1000)
        self._new_firm_form_url = url
        self._owned_firm_preview_url = None
        self.values = {}
        self.actions = []
        self.address_rows = 0
        self.editor_open = False
        self.selected_type = "Почтовый адрес"
        self.shot_values = None
        self.address_row_text = ""
        self._owned_firm_form_url = None

    def el(self, key):
        return _Element(self, key)

    def settle(self):
        pass

    def fill(self, key, value):
        raise AssertionError("empty strings must be written through the locator")

    def arm_owned_firm_address_add(self):
        self.actions.append("arm_address_add")

    def arm_owned_firm_address_cancel(self):
        self.actions.append("arm_address_cancel")

    def arm_operator_firm_address_add(self):
        self.actions.append("arm_operator_address_add")

    def arm_operator_firm_address_cancel(self):
        self.actions.append("arm_operator_address_cancel")

    def click_operator_firm_address_save(self):
        self.actions.append("nested_save")
        self.editor_open = False
        self.address_rows = 1
        self.address_row_text = "Адрес регистрации, 123456, г. Примерск, ул. Учебная, д. 7"

    arm_owned_person_address_add = arm_owned_firm_address_add
    arm_owned_person_address_cancel = arm_owned_firm_address_cancel
    arm_operator_person_address_add = arm_operator_firm_address_add
    arm_operator_person_address_cancel = arm_operator_firm_address_cancel
    click_operator_person_address_save = click_operator_firm_address_save

    def shot(self, label):
        self.actions.append("shot:" + label)
        self.shot_values = dict(self.values)
        return "synthetic://" + label


class FirmAddressAdapterTests(unittest.TestCase):
    def test_split_address_maps_observed_fields(self):
        self.assertEqual(address_field_values(
            "123456, г. Примерск, ул. Учебная, д. 7, кв. 1"), {
                "firm_addr_index": "123456", "firm_addr_city": "г. Примерск",
                "firm_addr_street": "ул. Учебная", "firm_addr_build": "д. 7",
                "firm_addr_room": "кв. 1",
            })
        self.assertEqual(address_field_values(
            "г. Примерск, ул. Учебная, офис 1, офис 2"), {
                "firm_addr_street": "г. Примерск, ул. Учебная, офис 1, офис 2",
            })
        self.assertEqual(address_field_values(
            "г. Примерск, ул. Учебная, д. 7, корп. 2"), {
                "firm_addr_city": "г. Примерск",
                "firm_addr_street": "ул. Учебная",
                "firm_addr_build": "д. 7",
                "firm_addr_box": "корп. 2",
            })

    def test_preview_shots_then_cancels_and_clears_literal_defaults(self):
        client = _Client()

        def select(_client, key, label):
            self.assertEqual(key, "firm_address_type")
            client.selected_type = label
            return label

        with patch("sro_robot.platform.firm_address.select_exact_option", side_effect=select):
            filled, screenshot = preview_address(
                client, "123456, г. Примерск, ул. Учебная, д. 7", "registration",
            )
        self.assertEqual(screenshot, "synthetic://firm_address_filled")
        self.assertEqual(filled["firm_address_type"], "Адрес регистрации")
        self.assertFalse(client.editor_open)
        self.assertEqual(client.address_rows, 0)
        self.assertIn("fill:firm_addr_box", client.actions)
        self.assertEqual(client.shot_values["firm_addr_build"], "д. 7")
        self.assertEqual(client.shot_values["firm_addr_box"], "")
        self.assertEqual(client.shot_values["firm_addr_room"], "")
        self.assertLess(client.actions.index("shot:firm_address_filled"),
                        client.actions.index("firm_address_cancel"))
        self.assertNotIn("firm_address_save", client.actions)

    def test_unbound_or_untyped_address_never_opens_editor(self):
        client = _Client()
        with self.assertRaises(SroError):
            preview_address(client, "г. Примерск", "")
        self.assertNotIn("firm_address_add", client.actions)
        client.page.url = "https://synthetic.invalid/register/firm_card.aspx?id=other"
        with self.assertRaises(SroError):
            preview_address(client, "г. Примерск", "actual")
        self.assertNotIn("firm_address_add", client.actions)

    def test_selection_error_cancels_open_editor(self):
        client = _Client()
        with patch("sro_robot.platform.firm_address.select_exact_option",
                   side_effect=SroError("missing option")):
            with self.assertRaises(SroError):
                preview_address(client, "г. Примерск", "actual")
        self.assertFalse(client.editor_open)
        self.assertEqual(client.address_rows, 0)
        self.assertIn("firm_address_cancel", client.actions)
        self.assertNotIn("shot:firm_address_filled", client.actions)

    def test_operator_nested_save_receipt_and_fresh_readback(self):
        client = _Client()
        client.operator_save_enabled = True
        client._owned_firm_form_url = client.page.url

        def select(_client, _key, label):
            client.selected_type = label
            return label

        with patch("sro_robot.platform.firm_address.select_exact_option", side_effect=select):
            receipt = save_address(
                client, "123456, г. Примерск, ул. Учебная, д. 7", "registration",
            )
        self.assertEqual(receipt["row_count"], "1")
        self.assertEqual(len(receipt["row_sha256"]), 64)
        self.assertIn("nested_save", client.actions)
        self.assertNotIn("firm_address_cancel", client.actions)
        verify_saved_address(client, receipt)
        client.address_row_text = "changed"
        with self.assertRaises(SroError):
            verify_saved_address(client, receipt)

    def test_operator_fill_error_cancels_before_nested_save(self):
        client = _Client()
        client.operator_save_enabled = True
        client._owned_firm_form_url = client.page.url
        with patch("sro_robot.platform.firm_address.select_exact_option",
                   side_effect=SroError("missing option")):
            with self.assertRaises(SroError):
                save_address(client, "г. Примерск", "actual")
        self.assertIn("firm_address_cancel", client.actions)
        self.assertNotIn("nested_save", client.actions)

    def test_operator_wrong_saved_row_holds_without_cancel(self):
        client = _Client()
        client.operator_save_enabled = True
        client._owned_firm_form_url = client.page.url
        original_save = client.click_operator_firm_address_save

        def wrong_save():
            original_save()
            client.address_row_text = "Адрес регистрации, 123456, г. Примерск, ул. Чужая, д. 7"

        client.click_operator_firm_address_save = wrong_save

        def select(_client, _key, label):
            client.selected_type = label
            return label

        with patch("sro_robot.platform.firm_address.select_exact_option", side_effect=select):
            with self.assertRaises(SroError):
                save_address(client, "123456, г. Примерск, ул. Учебная, д. 7", "registration")
        self.assertIn("nested_save", client.actions)
        self.assertNotIn("firm_address_cancel", client.actions)

    def test_person_profile_uses_owned_person_identity_and_same_editor_checks(self):
        client = _Client()
        client.page.url = "https://synthetic.invalid/register/person_card.aspx?id=owned"
        client._new_person_form_url = client.page.url
        client._owned_person_preview_url = None

        def select(_client, key, label):
            self.assertEqual(key, "address_kind_select")
            client.selected_type = label
            return label

        with patch("sro_robot.platform.firm_address.select_exact_option", side_effect=select):
            filled, screenshot = preview_person_address(
                client, "123456, г. Примерск, ул. Учебная, д. 7", "registration",
            )
        self.assertEqual(screenshot, "synthetic://person_address_filled")
        self.assertEqual(filled["addr_city"], "г. Примерск")
        self.assertIn("person_address_cancel", client.actions)
        self.assertNotIn("firm_address_add", client.actions)

        client.page.url = "https://synthetic.invalid/register/person_card.aspx?id=other"
        with self.assertRaises(SroError) as unbound:
            verify_person_address_blank(client)
        self.assertEqual(unbound.exception.code, "person_address_unbound")
        client.page.url = client._new_person_form_url

        client.operator_save_enabled = True
        client._owned_person_form_url = client.page.url
        verify_person_address_blank(client)
        with patch("sro_robot.platform.firm_address.select_exact_option", side_effect=select):
            receipt = save_person_address(
                client, "123456, г. Примерск, ул. Учебная, д. 7", "registration",
            )
        verify_saved_person_address(client, receipt)
        with self.assertRaises(SroError):
            verify_person_address_blank(client)


if __name__ == "__main__":
    unittest.main()
