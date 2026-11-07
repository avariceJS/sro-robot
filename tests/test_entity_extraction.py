from __future__ import annotations

import unittest
import json
from hypothesis import given, settings, strategies as st
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
    def test_reverse_debtor_keeps_outer_nested_joint_stock_name(self):
        name = ('закрытого акционерного общества «Учебное предприятие '
                'открытого акционерного общества «Синтетический трест»»')
        inn = make_inn("7744", 10)
        for dash in ("-", "–", "—"):
            with self.subTest(dash=dash):
                text = (f'По делу о банкротстве {name} (далее {dash} должник, '
                        f'ЗАО «Учебный трест», ОГРН 1027700132195, ИНН {inn}). '
                        'Кредитор: ООО «Другой участник» ИНН 7700000017')
                card = parse_entity([text])
                self.assertEqual(detect_entity_kind(text), "legal_entity")
                self.assertEqual(card.legal_name.value, name)
                self.assertEqual(card.legal_form.value, "закрытого акционерного общества")
                self.assertEqual(card.inn.value, inn)
                self.assertEqual(card.ogrn.value, "1027700132195")
                self.assertEqual(text[card.legal_name.source_start:card.legal_name.source_end], name)

    def test_long_wrapped_reverse_debtor_name_preserves_source(self):
        name = 'открытое акционерное общество «' + 'Синтетическая организация ' * 9 + '»'
        wrapped = name.replace('организация ', 'организация\n')
        inn = make_inn('7755', 10)
        text = wrapped + ' (далее – должник) ИНН ' + inn
        card = parse_entity([text])
        self.assertEqual(card.legal_name.value, ' '.join(name.split()))
        self.assertEqual(card.inn.value, inn)
        self.assertTrue(card.legal_name.source_end > card.legal_name.source_start)

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

    def test_full_unitary_enterprise_names_use_the_supported_entity_path(self):
        cases = (
            (
                "МУНИЦИПАЛЬНОЕ УНИТАРНОЕ ПРЕДПРИЯТИЕ «СИНТЕТИЧЕСКАЯ СЕТЬ»",
                "МУНИЦИПАЛЬНОЕ УНИТАРНОЕ ПРЕДПРИЯТИЕ",
            ),
            (
                "Федеральное государственное унитарное предприятие «Учебная организация»",
                "Федеральное государственное унитарное предприятие",
            ),
        )
        for legal_name, legal_form in cases:
            text = (
                f"Должник: {legal_name} ИНН 7700000016 "
                "ОГРН 1027700132195 КПП 770001001"
            )
            with self.subTest(legal_form=legal_form):
                card = parse_entity([text])
                self.assertEqual(card.entity_kind.value, "legal_entity")
                self.assertEqual(card.legal_name.value, legal_name)
                self.assertEqual(card.legal_form.value, legal_form)
                self.assertEqual(card.inn.value, "7700000016")
                self.assertEqual(card.ogrn.value, "1027700132195")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.pdf"
            path.write_bytes(b"synthetic test placeholder")
            with patch(
                "sro_robot.core.ocr.recognize",
                return_value=["Должник: МУНИЦИПАЛЬНОЕ УНИТАРНОЕ ПРЕДПРИЯТИЕ «СЕТЬ»"],
            ):
                routed = parse_file(path)
        self.assertIsInstance(routed, EntityDebtorCard)

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

    def test_damaged_identifier_variant_keeps_valid_suggestion_flagged_in_either_order(self):
        valid = (
            "Должник: ООО «Синтетика» ИНН 7700000016 "
            "ОГРН 1027700132195"
        )
        damaged = (
            "Должник: ООО «Синтетика» ИНН 7700000018 "
            "ОГРН 1027700132194"
        )
        for variants in ((valid, damaged), (damaged, valid)):
            with self.subTest(first=variants[0] == valid):
                card = parse_entity(list(variants))
                self.assertEqual(card.inn.value, "7700000016")
                self.assertIsNotNone(card.inn.warning)
                self.assertEqual(card.ogrn.value, "1027700132195")
                self.assertIsNotNone(card.ogrn.warning)

    def test_damaged_ogrnip_variant_keeps_valid_suggestion_flagged_in_either_order(self):
        valid = (
            "Должник: ИП Иванов Иван Иванович ИНН 500100001146 "
            "ОГРНИП 319500100000001"
        )
        damaged = (
            "Должник: ИП Иванов Иван Иванович ИНН 500100001146 "
            "ОГРНИП 319500100000002"
        )
        for variants in ((valid, damaged), (damaged, valid)):
            with self.subTest(first=variants[0] == valid):
                card = parse_entity(list(variants))
                self.assertEqual(card.ogrnip.value, "319500100000001")
                self.assertIsNotNone(card.ogrnip.warning)

    @given(prefix=st.integers(min_value=100_000_000, max_value=999_999_999))
    @settings(max_examples=60, deadline=None)
    def test_company_inn_warning_merge_is_order_independent(self, prefix):
        valid = make_inn(str(prefix), 10)
        damaged = valid[:-1] + str((int(valid[-1]) + 1) % 10)
        head = "Должник: ООО «Учебная организация» ИНН "
        forward = parse_entity([head + valid, head + damaged]).inn
        reverse = parse_entity([head + damaged, head + valid]).inn

        self.assertEqual(forward.value, valid)
        self.assertTrue(forward.warning)
        self.assertEqual((forward.value, forward.warning), (reverse.value, reverse.warning))

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

    def test_overlong_legal_name_is_truncated_with_warning_and_full_identity_check(self):
        shared = "ООО " + "А" * 510
        first = "Должник: " + shared + "Б"
        second = "Должник: " + shared + "В"

        card = parse_entity([first])
        self.assertEqual(len(card.legal_name.value), 512)
        self.assertIn("обрезано", card.legal_name.warning)
        self.assertTrue(card.legal_name.source_excerpt)

        conflict = parse_entity([first, second])
        self.assertIsNone(conflict.legal_name.value)
        self.assertIn("не установлены однозначно", conflict.legal_name.warning)

    @given(suffix=st.text(
        alphabet="БВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЭЮЯ",
        min_size=1,
        max_size=20,
    ))
    @settings(max_examples=80, derandomize=True, deadline=None)
    def test_long_name_suffix_conflicts_are_never_hidden_by_display_truncation(self, suffix):
        shared_prefix = "ООО " + "А" * 510
        first = "Должник: " + shared_prefix + suffix
        second = "Должник: " + shared_prefix + suffix + "Б"

        card = parse_entity([first, second])

        self.assertIsNone(card.legal_name.value)
        self.assertIn("не установлены однозначно", card.legal_name.warning)

    def test_long_entity_address_and_short_name_are_not_silently_truncated(self):
        address = "ул. " + "А" * 520
        short_name = "«" + "Б" * 520 + "»"
        short_card = parse_entity([
            "Должник: ООО «Учебная организация»\n"
            f"Сокращённое наименование: {short_name}"
        ])
        address_card = parse_entity([
            "Должник: ООО «Учебная организация»\n"
            f"Адрес: {address}"
        ])

        self.assertEqual(short_card.short_name.value, short_name)
        self.assertEqual(address_card.address.value, address)
        self.assertIsNone(address_card.address.warning)

    def test_debtor_section_boundary_warns_that_later_fields_need_review(self):
        text = (
            "Должник: ООО «Учебная организация»\n"
            + "Описание обстоятельств. " * 40
            + "ИНН 7700000016 ОГРН 1027700132195"
        )

        card = parse_entity([text])

        self.assertEqual(card.legal_name.value, "ООО «Учебная организация»")
        self.assertIn("длиннее 800 символов", card.legal_name.warning)
        self.assertIn("длиннее 800 символов", card.inn.warning)

    def test_company_representative_is_not_appended_to_legal_name(self):
        card = parse_entity([
            "Должник: ООО «Учебная организация» в лице директора Иванова Ивана Ивановича\n"
            "ИНН 7700000016 ОГРН 1027700132195"
        ])
        self.assertEqual(card.legal_name.value, "ООО «Учебная организация»")
        self.assertEqual(card.inn.value, "7700000016")
        self.assertEqual(card.ogrn.value, "1027700132195")

    def test_unreadable_tax_number_label_does_not_extend_the_legal_name(self):
        card = parse_entity([
            "Должник: ООО «Учебная организация» ИНН не распознан "
            "ОГРН 1027700132195"
        ])

        self.assertEqual(card.legal_name.value, "ООО «Учебная организация»")
        self.assertIsNone(card.inn.value)
        self.assertIsNotNone(card.inn.warning)
        self.assertEqual(card.ogrn.value, "1027700132195")

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




class CivilEntityParticipantTests(unittest.TestCase):
    def parties(self, text):
        from sro_robot.core.extract import parse_variants_for_review
        card = parse_variants_for_review([text])
        return card, [item for item in card.review_suggestions if item['kind'] == 'entity_participant']

    def test_inflected_claim_preserves_two_own_ogrns_and_unsupported_defendant_form(self):
        text = ('По иску общества с ограниченной ответственностью «Учебный истец» '
                '(ОГРН 1027700132195) к Учебному бюджетному учреждению «Ответчик» '
                '(ОГРН 1027700132195) о взыскании задолженности.\f')
        card, items = self.parties(text)
        self.assertIsInstance(card, EntityDebtorCard)
        self.assertEqual(card.entity_kind.value, 'legal_entity')
        self.assertIsNone(card.legal_name.value)
        self.assertIsNone(card.ogrn.value)
        self.assertEqual([x['role'] for x in items], ['plaintiff', 'defendant'])
        self.assertEqual(items[0]['fields']['legal_name']['value'], 'общества с ограниченной ответственностью «Учебный истец»')
        self.assertEqual(items[1]['fields']['legal_name']['value'], 'Учебному бюджетному учреждению «Ответчик»')
        for item in items:
            self.assertTrue(item['review_only'])
            self.assertIn('ogrn', item['copyable_fields'])
            for field in item['fields'].values():
                actual = text[field['source_start']:field['source_end']]
                if field['value'] != 'legal_entity':
                    self.assertEqual(actual, field['value'])
            self.assertEqual(item['fields']['ogrn']['value'], '1027700132195')
            self.assertEqual(item['fields']['ogrn']['source_page'], 1)

    def test_claim_does_not_mix_different_organization_identifiers(self):
        stem = '103770013219'
        second_ogrn = stem + str(int(stem) % 11 % 10)
        self.assertTrue(valid_ogrn(second_ogrn))
        inn_a, inn_b = make_inn('7712', 10), make_inn('7723', 10)
        text = (f'По иску ООО «Истец» (ОГРН 1027700132195, ИНН {inn_a}) '
                f'к АНО «Ответчик» (ОГРН {second_ogrn}, ИНН {inn_b}) о взыскании долга.')
        _, items = self.parties(text)
        self.assertEqual(len(items), 2)
        self.assertEqual([(x['fields']['ogrn']['value'], x['fields']['inn']['value']) for x in items],
                         [('1027700132195', inn_a), (second_ogrn, inn_b)])

    def test_explicit_reverse_roles_preserve_whole_participant_bundle(self):
        for role, expected in (('истец', 'plaintiff'), ('ответчик', 'defendant'), ('заявитель', 'applicant')):
            with self.subTest(role=role):
                text = f'ООО «Учебный участник» (далее — {role}, ОГРН 1027700132195).'
                card, items = self.parties(text)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]['role'], expected)
                self.assertEqual(items[0]['fields']['legal_name']['value'], 'ООО «Учебный участник»')
                self.assertEqual(items[0]['fields']['ogrn']['value'], '1027700132195')
                self.assertIsNone(card.legal_name.value)

    def test_generic_direct_role_supports_form_without_a_wordlist(self):
        text = ('Ответчик: Учебное казённое учреждение «Пример» '
                '(ОГРН 1027700132195).')
        _, items = self.parties(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['role'], 'defendant')
        self.assertEqual(items[0]['fields']['legal_name']['value'], 'Учебное казённое учреждение «Пример»')
        self.assertNotIn('legal_form', items[0]['fields'])
        self.assertEqual(items[0]['fields']['entity_kind']['source_start'], items[0]['fields']['ogrn']['source_start'])

    def test_role_alone_never_supplies_unknown_organization_type(self):
        from sro_robot.core.entity_extract import has_entity_participant
        for text in ('Ответчик: Учебное учреждение.',
                     'Ответчик: Учебное учреждение (ОГРН 1027700132190).',
                     'Учебное учреждение ОГРН 1027700132195.',
                     'Представитель истца ООО «Учебный кредитор» ОГРН 1027700132195',
                     'Конкурсный управляющий ООО «Участник» ОГРН 1027700132195',
                     'Кредитор: ООО «Участник» ОГРН 1027700132195'):
            with self.subTest(text=text):
                self.assertFalse(has_entity_participant(text))

    def test_reverse_role_cannot_attach_to_distant_earlier_organization(self):
        from sro_robot.core.entity_extract import has_entity_participant
        text = ('ООО «Учебный участник» направило документы в суд. '
                'Суд установил иные обстоятельства (ответчик не явился).')
        self.assertFalse(has_entity_participant(text))

    def test_exact_person_debtor_remains_person_when_civil_organization_is_present(self):
        from sro_robot.core.extract import parse_variants_for_review
        text = ('Должник: Иванов Иван Иванович\n'
                'Истец: ООО «Учебный истец» ОГРН 1027700132195')
        card = parse_variants_for_review([text])
        self.assertIsInstance(card, DebtorCard)
        self.assertEqual(card.fio_raw.value, 'Иванов Иван Иванович')

    def test_partial_person_debtor_is_not_replaced_by_corporate_civil_party(self):
        from sro_robot.core.extract import parse, parse_variants_for_review
        corporate = '\nИстец: ООО «Учебная организация» ОГРН 1027700132195'
        variants = ['Должник: Иванова Анна Сергеевна' + corporate,
                    'Заявитель: Иванова Анна Сергеевна' + corporate]
        before, card = parse(variants), parse_variants_for_review(variants)
        self.assertEqual(before.fio_raw.source, 'role_partial')
        self.assertIsInstance(card, DebtorCard)
        self.assertEqual(card.fio_raw.value, before.fio_raw.value)
        self.assertEqual(card.fio_raw.source, 'role_partial')
        self.assertIsNotNone(card.fio_raw.warning)

    def test_new_bankruptcy_anchor_cannot_replace_established_person_debtor(self):
        from sro_robot.core.extract import parse_variants_for_review
        text = ('Должник: Иванов Иван Иванович\n' + 'Синтетическое изложение. ' * 60 +
                'Справочная практика: о признании ООО «Синтетическая ссылка» банкротом.')
        card = parse_variants_for_review([text])
        self.assertIsInstance(card, DebtorCard)
        self.assertEqual(card.fio_raw.value, 'Иванов Иван Иванович')
        self.assertEqual(card.fio_raw.source, 'debtor_role:v1')

    def test_company_plaintiff_or_applicant_does_not_hide_person_address_candidates(self):
        from sro_robot.core.extract import parse, parse_variants_for_review
        details = ('Анкетные сведения: Иванова Анна Сергеевна; личный номер 772881234508; '
                   '112-233-445 95; г. Макетный, ул. Учебная, д. 7, кв. 1.\n')
        for role in ('Истец', 'Заявитель'):
            with self.subTest(role=role):
                text = details + f'{role}: ООО «Учебный кредитор» ОГРН 1027700132195'
                original = [item for item in parse([text]).review_suggestions if item['kind'] == 'application_person']
                card = parse_variants_for_review([text])
                people = [item for item in card.review_suggestions if item['kind'] == 'application_person']
                parties = [item for item in card.review_suggestions if item['kind'] == 'entity_participant']
                self.assertIsInstance(card, DebtorCard)
                self.assertTrue(original)
                self.assertEqual(people, original)
                self.assertEqual(len(parties), 1)
                address = people[0]['fields']['address']
                self.assertEqual(address['value'], 'г. Макетный, ул. Учебная, д. 7, кв. 1')
                self.assertEqual(text[address['source_start']:address['source_end']], address['value'])
                self.assertIsNone(card.fio_raw.value)
                self.assertIsNone(card.address.value)

    def test_entity_defendant_route_retains_all_person_occurrences_without_field_merge(self):
        from sro_robot.core.extract import parse, parse_variants_for_review
        text = ('Анкетные сведения: Иванова Анна Сергеевна; личный номер 772881234508; '
                'г. Макетный, ул. Учебная, д. 7, кв. 1.\n'
                'Ответчик: АНО «Учебное учреждение» (ОГРН 1027700132195)')
        original = [item for item in parse([text]).review_suggestions if item['kind'] == 'application_person']
        card = parse_variants_for_review([text])
        self.assertIsInstance(card, EntityDebtorCard)
        self.assertTrue(original)
        self.assertEqual([item for item in card.review_suggestions if item['kind'] == 'application_person'], original)
        self.assertEqual(card.review_suggestions[:len(original)], original)
        self.assertTrue(any(item['kind'] == 'entity_participant' and item['role'] == 'defendant' for item in card.review_suggestions))
        self.assertIsNone(card.legal_name.value)
        self.assertIsNone(card.inn.value)
        self.assertIsNone(card.address.value)
        entity = next(item for item in card.review_suggestions if item['kind'] == 'entity_participant')
        self.assertNotIn('address', entity['fields'])
        self.assertNotIn('inn', entity['fields'])

    def test_strict_entity_debtor_route_preserves_person_review_candidates(self):
        from sro_robot.core.extract import parse, parse_variants_for_review
        text = ('Анкетные сведения: Иванова Анна Сергеевна; личный номер 772881234508; '
                'г. Макетный, ул. Учебная, д. 7, кв. 1.\n'
                'Должник: ООО «Учебная организация» ОГРН 1027700132195')
        original = [item for item in parse([text]).review_suggestions if item['kind'] == 'application_person']
        card = parse_variants_for_review([text])
        self.assertIsInstance(card, EntityDebtorCard)
        self.assertTrue(original)
        self.assertEqual([item for item in card.review_suggestions if item['kind'] == 'application_person'], original)
        self.assertEqual(card.legal_name.value, 'ООО «Учебная организация»')
        self.assertEqual(card.legal_name.source, 'debtor_entity_role:v1')
        self.assertIsNone(card.address.value)

    def test_role_bound_fields_include_address_and_sources_on_second_page(self):
        text = ('Синтетическая первая страница\fОтветчик: АНО «Пример» '
                'ОГРН 1027700132195, Адрес: 101000, г. Москва, ул. Учебная, д. 5')
        _, items = self.parties(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['fields']['address']['value'], '101000, г. Москва, ул. Учебная, д. 5')
        self.assertEqual(items[0]['fields']['address']['source_page'], 2)
        self.assertEqual(items[0]['role_location']['source_page'], 2)

    def test_generic_ogrn_cannot_bind_to_a_narrative_role_mention(self):
        from sro_robot.core.entity_extract import has_entity_participant
        for text in ('Ответчик не явился. В деле упоминается Учебное учреждение (ОГРН 1027700132195).',
                     'Ответчик ссылается на Учебное учреждение (ОГРН 1027700132195).',
                     'Ответчик: не явился. В деле упоминается Учебное учреждение (ОГРН 1027700132195).'):
            self.assertFalse(has_entity_participant(text))

    def test_generic_section_with_multiple_identifiers_stays_unresolved(self):
        from sro_robot.core.entity_extract import has_entity_participant
        stem = '103770013219'; other = stem + str(int(stem) % 11 % 10)
        self.assertFalse(has_entity_participant(f'Ответчик: Учреждение (ОГРН 1027700132195, ОГРН {other})'))

    def test_quoted_claim_words_cannot_invent_participants(self):
        from sro_robot.core.entity_extract import has_entity_participant
        for text in ('ООО «По иску ООО Истец к ООО Ответчик»',
                     'ООО «Учебный бренд по иску ООО Пример к Учебному учреждению (ОГРН 1027700132195)»'):
            self.assertFalse(has_entity_participant(text))

    def test_narrative_recipient_after_claim_purpose_is_not_a_defendant(self):
        for purpose in ('о взыскании долга', 'об обязании передать вещи', 'об истребовании имущества',
                        'о защите исключительных прав', ''):
            with self.subTest(purpose=purpose):
                text = (f'По иску ООО «Учебная сторона» {purpose}. '
                        'Суд указал на письмо к ООО «Иное лицо» (ОГРН 1027700132195).')
                _, items = self.parties(text)
                self.assertEqual([item['role'] for item in items], ['plaintiff'])
                self.assertNotIn('ogrn', items[0]['fields'])
                self.assertEqual(items[0]['fields']['legal_name']['value'], 'ООО «Учебная сторона»')

    def test_claim_header_retains_short_abbreviations_and_quoted_name_punctuation(self):
        text = ('По иску ООО «Истец г. Пример. Учебный» к Учебному учреждению им. А. Б. Макетова '
                '(ОГРН 1027700132195, Адрес: г. Москва, ул. Учебная, д. 1, кв. 2) о защите прав.')
        _, items = self.parties(text)
        self.assertEqual([item['role'] for item in items], ['plaintiff', 'defendant'])
        self.assertEqual(items[1]['fields']['legal_name']['value'], 'Учебному учреждению им. А. Б. Макетова')
        self.assertEqual(items[1]['fields']['ogrn']['value'], '1027700132195')

    def test_organization_mention_and_creditor_claim_are_not_bankruptcy_debtor(self):
        from sro_robot.core.entity_extract import has_explicit_entity_marker
        for text in ('В отношении ООО «Учебная организация» рассмотрен спор.',
                     'О признании требований ООО «Учебный кредитор» обоснованными.',
                     'В отношении ООО «Кредитор» как кредитора по делу о банкротстве гражданина.',
                     'В отношении ООО «Синтетическое банкротство» рассмотрен гражданский спор.',
                     'Название ООО «О банкротстве ООО Пример» не является ролью.'):
            self.assertFalse(has_explicit_entity_marker(text))


if __name__ == "__main__":
    unittest.main()
