from __future__ import annotations

import unittest
import json
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from sro_robot.core.entity_extract import EntityDebtorCard, detect_entity_kind, parse_entity
from sro_robot.core.extract import parse_file
from sro_robot.core.models import DebtorCard
from sro_robot.core.validators import valid_inn, valid_ogrn, valid_ogrnip


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
    def test_entity_scenario_is_fake_platform_fail_closed(self):
        from unittest.mock import MagicMock
        from sro_robot.scenarios.entity_review import EntityReviewScenario
        from sro_robot.platform import SroError

        client = MagicMock()
        card = parse_entity(["Должник: ООО «Учебная организация»"])
        scenario = EntityReviewScenario()
        self.assertFalse(scenario.preflight(client, card).ok)
        with self.assertRaises(SroError):
            scenario.run(client, card)
        client.assert_not_called()

    def test_independent_synthetic_corpus_matches_field_level_expected_values(self):
        corpus_path = Path(__file__).parent / "fixtures" / "entity_parser_cases.json"
        corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
        field_names = (
            "entity_kind", "legal_name", "debtor_name", "inn", "ogrn", "ogrnip",
            "kpp", "address", "address_kind",
        )
        compared = 0
        mismatches = []
        for item in corpus["cases"]:
            card = parse_entity(item["variants"])
            for name in field_names:
                compared += 1
                actual = getattr(card, name).value
                expected = item["expected"][name]
                if actual != expected:
                    mismatches.append(f"{item['id']}:{name}")
        self.assertEqual(len(corpus["cases"]), 8)
        self.assertEqual(compared, 72)
        self.assertEqual(mismatches, [], f"Synthetic field mismatch categories: {mismatches}")

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
        for field in (card.entity_kind, card.legal_name, card.inn, card.ogrn, card.kpp, card.address):
            self.assertEqual(field.source_variant, 0)
            self.assertLess(field.source_start, field.source_end)
            self.assertTrue(field.source_excerpt)

    def test_address_kind_is_assigned_only_from_an_explicit_label(self):
        registered = parse_entity(["Должник: ООО «Синтетика»\nАдрес регистрации: 101000, Москва, ул. Тестовая, д. 1"])
        postal = parse_entity(["Должник: ООО «Синтетика»\nПочтовый адрес: 101000, Москва, ул. Тестовая, д. 2"])
        untyped = parse_entity(["Должник: ООО «Синтетика»\nАдрес: 101000, Москва, ул. Тестовая, д. 3"])
        multiple = parse_entity(["Должник: ООО «Синтетика»\nАдрес регистрации: 101000, Москва, д. 1\nФактический адрес: 101000, Москва, д. 2"])
        self.assertEqual(registered.address_kind.value, "registration")
        self.assertEqual(postal.address_kind.value, "postal")
        self.assertIsNone(untyped.address_kind.value)
        self.assertIn("явно не указан", untyped.address_kind.warning)
        self.assertIsNone(multiple.address_kind.value)
        self.assertIn("несколько типов адреса", multiple.address_kind.warning)
        self.assertIsNone(multiple.address.value)
        self.assertIn("несколько адресов", multiple.address.warning)

    def test_identifier_conflict_across_ocr_variants_is_not_resolved_by_first_or_longest(self):
        first = "Должник: ООО «Синтетика» ИНН 7700000017 ОГРН 1027700132195"
        second = "Должник: ООО «Синтетика» ИНН 7700000017 ОГРН 1037700132194"
        card = parse_entity([first, second])
        self.assertEqual(card.legal_name.value, "ООО «Синтетика»")
        self.assertIsNone(card.ogrn.value)
        self.assertIn("расходятся", card.ogrn.warning)
        self.assertIsNone(card.ogrn.source_variant)

    def test_grouped_ocr_digits_keep_an_exact_source_range_after_normalization(self):
        text = "Должник: ООО «Пробелы» ИНН 7700 000016"
        card = parse_entity([text])
        self.assertEqual(card.inn.value, "7700000016")
        self.assertEqual(text[card.inn.source_start:card.inn.source_end], "7700 000016")

    def test_deterministic_role_invariants_fail_closed_for_non_debtor_mentions(self):
        # A small generated suite varies other participant roles and noise
        # around a debtor marker. These are synthetic parser invariants only.
        labels = ("Заявитель", "Кредитор", "Представитель должника", "Третье лицо")
        for label in labels:
            text = f"{label}: ООО «Сторонняя организация»\nДолжник: гражданин Иванов Иван Иванович"
            with self.subTest(label=label):
                self.assertEqual(detect_entity_kind(text), "person")

    def test_company_short_name_is_never_guessed_from_full_name(self):
        card = parse_entity(["Должник: ООО «Синтетическая компания» ИНН 7700000017"])
        self.assertEqual(card.legal_name.value, "ООО «Синтетическая компания»")
        self.assertIsNone(card.short_name.value)

    def test_ogrn_control_digits_are_checked_and_failures_remain_visible(self):
        self.assertTrue(valid_ogrn("1027700132195"))
        self.assertTrue(valid_ogrn("5027700132191"))
        self.assertFalse(valid_ogrn("1027700132194"))
        self.assertFalse(valid_ogrn("2027700132194"))
        self.assertTrue(valid_ogrnip("319500100000001"))
        self.assertFalse(valid_ogrnip("319500100000002"))
        company = parse_entity(["Должник: ООО «Синтетика» ОГРН 1027700132194"])
        ip = parse_entity(["Должник: ИП Иванов Иван Иванович ОГРНИП 319500100000002"])
        self.assertIsNone(company.ogrn.value)
        self.assertIn("контроль", company.ogrn.warning)
        self.assertIsNone(ip.ogrnip.value)
        self.assertIn("контроль", ip.ogrnip.warning)
        wrong_type = parse_entity(["Должник: ООО «Неверный ОГРН» ОГРН 2027700132194"])
        self.assertIsNone(wrong_type.ogrn.value)
        self.assertIn("структуры", wrong_type.ogrn.warning)

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

    def test_all_zero_tax_and_registry_numbers_remain_warnings(self):
        card = parse_entity([
            "Должник: ООО «Синтетическая организация» "
            "ИНН 0000000000 ОГРН 0000000000000"
        ])
        self.assertIsNone(card.inn.value)
        self.assertIsNone(card.ogrn.value)
        self.assertIn("контрольной суммы", card.inn.warning)
        self.assertIn("контрольной цифры", card.ogrn.warning)

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
