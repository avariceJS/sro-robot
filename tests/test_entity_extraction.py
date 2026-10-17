from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from sro_robot.core.entity_extract import EntityDebtorCard, detect_entity_kind, parse_entity
from sro_robot.core.extract import parse_file
from sro_robot.core.models import DebtorCard
from sro_robot.core.validators import valid_inn


def make_inn(prefix: str, length: int) -> str:
    """Create a checksum-valid synthetic identifier for parser tests."""
    weights10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    weights11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    weights12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    stem = prefix.ljust(length - (2 if length == 12 else 1), "1")[:length - (2 if length == 12 else 1)]
    digits = [int(digit) for digit in stem]
    first = sum(a * b for a, b in zip(digits, weights10)) % 11 % 10
    if length == 10:
        value = stem + str(first)
    else:
        second = sum(a * b for a, b in zip(digits + [first], weights12)) % 11 % 10
        # Recompute the first control digit with the 12-digit INN weights.
        first12 = sum(a * b for a, b in zip(digits, weights11)) % 11 % 10
        second = sum(a * b for a, b in zip(digits + [first12], weights12)) % 11 % 10
        value = stem + str(first12) + str(second)
    assert valid_inn(value)
    return value


class EntityExtractionTests(unittest.TestCase):
    def test_detects_organization_and_extracts_debtor_requisites(self):
        inn = make_inn("770000001", 10)
        text = (
            "ОПРЕДЕЛЕНИЕ\n10 октября 2026 года\nДело А40-12345/2026\n"
            f"Должник: Общество с ограниченной ответственностью «Синтетика»\n"
            f"Сокращённое наименование: «Синтетика» ИНН {inn} "
            "ОГРН 1027700132195 КПП 770001001\n"
            "Адрес: 101000, Россия,\nг. Москва, ул. Садовая, д. 10\n"
            "Заявитель: Индивидуальный предприниматель Петров Пётр Петрович"
        )
        card = parse_entity([text, text])
        self.assertEqual(detect_entity_kind(text), "legal_entity")
        self.assertEqual(card.entity_kind.value, "legal_entity")
        self.assertEqual(card.legal_name.value, "Общество с ограниченной ответственностью «Синтетика»")
        self.assertEqual(card.legal_form.value, "Общество с ограниченной ответственностью")
        self.assertEqual(card.short_name.value, "«Синтетика»")
        self.assertEqual(card.inn.value, inn)
        self.assertEqual(card.ogrn.value, "1027700132195")
        self.assertEqual(card.kpp.value, "770001001")
        self.assertEqual(card.case_number.value, "А40-12345/2026")
        self.assertEqual(card.address.value, "101000, Россия, г. Москва, ул. Садовая, д. 10")

    def test_company_short_name_is_never_guessed_from_full_name(self):
        card = parse_entity(["Должник: ООО «Синтетическая компания» ИНН 7700000017"])
        self.assertEqual(card.legal_name.value, "ООО «Синтетическая компания»")
        self.assertIsNone(card.short_name.value)

    def test_detects_sole_proprietor_without_running_person_normalizer(self):
        inn = make_inn("500100001", 12)
        text = (
            "Дело А41-3333/2026\nДолжник: ИП Иванов Иван Иванович ИНН "
            f"{inn} ОГРНИП 319500100000001\n"
            "Адрес регистрации: 140000, Московская область, г. Люберцы, ул. Тестовая, д. 4"
        )
        card = parse_entity([text, text])
        self.assertEqual(detect_entity_kind(text), "sole_proprietor")
        self.assertEqual(card.entity_kind.value, "sole_proprietor")
        self.assertEqual(card.debtor_name.value, "Иванов Иван Иванович")
        self.assertEqual(card.inn.value, inn)
        self.assertEqual(card.ogrnip.value, "319500100000001")
        self.assertIsNone(card.legal_name.value)

    def test_business_entity_mentioned_outside_debtor_role_is_not_selected(self):
        text = (
            "Заявитель: ООО «Кредитор»\nДолжник: гражданин Иванов Иван Иванович\n"
            "Заявитель действует в интересах ООО «Кредитор»"
        )
        self.assertEqual(detect_entity_kind(text), "person")

    def test_business_entity_as_debtor_representative_is_not_misclassified(self):
        text = "Представитель должника: ООО «Представитель»\nЗаявитель: ИП Иванов Иван Иванович"
        self.assertIsNone(detect_entity_kind(text))

    def test_conflicting_ocr_variants_are_held_for_manual_type_review(self):
        company = "Должник: ООО «Синтетика» ИНН 7700000017"
        entrepreneur = "Должник: ИП Иванов Иван Иванович ИНН 500100001234"
        card = parse_entity([company, entrepreneur])
        self.assertIsNone(card.entity_kind.value)
        self.assertIn("не установлены однозначно", card.entity_kind.warning)
        self.assertIsNone(card.inn.value)

    def test_checksum_failure_is_visible_as_warning_not_accepted(self):
        card = parse_entity(["Должник: ООО «Синтетика» ИНН 7700000018"])
        self.assertIsNone(card.inn.value)
        self.assertIn("контрольной суммы", card.inn.warning)

    def test_local_file_parser_routes_mixed_intake_to_typed_card(self):
        company = "Должник: ООО «Синтетика» ИНН 7700000017"
        person = "Должник: гражданин Иванов Иван Иванович ИНН 500100001234"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.pdf"
            path.write_bytes(b"synthetic test placeholder")
            with patch("sro_robot.core.ocr.recognize", return_value=[company]):
                entity_card = parse_file(path)
            with patch("sro_robot.core.ocr.recognize", return_value=[person]):
                person_card = parse_file(path)
        self.assertIsInstance(entity_card, EntityDebtorCard)
        self.assertIsInstance(person_card, DebtorCard)


if __name__ == "__main__":
    unittest.main()
