"""Read-back checks for prepared fields in the external SRO form."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from sro_robot.core.models import DebtorCard, Field
from sro_robot.platform import SroError
from sro_robot.scenarios.person_card import CARD_STATUS, PersonCardScenario


class _Control:
    def __init__(self, value: str):
        self.value = value

    def input_value(self) -> str:
        return self.value

    def locator(self, selector: str) -> "_Control":
        if selector != "option:checked":
            raise AssertionError("Unexpected selector")
        return self

    def inner_text(self) -> str:
        return self.value


def _card() -> DebtorCard:
    return DebtorCard(
        case_number=Field("А47-123/2026"),
        last_name=Field("Тестова"),
        first_name=Field("Мария"),
        middle_name=Field("Ивановна"),
        inn=Field("1234567890"),
    )


class PersonCardReadbackTests(unittest.TestCase):
    def test_accepts_exact_text_and_selected_option_readback(self):
        values = {
            "last_name": "Тестова",
            "first_name": "Мария",
            "middle_name": "Ивановна",
            "inn": "1234567890",
            "comment": "А47-123/2026",
            "sex_select": "Ж",
            "status_select": CARD_STATUS,
        }
        client = SimpleNamespace(el=lambda key: _Control(values[key]))

        PersonCardScenario._verify_general_fields(client, _card(), "Ж")

    def test_mismatch_fails_without_including_document_values(self):
        values = {
            "last_name": "Тестова",
            "first_name": "Мария",
            "middle_name": "Ивановна",
            "inn": "1234567890",
            "comment": "wrong-case",
            "sex_select": "Ж",
            "status_select": CARD_STATUS,
        }
        client = SimpleNamespace(el=lambda key: _Control(values[key]))

        with self.assertRaisesRegex(SroError, "read-back mismatch for comment") as caught:
            PersonCardScenario._verify_general_fields(client, _card(), "Ж")

        self.assertNotIn("wrong-case", str(caught.exception))
        self.assertNotIn("А47-123/2026", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
