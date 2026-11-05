"""Deterministic checks for core normalization and field extraction."""

from __future__ import annotations

import unittest
import unicodedata

from hypothesis import given, settings, strategies as st

from sro_robot.core.address import split_address
from sro_robot.core.extract import find_court, find_doc_date, parse
from sro_robot.core.models import Field, card_to_fields, confidence_at_least
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
    @settings(max_examples=300, derandomize=True, deadline=None)
    @given(value=st.floats(allow_nan=False, allow_infinity=False))
    def test_confidence_threshold_property_over_finite_float_domain(self, value: float) -> None:
        expected = 0.7 <= value <= 1.0
        self.assertEqual(confidence_at_least(value), expected)

    def test_confidence_requires_finite_value_in_unit_interval(self) -> None:
        for value, expected in (
            (0.7, True), (1.0, True), (0.69, False), (-0.1, False),
            (1.01, False), (float("inf"), False), (float("nan"), False),
            ("not-a-number", False), (True, False),
        ):
            with self.subTest(value=value):
                self.assertEqual(confidence_at_least(value), expected)

        self.assertTrue(Field("value", float("inf")).uncertain)

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
        self.assertEqual(card.court.source_variant, 0)
        self.assertEqual(text[card.court.source_start:card.court.source_end], card.court.value)

    def test_court_field_keeps_source_span_and_excerpt(self) -> None:
        text = "ОПРЕДЕЛЕНИЕ\nАрбитражный суд г. Москвы. РЕШЕНИЕ от 14 октября 2026 года"

        field = parse([text]).court

        self.assertEqual(field.source_variant, 0)
        self.assertEqual(text[field.source_start:field.source_end], field.value)
        self.assertIn(field.value, field.source_excerpt)

    def test_document_metadata_fields_keep_their_ocr_source_spans(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года. Дело № А41-61776/2026.\n"
            "Должник: Иванов Иван Иванович"
        )

        card = parse([text, text])

        for field in (card.doc_type, card.doc_date, card.case_number):
            with self.subTest(field=field.value):
                self.assertEqual(field.source_variant, 0)
                self.assertLess(field.source_start, field.source_end)
        self.assertEqual(text[card.doc_type.source_start:card.doc_type.source_end], "ОПРЕДЕЛЕНИЕ")
        self.assertEqual(text[card.case_number.source_start:card.case_number.source_end], "А41-61776/2026")
        self.assertEqual(text[card.doc_date.source_start:card.doc_date.source_end], "14 августа 2026")

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

        self.assertIsNone(parse([text]).fio_raw.value)

    def test_application_identity_is_suggested_for_review_without_debtor_role(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 1 января 2099 года, дело № А99-12345/2099. "
            "Рассмотрев заявление гражданки Макетовой (Синтетической) Анны Петровны, "
            "ИНН 772881234508, СНИЛС 112-233-445 95, "
            "адрес регистрации: 123456, вымышленный город, улица Учебная, дом 7, "
            "суд установил обстоятельства дела."
        )

        card = parse([text, text])

        self.assertFalse(card.fio_raw.value)
        self.assertFalse(card.last_name.value)
        self.assertFalse(card.inn.value)
        self.assertFalse(card.address.value)
        self.assertEqual(len(card.review_suggestions), 1)
        suggestion = card.review_suggestions[0]
        self.assertEqual(suggestion["kind"], "application_person")
        self.assertIn("связь с должником", suggestion["warning"])
        self.assertEqual(suggestion["fields"]["last_name"]["value"], "Макетова")
        self.assertEqual(suggestion["fields"]["first_name"]["value"], "Анна")
        self.assertEqual(suggestion["fields"]["middle_name"]["value"], "Петровна")
        self.assertEqual(suggestion["fields"]["inn"]["value"], "772881234508")
        self.assertEqual(suggestion["fields"]["snils"]["value"], "112-233-445 95")
        self.assertEqual(suggestion["fields"]["address"]["value"], "123456, вымышленный город, улица Учебная, дом 7")
        for item in suggestion["fields"].values():
            with self.subTest(field=item["value"][:3]):
                self.assertIsNotNone(item["source_variant"])
                self.assertIsNotNone(item["source_start"])
                self.assertIsNotNone(item["source_end"])
                self.assertTrue(item["source_excerpt"])
        self.assertNotIn("_review_suggestions_v1", card_to_fields(card))

    def test_hearing_application_keeps_labeled_person_details_for_review(self) -> None:
        text = (
            "Рассмотрев в открытом судебном заседании без участия представителей\n"
            "сторон, заявление Макетовой Анны Петровны (01.01.1980 г.р., место\n"
            "рождения: Учебноград Вымышленной ССР, адрес проживания: 123456, "
            "г. Учебный, ул. Тестовая, д. 7, кв. 1, ИНН 772881234508, "
            "СНИЛС 112-233-445 95) о признании ее несостоятельной (банкротом)."
        )
        card = parse([text])
        self.assertFalse(card.fio_raw.value)
        self.assertEqual(len(card.review_suggestions), 1)
        fields = card.review_suggestions[0]["fields"]
        self.assertEqual(fields["last_name"]["value"], "Макетова")
        self.assertEqual(fields["inn"]["value"], "772881234508")
        self.assertEqual(fields["snils"]["value"], "112-233-445 95")
        self.assertIn("кв. 1", fields["address"]["value"])

    def test_hearing_candidate_still_stops_before_another_person(self) -> None:
        text = (
            "Рассмотрев в закрытом судебном заседании заявление Макетовой Анны Петровны, "
            "место рождения: Учебноград Вымышленной ССР, "
            "Другов Иван Петрович, ИНН 772881234508, СНИЛС 112-233-445 95."
        )
        card = parse([text])
        self.assertEqual(len(card.review_suggestions), 1)
        fields = card.review_suggestions[0]["fields"]
        self.assertNotIn("inn", fields)
        self.assertNotIn("snils", fields)

    def test_application_review_suggestion_does_not_use_document_wide_identifiers(self) -> None:
        text = (
            "Рассмотрев заявление гражданки Макетовой Анны Петровны, "
            "суд установил обстоятельства. "
            "Позднее упомянуты ИНН 772881234508 и адрес регистрации: "
            "123456, вымышленный город, улица Учебная, дом 7."
        )

        card = parse([text])

        self.assertFalse(card.fio_raw.value)
        self.assertIsNone(card.inn.value)
        self.assertIsNone(card.address.value)
        suggestion = card.review_suggestions[0]
        self.assertEqual(suggestion["fields"]["fio"]["value"], "Макетовой Анны Петровны")
        self.assertNotIn("inn", suggestion["fields"])
        self.assertNotIn("address", suggestion["fields"])

    def test_insolvency_case_heading_produces_a_separate_review_candidate(self) -> None:
        text = (
            "Дело о несостоятельности (банкротстве) Макетовой Анны Петровны, "
            "дата рождения: 01.02.1980, место рождения: вымышленное село, "
            "ИНН 772881234508, "
            "СНИЛС 112-233-445 95, адрес регистрации: "
            "123456, вымышленный город, улица Учебная, дом 7."
        )

        card = parse([text, text])

        self.assertFalse(card.fio_raw.value)
        self.assertFalse(card.inn.value)
        self.assertEqual(len(card.review_suggestions), 1)
        fields = card.review_suggestions[0]["fields"]
        self.assertEqual(fields["fio"]["value"], "Макетовой Анны Петровны")
        self.assertEqual(fields["birth_date"]["value"], "01.02.1980")
        self.assertEqual(fields["birth_place"]["value"], "вымышленное село")
        self.assertEqual(fields["inn"]["value"], "772881234508")
        self.assertEqual(fields["snils"]["value"], "112-233-445 95")
        self.assertEqual(fields["address"]["value"], "123456, вымышленный город, улица Учебная, дом 7")

    def test_review_candidate_keeps_birth_date_and_partial_registration_address_as_review_only(self) -> None:
        text = (
            "Рассмотрев заявление гражданки Макетовой (Синтетической) Анны Петровны "
            "(21.08.1993 г.р.), ИНН 772881234508, адрес регистрации: "
            "вымышленный город, улица Учебная, место рождения: вымышленный край."
        )

        card = parse([text])
        fields = card.review_suggestions[0]["fields"]

        self.assertEqual(fields["birth_date"]["value"], "21.08.1993")
        self.assertEqual(fields["address"]["value"], "вымышленный город, улица Учебная")
        self.assertEqual(fields["address_kind"]["value"], "registration")
        self.assertNotIn("address", card.review_suggestions[0]["copyable_fields"])
        self.assertNotIn("address_kind", card.review_suggestions[0]["copyable_fields"])
        self.assertIsNone(card.address.value)
        self.assertEqual(card.fio_raw.source, "role_ambiguous")
        self.assertTrue(card.fio_raw.warning)

    def test_unlabeled_parenthesized_date_is_not_birth_date_candidate(self) -> None:
        for suffix in ("поступило в суд", "гражданин подал документы"):
            with self.subTest(suffix=suffix):
                card = parse([f"Рассмотрев заявление гражданки Макетовой Анны Петровны (14.10.2099 {suffix})."])
                fields = card.review_suggestions[0]["fields"]

                self.assertNotIn("birth_date", fields)

    def test_oversized_candidate_name_is_rejected_before_normalization(self) -> None:
        long_name = "А" + "а" * 1200
        card = parse([f"Рассмотрев заявление гражданки {long_name} Анны Петровны."])

        self.assertEqual(card.review_suggestions, [])
        self.assertFalse(card.fio_raw.value)
        self.assertEqual(card.fio_raw.source, "role_ambiguous")

    def test_repeated_same_name_anchors_do_not_merge_identifiers_or_addresses(self) -> None:
        text = (
            "Рассмотрев заявление гражданки Макетовой Анны Петровны, ИНН 772881234508. "
            "Позже, рассмотрев заявление гражданки Макетовой Анны Петровны, "
            "адрес регистрации: вымышленный город, улица Учебная, дом 8."
        )

        card = parse([text])
        suggestions = card.review_suggestions

        self.assertEqual(len(suggestions), 2)
        fields = [item["fields"] for item in suggestions]
        self.assertTrue(any("inn" in item and "address" not in item for item in fields))
        self.assertTrue(any("address" in item and "inn" not in item for item in fields))
        self.assertEqual(card.fio_raw.source, "role_ambiguous")
        self.assertTrue(card.fio_raw.warning)

    def test_application_candidate_stays_review_only_when_debtor_role_is_ambiguous(self) -> None:
        text = (
            "Рассмотрев заявление гражданки Макетовой Анны Петровны. "
            "Должник: Иванова Анна Сергеевна; должник Петров Петр Петрович."
        )

        card = parse([text])

        self.assertIsNone(card.fio_raw.value)
        self.assertEqual(card.fio_raw.source, "role_ambiguous")
        self.assertTrue(card.review_suggestions)

    def test_parser_handles_previous_surname_in_parentheses(self) -> None:
        text = "Должник Сидоровой (Петровой) Анны Сергеевны дата рождения: 01.02.1980"

        self.assertEqual(parse([text]).fio_raw.value, "Сидоровой Анны Сергеевны")

    def test_parser_matches_capitalized_debtor_label_without_loosening_name_shape(self) -> None:
        capitalized = parse(["Должник: Ладоберегова Анна Петровна"])
        lowercase = parse(["ДОЛЖНИК: ладоберегова анна петровна"])
        self.assertEqual(capitalized.fio_raw.value, "Ладоберегова Анна Петровна")
        self.assertIsNone(lowercase.fio_raw.value)

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
        card = parse([text])
        self.assertEqual(card.fio_raw.value, "Сидоров Алексей Петрович")
        self.assertEqual(card.inn.value, "772881234508")

    def test_does_not_use_applicant_identity_or_inn_when_debtor_is_not_a_person(self) -> None:
        text = (
            "Заявитель Петрова Мария Ивановна, ИНН 772881234508. "
            "Должник: ООО Ромашка"
        )
        card = parse([text])
        self.assertIsNone(card.fio_raw.value)
        self.assertIsNone(card.inn.value)

    def test_does_not_treat_debtor_representative_as_the_debtor(self) -> None:
        for text in (
            "Заявитель Петрова Мария Ивановна. Представитель должника: Сидоров Алексей Петрович",
            "Представитель гражданина-должника: Иванов Иван Иванович",
        ):
            with self.subTest(text=text):
                card = parse([text])
                self.assertIsNone(card.fio_raw.value)
                self.assertNotEqual(card.fio_raw.source, "debtor_role:v1")

    def test_ambiguous_debtor_people_are_left_for_employee_review(self) -> None:
        text = "Должник: Сидоров Алексей Петрович; должник Иванова Анна Сергеевна"
        card = parse([text])
        self.assertIsNone(card.fio_raw.value)
        self.assertEqual(card.fio_raw.source, "role_ambiguous")

    def test_parser_after_citizen_reference_skips_grammatical_prefix(self) -> None:
        cases = (
            ("В отношении гражданки Ивановой Анны Сергеевны введена процедура",
             "Ивановой Анны Сергеевны"),
            ("Должник: гражданка Иванова Анна Сергеевна, дата рождения 01.01.1980",
             "Иванова Анна Сергеевна"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse([text]).fio_raw.value, expected)

    def test_self_bankruptcy_application_has_explicit_debtor_role(self) -> None:
        text = "Суд рассмотрел заявление Бурцева Егора Анатольевича о признании себя банкротом"
        card = parse([text])
        self.assertEqual(card.fio_raw.value, "Бурцева Егора Анатольевича")
        self.assertEqual(card.fio_raw.source, "debtor_role:v1")

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

    def test_review_address_keeps_wrapped_apartment_closing_biography(self) -> None:
        text = (
            "Рассмотрев заявление Макетова Ивана Ивановича (01.01.1980 г.р., "
            "адрес регистрации: 123456, г. Примерск, ул. Макетная, д. 7, кв.\n"
            "1) о признании его несостоятельным (банкротом)."
        )
        card = parse([text])
        addresses = [item["fields"]["address"] for item in card.review_suggestions if "address" in item["fields"]]
        self.assertEqual(len(addresses), 1)
        self.assertEqual(addresses[0]["value"], "123456, г. Примерск, ул. Макетная, д. 7, кв. 1")
        self.assertTrue(text[addresses[0]["source_start"]:addresses[0]["source_end"]].endswith("кв.\n1"))
        self.assertIsNone(card.address.value, "review suggestion must not become an authoritative debtor address")

    def test_review_address_does_not_take_numbered_item_without_biography(self) -> None:
        text = (
            "Рассмотрев заявление Макетова Ивана Ивановича, "
            "адрес регистрации: 123456, г. Примерск, ул. Макетная, д. 7, кв.\n"
            "1) о признании его несостоятельным (банкротом)."
        )
        card = parse([text])
        addresses = [item["fields"]["address"] for item in card.review_suggestions if "address" in item["fields"]]
        self.assertTrue(addresses)
        self.assertTrue(all("кв. 1" not in item["value"] for item in addresses))

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

        card = parse([
            "Должник: Иванова Анна Сергеевна, ИНН 772881234508, " + text,
        ])
        self.assertEqual(card.address.value, "123456, Тестовая область, г. Примерск, ул. Макетная, д. 7")
        self.assertEqual(card.address_kind.value, "registration")

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
