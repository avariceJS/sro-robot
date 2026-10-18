"""Deterministic checks for core normalization and field extraction."""

from __future__ import annotations

import unittest
import unicodedata

from hypothesis import given, settings, strategies as st

from sro_robot.core.address import split_address
from sro_robot.core.extract import find_address, find_court, find_doc_date, find_fio, parse
from sro_robot.scenarios.person_card import PersonCardScenario
from sro_robot.core.validators import (
    normalize_case_number,
    digits,
    is_digit_presentation,
    valid_inn,
    valid_ogrn,
    valid_ogrnip,
    valid_snils,
)


class CoreNormalizationTests(unittest.TestCase):
    @settings(max_examples=250, derandomize=True, deadline=None)
    @given(value=st.text(
        alphabet=st.characters(
            categories=("Nd",), exclude_characters="0123456789",
        ),
        min_size=1,
        max_size=40,
    ))
    def test_unicode_decimal_digits_normalize_to_ascii(self, value: str) -> None:
        self.assertEqual(
            digits(value),
            "".join(str(unicodedata.decimal(character)) for character in value),
        )

    def test_case_number_normalization_is_idempotent(self) -> None:
        normalized = normalize_case_number(" № a41 - 61776 / 2026 ")

        self.assertEqual(normalized, "А41-61776/2026")
        self.assertEqual(normalize_case_number(normalized), normalized)

    def test_inn_checksum_accepts_fictional_valid_value_and_rejects_changed_check_digit(self) -> None:
        self.assertTrue(valid_inn("772881234508"))
        self.assertFalse(valid_inn("772881234509"))

    @settings(max_examples=4, derandomize=True, deadline=None)
    @given(length=st.sampled_from((10, 12)))
    def test_all_zero_inn_is_rejected_for_each_supported_length(self, length: int) -> None:
        self.assertFalse(valid_inn("0" * length))

    def test_registry_identifiers_reject_all_zero_and_non_ip_ogrnip_type(self) -> None:
        self.assertFalse(valid_ogrn("0" * 13))
        # Check digit matches, but 2 is not a valid first digit for an OGRN.
        self.assertFalse(valid_ogrn("2027700132194"))
        self.assertTrue(valid_ogrn("5027700132191"))
        self.assertFalse(valid_ogrnip("0" * 15))
        # This number's last digit satisfies the OGRNIP checksum formula, but
        # FNS defines the first digit 3 as the individual-entrepreneur type.
        self.assertFalse(valid_ogrnip("119500100000007"))

    def test_digit_presentation_allows_only_digits_spaces_and_hyphens(self) -> None:
        self.assertTrue(is_digit_presentation("1 027-700 132195"))
        self.assertFalse(is_digit_presentation("1027X00132195"))
        self.assertFalse(is_digit_presentation("1027.001.32195"))

    def test_snils_rejects_invalid_length_and_zero_number(self) -> None:
        self.assertFalse(valid_snils("1234567890"))
        self.assertFalse(valid_snils("00000000000"))


class ExtractionTests(unittest.TestCase):
    def test_court_extraction_keeps_multiword_jurisdiction(self) -> None:
        cases = (
            ("Арбитражный суд города Москвы", "Арбитражный суд города Москвы"),
            ("Арбитражный суд г. Москвы", "Арбитражный суд г. Москвы"),
            (
                "Арбитражный суд города Санкт-Петербурга и Ленинградской области",
                "Арбитражный суд города Санкт-Петербурга и Ленинградской области",
            ),
            ("Арбитражный суд Республики Татарстан", "Арбитражный суд Республики Татарстан"),
            (
                "Арбитражный суд Северо-Кавказского округа",
                "Арбитражный суд Северо-Кавказского округа",
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(find_court(text), expected)

    def test_court_extraction_stops_before_case_and_date_on_same_line(self) -> None:
        text = (
            "Арбитражный суд г. Москвы. РЕШЕНИЕ 14 октября 2026 года "
            "по делу А40-12345/2026"
        )

        self.assertEqual(find_court(text), "Арбитражный суд г. Москвы")

    def test_conflicting_court_mentions_require_manual_review(self) -> None:
        text = (
            "РЕШЕНИЕ\nАрбитражный суд г. Москвы\n"
            "Дело передано для рассмотрения в Арбитражный суд Московской области."
        )

        card = parse([text])

        self.assertEqual(card.court.value, "Арбитражный суд г. Москвы")
        self.assertIn("расходится", card.court.warning)

    def test_repeated_same_court_mention_does_not_add_conflict_warning(self) -> None:
        text = (
            "Арбитражный суд города Москвы. РЕШЕНИЕ. "
            "Дело рассматривается Арбитражный суд города Москвы."
        )

        self.assertIsNone(parse([text]).court.warning)

    def test_document_date_comes_from_header(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года\n"
            "УСТАНОВИЛ: Заседание назначено на 22 сентября 2026 года"
        )

        self.assertEqual(find_doc_date(text), "14.08.2026")

    def test_date_of_a_previously_cited_act_requires_review(self) -> None:
        text = (
            "РЕШЕНИЕ. Ранее принятое решение от 14 августа 2025 года упомянуто в споре. "
            + "Текст обстоятельств без указания даты самого акта. " * 6
            + "УСТАНОВИЛ: Иванов обратился в суд."
        )

        card = parse([text])
        self.assertEqual(card.doc_date.value, "14.08.2025")
        self.assertIn("неоднозначна", card.doc_date.warning)

    def test_document_type_uses_earliest_act_heading_before_later_citations(self) -> None:
        cases = (
            (
                "РЕШЕНИЕ\nпо делу А99-123/2099. Ранее вынесенное ОПРЕДЕЛЕНИЕ "
                "обжаловалось в ПОСТАНОВЛЕНИИ.",
                "решение",
            ),
            (
                "ПОСТАНОВЛЕНИЕ\nпроверяет решение суда и определение первой инстанции.",
                "постановление",
            ),
            (
                "ОПРЕДЕЛЕНИЕ\nпо делу А99-456/2099. Суд сослался на прежнее РЕШЕНИЕ.",
                "определение",
            ),
        )

        for text, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(parse([text]).doc_type.value, expected)

    def test_case_and_act_date_collisions_require_review(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года по делу № А41-61776/2026. "
            "Заявитель родился 01.01.1980; упомянуто дело № А40-12345/2025. "
            "Должник: Иванов Иван Иванович, ИНН 772881234508"
        )
        card = parse([text])
        self.assertIsNotNone(card.case_number.warning)
        self.assertIsNotNone(card.doc_date.warning)

    def test_date_without_act_heading_is_not_auto_evidence(self) -> None:
        card = parse([
            "Заявитель родился 01.01.1980. Дело № А41-61776/2026. "
            "Должник: Иванов Иван Иванович, ИНН 772881234508"
        ])
        self.assertEqual(card.doc_date.value, "01.01.1980")
        self.assertIsNotNone(card.doc_date.warning)

    def test_birth_date_near_act_heading_is_not_auto_evidence(self) -> None:
        card = parse([
            "ОПРЕДЕЛЕНИЕ. Заявитель Иванов Иван Иванович, дата рождения 01.01.1980. "
            "Должник: Петров Петр Петрович, ИНН 772881234508"
        ])
        self.assertEqual(card.doc_date.value, "01.01.1980")
        self.assertIsNotNone(card.doc_date.warning)

    def test_unambiguous_act_heading_date_needs_no_warning(self) -> None:
        card = parse([
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года. Дело № А41-61776/2026. "
            "Должник: Иванов Иван Иванович, ИНН 772881234508"
        ])
        self.assertEqual(card.doc_date.value, "14.08.2026")
        self.assertIsNone(card.doc_date.warning)
        self.assertIsNone(card.case_number.warning)

    def test_unlabeled_application_is_not_a_debtor_role(self) -> None:
        text = "Суд рассмотрев заявление Бурцева Егора Анатольевича установил следующее"

        self.assertIsNone(find_fio(text))
        self.assertIsNone(parse([text]).fio_raw.value)

    def test_fio_rule_handles_previous_surname_in_parentheses(self) -> None:
        text = "Должник Сидоровой (Петровой) Анны Сергеевны дата рождения: 01.02.1980"

        self.assertEqual(find_fio(text), "Сидоровой Анны Сергеевны")

    def test_fio_rule_matches_capitalized_debtor_label_without_loosening_name_shape(self) -> None:
        self.assertEqual(find_fio("Должник: Ладоберегова Анна Петровна"), "Ладоберегова Анна Петровна")
        self.assertIsNone(find_fio("ДОЛЖНИК: ладоберегова анна петровна"))

    def test_explicit_nominative_female_surname_is_not_masculinized(self) -> None:
        for surname in ("Иванова", "Ладоберегова"):
            with self.subTest(surname=surname):
                card = parse([f"Должник: {surname} Анна Петровна"])
                self.assertEqual(card.last_name.value, surname)
                self.assertEqual(card.first_name.value, "Анна")
                self.assertIsNone(card.last_name.warning)

    def test_debtor_label_with_oblique_name_requires_morphology_review(self) -> None:
        card = parse(["Должник: Сидоровой Анны Сергеевны, ИНН 772881234508"])
        self.assertEqual(card.fio_raw.source, "debtor_role:v1")
        self.assertEqual(card.fio, "Сидорова Анна Сергеевна")
        for field in (card.last_name, card.first_name, card.middle_name):
            self.assertIsNotNone(field.warning)

    def test_male_genitive_surname_after_debtor_label_requires_review(self) -> None:
        card = parse(["Должник: Петрова Петр Петрович, ИНН 772881234508"])
        self.assertEqual(card.fio, "Петров Пётр Петрович")
        self.assertIsNotNone(card.last_name.warning)

    def test_debtor_identity_wins_over_applicant_mentioned_earlier(self) -> None:
        text = (
            "Рассмотрев заявление Петровой Марии Ивановны. "
            "Заявитель Петрова Мария Ивановна. "
            "Должник: Сидоров Алексей Петрович, ИНН 772881234508"
        )
        self.assertEqual(find_fio(text), "Сидоров Алексей Петрович")
        self.assertEqual(parse([text]).inn.value, "772881234508")

    def test_does_not_use_applicant_identity_or_inn_when_debtor_is_not_a_person(self) -> None:
        text = (
            "Заявитель Петрова Мария Ивановна, ИНН 772881234508. "
            "Должник: ООО Ромашка"
        )
        self.assertIsNone(find_fio(text))
        self.assertIsNone(parse([text]).inn.value)

    def test_does_not_treat_debtor_representative_as_the_debtor(self) -> None:
        text = "Заявитель Петрова Мария Ивановна. Представитель должника: Сидоров Алексей Петрович"
        self.assertIsNone(find_fio(text))
        self.assertIsNone(find_fio("Представитель гражданина-должника: Иванов Иван Иванович"))

    def test_ambiguous_debtor_people_are_left_for_employee_review(self) -> None:
        text = "Должник: Сидоров Алексей Петрович; должник Иванова Анна Сергеевна"
        self.assertIsNone(find_fio(text))

    def test_fio_after_citizen_reference_skips_grammatical_prefix(self) -> None:
        self.assertEqual(
            find_fio("В отношении гражданки Ивановой Анны Сергеевны введена процедура"),
            "Ивановой Анны Сергеевны",
        )
        self.assertEqual(
            find_fio("Должник: гражданка Иванова Анна Сергеевна, дата рождения 01.01.1980"),
            "Иванова Анна Сергеевна",
        )

    def test_self_bankruptcy_application_has_explicit_debtor_role(self) -> None:
        text = "Суд рассмотрел заявление Бурцева Егора Анатольевича о признании себя банкротом"
        self.assertEqual(find_fio(text), "Бурцева Егора Анатольевича")
        self.assertEqual(parse([text]).fio_raw.source, "debtor_role:v1")

    def test_competing_party_fields_never_fill_debtor_card(self) -> None:
        text = (
            "Заявитель: Иванова Анна Сергеевна, СНИЛС 112-233-445 95, "
            "адрес регистрации: 123456, г. Примерск, ул. Тестовая, д. 7. "
            "Должник: Петров Петр Петрович, ИНН 772881234508. "
            "Кредитора Сидорова Мария Ивановна, ИНН 500100732259, "
            "СНИЛС 123-456-789 64, адрес регистрации: 654321, г. Тестоград."
        )
        card = parse([text, text])
        self.assertEqual(card.fio_raw.value, "Петров Петр Петрович")
        self.assertEqual(card.fio_raw.source, "debtor_role:v1")
        self.assertEqual(card.inn.value, "772881234508")
        self.assertEqual(card.inn.source, "debtor_role:v1")
        self.assertIsNone(card.snils.value)
        self.assertIsNone(card.address.value)

    def test_scoped_debtor_identifiers_and_address_keep_role_marker(self) -> None:
        text = (
            "Должника: Иванова Анна Сергеевна, ИНН 772881234508, "
            "СНИЛС 112-233-445 95, адрес регистрации: 123456, г. Примерск, ул. Макетная, д. 7\n\n"
            "Заявителя: Петров Петр Петрович, ИНН 500100732259"
        )
        card = parse([text])
        self.assertEqual(card.fio_raw.value, "Иванова Анна Сергеевна")
        self.assertEqual(card.snils.value, "112-233-445 95")
        self.assertEqual(card.address.value, "123456, г. Примерск, ул. Макетная, д. 7")
        for field in (card.fio_raw, card.inn, card.snils, card.address):
            self.assertEqual(field.source, "debtor_role:v1")
            self.assertIsNone(field.warning)

    def test_scoped_address_stops_at_end_of_line_after_house(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ\n1 января 2099 года\nДело № А99-900001/2099\n"
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n"
            "Адрес регистрации: 101000, вымышленный город Макетный, улица Учебная, дом 1\n"
            "Документ сформирован для проверки. Следующее предложение не является адресом."
        )
        card = parse([text])
        self.assertEqual(
            card.address.value,
            "101000, вымышленный город Макетный, улица Учебная, дом 1",
        )
        self.assertEqual(card.address.source, "debtor_role:v1")
        self.assertIsNone(card.address.warning)

    def test_scoped_address_stops_before_same_line_prose_and_keeps_room(self) -> None:
        text = (
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508, "
            "адрес регистрации: 123456, г. Примерск, ул. Макетная, д. 7, кв. 42. "
            "Документ содержит описание дела."
        )
        self.assertEqual(
            parse([text]).address.value,
            "123456, г. Примерск, ул. Макетная, д. 7, кв. 42",
        )

    def test_scoped_multiline_address_keeps_house_and_room(self) -> None:
        text = (
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n"
            "Адрес регистрации: 123456, г. Примерск,\n"
            "ул. Макетная, д. 7,\nкв. 42\n"
            "Документ содержит описание дела."
        )
        card = parse([text])
        self.assertEqual(card.address.value, "123456, г. Примерск, ул. Макетная, д. 7, кв. 42")
        self.assertIsNone(card.address.warning)

    def test_scoped_address_without_clear_house_boundary_requires_review(self) -> None:
        text = (
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n"
            "Адрес регистрации: 123456, г. Примерск, ул. Макетная,\n"
            "Документ содержит дом 99 и иные сведения."
        )
        card = parse([text])
        self.assertIsNone(card.address.value)
        self.assertIsNotNone(card.address.warning)

    def test_conflicting_ocr_debtor_identities_fail_closed(self) -> None:
        first = "Должник: Иванова Анна Сергеевна, ИНН 772881234508"
        second = "Должник: Петров Петр Петрович, ИНН 500100732259"
        card = parse([first, second])
        for field in (card.fio_raw, card.inn, card.snils, card.address):
            self.assertIsNone(field.value)
            self.assertIsNotNone(field.warning)

    def test_conflicting_ocr_identifier_does_not_splice_person_fields(self) -> None:
        first = "Должник: Иванова Анна Сергеевна, ИНН 772881234508"
        second = "Должник: Иванова Анна Сергеевна, ИНН 500100732259"
        card = parse([first, second])
        self.assertEqual(card.fio_raw.source, "debtor_role:v1")
        self.assertIsNone(card.inn.value)
        self.assertIsNotNone(card.inn.warning)

    def test_one_valid_inn_survives_damaged_ocr_only_as_review_suggestion(self) -> None:
        valid = "Должник: Иванова Анна Сергеевна, ИНН 772881234508"
        damaged = "Должник: Иванова Анна Сергеевна, ИНН 7728812345089"
        card = parse([valid, valid, valid, damaged])

        self.assertEqual(card.inn.value, "772881234508")
        self.assertEqual(card.inn.source, "role_partial")
        self.assertIsNotNone(card.inn.warning)

    def test_multiple_valid_inns_never_collapse_to_a_single_suggestion(self) -> None:
        first = "Должник: Иванова Анна Сергеевна, ИНН 772881234508"
        second = "Должник: Иванова Анна Сергеевна, ИНН 500100732259"
        damaged = "Должник: Иванова Анна Сергеевна, ИНН 7728812345089"
        card = parse([first, first, second, damaged])

        self.assertIsNone(card.inn.value)
        self.assertEqual(card.inn.source, "role_ambiguous")
        self.assertIsNotNone(card.inn.warning)

    def test_inn_absent_from_all_debtor_sections_stays_empty(self) -> None:
        card = parse([
            "ОПРЕДЕЛЕНИЕ 14 октября 2099 года\nДело № А99-12345/2099\n"
            "Заявитель: Петров Петр Петрович, ИНН 500100732259\n"
            "Должник: Иванова Анна Сергеевна"
        ])

        self.assertIsNone(card.inn.value)

    @given(
        surname=st.sampled_from(("Петров", "Сидоров", "Макетова", "Вымышленная")),
        first=st.sampled_from(("Петр", "Егор", "Лидия", "Ольга")),
        middle=st.sampled_from(("Петрович", "Алексеевич", "Ивановна", "Сергеевна")),
        applicant_inn=st.sampled_from(("500100732259", "781108041773")),
        applicant_first=st.booleans(),
    )
    def test_applicant_fields_never_replace_debtor_when_party_order_changes(
        self, surname, first, middle, applicant_inn, applicant_first
    ) -> None:
        applicant = f"Заявитель: {surname} {first} {middle}, ИНН {applicant_inn}"
        debtor = "Должник: Иванова Анна Сергеевна, ИНН 772881234508"
        parties = (applicant, debtor) if applicant_first else (debtor, applicant)
        card = parse([". ".join(parties)])

        self.assertEqual(card.fio, "Иванова Анна Сергеевна")
        self.assertEqual(card.inn.value, "772881234508")

    def test_blank_line_field_continuation_needs_direct_ocr_corroboration(self) -> None:
        inline = "Должник: Иванова Анна Сергеевна, ИНН 772881234508"
        split = "Должник: Иванова Анна Сергеевна\n\nИНН 772881234508"
        card = parse([inline, split])
        self.assertEqual(card.inn.value, "772881234508")
        self.assertEqual(card.inn.source, "debtor_role:v1")
        self.assertIsNone(card.inn.warning)

        uncorroborated = parse([split, split])
        self.assertEqual(uncorroborated.inn.value, "772881234508")
        self.assertEqual(uncorroborated.inn.source, "role_partial")
        self.assertIsNotNone(uncorroborated.inn.warning)

    def test_blank_line_continuation_never_crosses_new_applicant_or_representative(self) -> None:
        for later in (
            "Заявитель: Петров Петр Петрович, ИНН 772881234508",
            "Представитель должника: Петров Петр Петрович, ИНН 772881234508",
        ):
            with self.subTest(later=later.split(":")[0]):
                card = parse(["Должник: Иванова Анна Сергеевна\n\n" + later])
                self.assertEqual(card.fio_raw.value, "Иванова Анна Сергеевна")
                self.assertIsNone(card.inn.value)

    def test_blank_line_continuation_stops_at_unlabeled_other_person(self) -> None:
        card = parse([
            "Должник: Иванова Анна Сергеевна\n\n"
            "Петров Петр Петрович\nИНН 772881234508"
        ])
        self.assertEqual(card.fio_raw.value, "Иванова Анна Сергеевна")
        self.assertIsNone(card.inn.value)

        same_line = parse([
            "Должник: Иванова Анна Сергеевна\n\n"
            "ИНН 772881234508, Петров Петр Петрович"
        ])
        self.assertIsNone(same_line.fio_raw.value)
        self.assertIsNone(same_line.inn.value)

        bare_identifier = parse([
            "Должник: Иванова Анна Сергеевна\n\n772881234508"
        ])
        self.assertIsNone(bare_identifier.inn.value)

    def test_genuine_ocr_absence_remains_partial_after_blank_line_fix(self) -> None:
        card = parse([
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508",
            "Должник: Иванова Анна Сергеевна",
        ])
        self.assertEqual(card.inn.value, "772881234508")
        self.assertEqual(card.inn.source, "role_partial")
        self.assertIsNotNone(card.inn.warning)

    def test_blank_line_identifier_conflict_remains_review_only(self) -> None:
        card = parse([
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508",
            "Должник: Иванова Анна Сергеевна\n\nИНН 500100732259",
        ])
        self.assertIsNone(card.inn.value)
        self.assertEqual(card.inn.source, "role_ambiguous")

    def test_mentioned_but_undelimited_address_after_blank_requires_review(self) -> None:
        card = parse([
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n\n"
            "Адрес регистрации: 123456, город Примерск"
        ])
        self.assertIsNone(card.address.value)
        self.assertEqual(card.address.source, "role_ambiguous")
        self.assertIsNotNone(card.address.warning)

    def test_valid_address_after_blank_needs_direct_ocr_corroboration(self) -> None:
        address = "Адрес регистрации: 123456, г. Примерск, ул. Макетная, д. 7"
        card = parse([
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508, " + address,
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n\n" + address,
        ])
        self.assertEqual(card.address.value, "123456, г. Примерск, ул. Макетная, д. 7")
        self.assertEqual(card.address.source, "debtor_role:v1")
        self.assertEqual(card.address_kind.value, "registration")

    def test_ocr_wrapped_city_fragment_and_street_remain_one_address(self) -> None:
        for separator in ("\n", "\n\n"):
            with self.subTest(separator=repr(separator)):
                card = parse([
                    "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n\n"
                    "Адрес регистрации: 101000, вымышленный город Макетны"
                    + separator + "й, улица Учебная, дом 1"
                ])
                self.assertEqual(
                    card.address.value,
                    "101000, вымышленный город Макетный, улица Учебная, дом 1",
                )
                self.assertEqual(card.address.source, "role_partial")
                self.assertIsNotNone(card.address.warning)

    def test_ocr_wrapped_address_does_not_absorb_prose_or_other_person(self) -> None:
        prose = parse([
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n\n"
            "Адрес регистрации: 101000, вымышленный город Макетны\n"
            "Документ содержит улицу Учебную и дом 1"
        ])
        self.assertIsNone(prose.address.value)
        self.assertIsNotNone(prose.address.warning)

        other_person = parse([
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n\n"
            "Адрес регистрации: 101000, вымышленный город Макетны\n"
            "й, улица Петров Петр Петрович, дом 1"
        ])
        self.assertIsNone(other_person.fio_raw.value)
        self.assertIsNone(other_person.address.value)

    def test_unlabeled_ocr_variant_prevents_auto_role_marker(self) -> None:
        card = parse([
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508",
            "Заявитель: Иванова Анна Сергеевна, ИНН 772881234508",
        ])
        self.assertEqual(card.fio_raw.value, "Иванова Анна Сергеевна")
        self.assertEqual(card.fio_raw.source, "role_partial")
        self.assertIsNotNone(card.fio_raw.warning)
        self.assertEqual(card.inn.source, "role_partial")
        self.assertIsNotNone(card.inn.warning)

    def test_sole_proprietor_and_multiple_debtors_require_review(self) -> None:
        for text in (
            "Должник: ИП Иванова Анна Сергеевна, ИНН 772881234508",
            "Должник: Иванова Анна Сергеевна; должник Петров Петр Петрович",
        ):
            with self.subTest(text=text):
                card = parse([text])
                self.assertIsNone(card.fio_raw.value)
                self.assertIsNotNone(card.fio_raw.warning)

    def test_unlabeled_second_person_inside_debtor_section_requires_review(self) -> None:
        text = (
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508; "
            "Петров Петр Петрович, СНИЛС 112-233-445 95"
        )
        card = parse([text])
        self.assertIsNone(card.fio_raw.value)
        self.assertIsNone(card.snils.value)
        self.assertIsNotNone(card.fio_raw.warning)

    def test_review_fields_omit_birth_date_and_birth_place(self) -> None:
        attrs = [spec.attr for spec in PersonCardScenario.fields]
        titles = [spec.title for spec in PersonCardScenario.fields]

        self.assertNotIn("birth_date", attrs)
        self.assertNotIn("birth_place", attrs)
        self.assertNotIn("Дата рождения", titles)
        self.assertNotIn("Место рождения", titles)

        card = parse([
            "Должник Сидоровой Анны Сергеевны, дата рождения: 01.02.1980, "
            "место рождения: г. Синтетический, адрес регистрации: 123456, "
            "Тестовая область, г. Примерск, ул. Макетная, д. 7"
        ])
        self.assertFalse(hasattr(card, "birth_date"))
        self.assertFalse(hasattr(card, "birth_place"))
        self.assertEqual(card.address.value, "123456, Тестовая область, г. Примерск, ул. Макетная, д. 7")

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
