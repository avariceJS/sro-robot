"""Deterministic checks for core normalization and field extraction."""

from __future__ import annotations

import unittest

from sro_robot.core.address import split_address
from sro_robot.core.extract import find_address, find_birth_date, find_doc_date, find_fio
from sro_robot.core.validators import (
    normalize_case_number,
    valid_inn,
    valid_snils,
)


class CoreNormalizationTests(unittest.TestCase):
    def test_case_number_normalization_is_idempotent(self) -> None:
        normalized = normalize_case_number(" № a41 - 61776 / 2026 ")

        self.assertEqual(normalized, "А41-61776/2026")
        self.assertEqual(normalize_case_number(normalized), normalized)

    def test_inn_checksum_accepts_fictional_valid_value_and_rejects_changed_check_digit(self) -> None:
        self.assertTrue(valid_inn("772881234508"))
        self.assertFalse(valid_inn("772881234509"))

    def test_snils_rejects_invalid_length_and_zero_number(self) -> None:
        self.assertFalse(valid_snils("1234567890"))
        self.assertFalse(valid_snils("00000000000"))


class ExtractionTests(unittest.TestCase):
    def test_document_date_comes_from_header(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года\n"
            "УСТАНОВИЛ: Заседание назначено на 22 сентября 2026 года"
        )

        self.assertEqual(find_doc_date(text), "14.08.2026")

    def test_fio_rule_recognizes_the_debtor_phrase(self) -> None:
        text = "Суд рассмотрев заявление Бурцева Егора Анатольевича установил следующее"

        self.assertEqual(find_fio(text), "Бурцева Егора Анатольевича")

    def test_fio_rule_handles_previous_surname_in_parentheses(self) -> None:
        text = "Должник Сидоровой (Петровой) Анны Сергеевны дата рождения: 01.02.1980"

        self.assertEqual(find_fio(text), "Сидоровой Анны Сергеевны")

    def test_birth_date_rule_handles_explicit_label_and_comma_separator(self) -> None:
        self.assertEqual(find_birth_date("дата рождения: 01,02,1980"), "01.02.1980")

    def test_registration_address_ends_before_birth_place(self) -> None:
        text = (
            "адрес регистрации: 123456, Тестовая область, г. Примерск, ул. Макетная, д. 7, "
            "место рождения: г. Синтетический"
        )

        self.assertEqual(
            find_address(text),
            ("123456, Тестовая область, г. Примерск, ул. Макетная, д. 7", "registration"),
        )

    def test_address_parts_keep_index_city_street_building_and_room(self) -> None:
        address = "123456, Северная область, г. Тестоград, ул. Примерная, д. 8, кв. 42"

        self.assertEqual(
            split_address(address),
            {
                "index": "123456",
                "area": "Северная область",
                "city": "г. Тестоград",
                "street": "ул. Примерная",
                "build": "д. 8",
                "room": "кв. 42",
            },
        )


if __name__ == "__main__":
    unittest.main()
