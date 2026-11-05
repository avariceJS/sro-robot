"""Synthetic review-only attributes in a caller-bounded person span."""

from __future__ import annotations

import unittest

from sro_robot.core.person_attributes import detect_person_attributes
from sro_robot.core.validators import valid_inn, valid_snils


def _inn(stem: str = "7723456789") -> str:
    digits = [int(value) for value in stem]
    first = sum(a * b for a, b in zip(digits, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
    second = sum(a * b for a, b in zip(digits + [first], (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
    value = stem + str(first) + str(second)
    assert valid_inn(value)
    return value


def _snils(number: str = "123456789") -> str:
    total = sum(int(digit) * (9 - index) for index, digit in enumerate(number))
    control = total if total < 100 else 0 if total in (100, 101) else total % 101
    if control in (100, 101):
        control = 0
    value = number + f"{control:02d}"
    assert valid_snils(value)
    return f"{number[:3]}-{number[3:6]}-{number[6:]} {control:02d}"


class PersonAttributeTests(unittest.TestCase):
    def test_unlabeled_valid_identifiers_and_address_have_exact_source_spans(self):
        inn, snils = _inn(), _snils()
        text = (
            "Иванов Иван Иванович\n"
            f"{inn}; {snils}; г. Москва, ул. Садовая, д. 7, кв. 1\n"
            "Заявитель: Петров Петр Петрович\n"
            f"{_inn('7700000001')}"
        )
        start = text.index("\n") + 1
        end = text.index("Заявитель")
        found = detect_person_attributes(text, start, end)
        self.assertEqual(found.inn, inn)
        self.assertEqual(found.snils, snils)
        self.assertEqual(found.address, "г. Москва, ул. Садовая, д. 7, кв. 1")
        self.assertIsNone(found.address_kind)
        self.assertFalse(found.address_partial)
        for value, location in ((inn, found.inn_location), (snils, found.snils_location), (found.address, found.address_location)):
            self.assertEqual(text[location[0]:location[1]], value)
        self.assertEqual(found.ambiguous_fields, ())

    def test_conflicting_values_are_withheld_independently(self):
        text = (
            f"{_inn()} {_inn('7700000001')}\n"
            f"{_snils()} {_snils('987654321')}\n"
            "г. Москва, ул. Садовая, д. 7; г. Тверь, ул. Лесная, д. 8"
        )
        found = detect_person_attributes(text, 0, len(text))
        self.assertIsNone(found.inn)
        self.assertIsNone(found.snils)
        self.assertIsNone(found.address)
        self.assertEqual(found.ambiguous_fields, ("inn", "snils", "address"))

    def test_organization_and_document_numbers_do_not_become_person_ids(self):
        text = (
            f"ООО Условное ИНН 7700000001; дело А40-{_inn()}/2026; "
            f"кадастровый номер {_inn()}; 77:{_inn()}; 12.10.2026; "
            "12345678901"
        )
        found = detect_person_attributes(text, 0, len(text))
        self.assertIsNone(found.inn)
        self.assertIsNone(found.snils)
        self.assertIsNone(found.address)

    def test_city_only_is_partial_and_kind_comes_only_from_label(self):
        for label, kind in (("Адрес: ", "actual"), ("Адрес регистрации: ", "registration")):
            with self.subTest(label=label):
                text = f"{label}г. Москва; место рождения: г. Тверь"
                found = detect_person_attributes(text, 0, len(text))
                self.assertEqual(found.address, "г. Москва")
                self.assertEqual(found.address_kind, kind)
                self.assertTrue(found.address_partial)
                self.assertEqual(text[slice(*found.address_location)], "г. Москва")
                self.assertEqual(found.birth_place, "г. Тверь")
        unlabeled = detect_person_attributes("г. Москва", 0, len("г. Москва"))
        self.assertIsNone(unlabeled.address)
        self.assertIsNone(unlabeled.address_kind)

    def test_invalid_context_span_is_rejected(self):
        with self.assertRaises(ValueError):
            detect_person_attributes("abc", 2, 4)

    def test_sentence_punctuation_after_inn_is_allowed(self):
        text = f"Иванов Иван Иванович, ИНН {_inn()}."
        found = detect_person_attributes(text, text.index("ИНН"), len(text))
        self.assertEqual(found.inn, _inn())
        self.assertEqual(text[slice(*found.inn_location)], _inn())


if __name__ == "__main__":
    unittest.main()
