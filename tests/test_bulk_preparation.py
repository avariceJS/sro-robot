"""Candidate admission must require role evidence, never confidence alone."""

import unittest
from types import SimpleNamespace

from app.services.assignments import PREPARATION_KEY, automatic_preparation_reason, prepare_debtor_card_from_ocr
from sro_robot.core.extract import parse
from sro_robot.core.models import Field


def candidate():
    return parse(["ОПРЕДЕЛЕНИЕ 14 октября 2099 года\nДело № А99-12345/2099\n"
                  "Должник: Макетова Анна Петровна, ИНН 772881234508\n"
                  "Адрес регистрации: 101000, г. Примерск, ул. Тестовая, д. 1"])


class BulkPreparationTests(unittest.TestCase):
    def test_single_role_bound_person_is_eligible(self):
        self.assertIsNone(automatic_preparation_reason(candidate()))

    def test_high_confidence_without_field_role_is_insufficient(self):
        for name in ("last_name", "first_name", "middle_name", "inn", "address", "address_kind"):
            with self.subTest(name=name):
                card = candidate()
                getattr(card, name).source = "ocr"
                getattr(card, name).confidence = 1.0
                self.assertEqual(automatic_preparation_reason(card), "field_role_uncertain")

    def test_conflicting_address_type_requires_review_even_when_address_agrees(self):
        first = ("ОПРЕДЕЛЕНИЕ 14 октября 2099 года\nДело № А99-12345/2099\n"
                 "Должник: Макетова Анна Петровна, ИНН 772881234508\n"
                 "Адрес регистрации: 101000, г. Примерск, ул. Тестовая, д. 1")
        second = first.replace("Адрес регистрации:", "Адрес:")
        card = parse([first, second])
        self.assertEqual(card.address.value, candidate().address.value)
        self.assertIsNotNone(card.address_kind.warning)
        self.assertIsNotNone(automatic_preparation_reason(card))

    def test_zero_person_inn_is_not_an_identity(self):
        card = candidate()
        card.inn = Field("000000000000", 1.0, "debtor_role:v1")
        self.assertEqual(automatic_preparation_reason(card), "person_inn_uncertain")

    def test_damaged_ocr_inn_candidate_stays_manual_review_only(self):
        text = "ОПРЕДЕЛЕНИЕ 14 октября 2099 года\nДело № А99-12345/2099\n"
        debtor = "Должник: Макетова Анна Петровна, ИНН 772881234508\n"
        address = "Адрес регистрации: 101000, г. Примерск, ул. Тестовая, д. 1"
        card = parse([text + debtor + address] * 3 + [
            text + debtor.replace("772881234508", "7728812345089") + address
        ])

        self.assertEqual(card.inn.value, "772881234508")
        self.assertEqual(card.inn.source, "role_partial")
        self.assertIsNotNone(card.inn.warning)
        self.assertIsNotNone(automatic_preparation_reason(card))

    def test_ambiguous_debtor_role_persists_the_manual_review_reason(self):
        card = parse(["Заявитель: Макетова Анна Петровна; вопрос о должнике не разрешён"])
        task = SimpleNamespace(scenario="person_card", recognized={}, assignee_id=12)

        self.assertIsNone(prepare_debtor_card_from_ocr(None, task, card))
        self.assertEqual(task.recognized[PREPARATION_KEY]["reason"], "debtor_role_uncertain")


if __name__ == "__main__":
    unittest.main()
