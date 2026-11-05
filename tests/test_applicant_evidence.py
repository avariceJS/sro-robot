from __future__ import annotations

import unittest

from sro_robot.core.entity_extract import parse_entity
from sro_robot.core.extract import parse


class ApplicantEvidenceTests(unittest.TestCase):
    def test_unnamed_debtor_mention_preceding_explicit_person_does_not_hide_identity(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Суд рассмотрел заявление о признании должника банкротом.\n"
            "Должник: гражданин Иванов Иван Иванович ИНН 500100001146\n"
        )
        card = parse([text])
        self.assertEqual(card.fio_raw.value, "Иванов Иван Иванович")

    def test_damaged_explicit_debtor_name_blocks_second_identity_promotion(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Должник: Петров Пётр, ИНН 500100001146.\n"
            "Должник: Иванов Иван Иванович, ИНН 500100001146."
        )
        card = parse([text])
        self.assertIsNone(card.fio_raw.value)
        self.assertEqual(card.fio_raw.source, "role_ambiguous")
        self.assertIsNone(card.inn.value)

    def test_bankruptcy_subject_binds_role_without_narrative_identifiers(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Рассмотрено дело о банкротстве гражданина Иванова Ивана Ивановича.\n"
            "Представитель заявителя: Петров Пётр Петрович, ИНН 500100001146."
        )
        card = parse([text])
        self.assertEqual(card.fio_raw.value, "Иванова Ивана Ивановича")
        self.assertEqual(card.fio_raw.source, "debtor_role:v1")
        self.assertIsNone(card.inn.value)
        self.assertTrue(any(
            item["kind"] == "application_person"
            and item["fields"]["fio"]["value"] == "Иванова Ивана Ивановича"
            for item in card.review_suggestions
        ))

    def test_parenthetical_reverse_person_role_is_reviewable(self) -> None:
        card = parse([
            "Дело А40-12345/2026\n"
            "Иванов Иван Иванович (далее — должник), ИНН 500100001146"
        ])
        self.assertEqual(card.fio_raw.value, "Иванов Иван Иванович")
        self.assertEqual(card.fio_raw.source, "debtor_role:v1")
        self.assertEqual(card.inn.value, "500100001146")
        self.assertTrue(any(
            item["kind"] == "application_person"
            and item["fields"]["fio"]["value"] == "Иванов Иван Иванович"
            for item in card.review_suggestions
        ))

    def test_fifth_photo_pass_candidate_remains_visible_but_incomplete(self) -> None:
        fifth = (
            "Дело А40-12345/2026\n"
            "Иванов Иван Иванович (далее — должник), ИНН 500100001146"
        )
        card = parse(["Неразборчивый текст"] * 4 + [fifth])
        self.assertEqual(card.fio_raw.value, "Иванов Иван Иванович")
        self.assertEqual(card.fio_raw.source, "role_partial")
        self.assertFalse(card.review_suggestions_complete)
        self.assertEqual(len(card.review_suggestions), 1)
        self.assertEqual(card.review_suggestions[0]["fields"]["fio"]["source_variant"], 4)

    def test_bounded_address_survives_damaged_ocr_variant_for_review(self) -> None:
        head = "Должник: гражданин Иванов Иван Иванович\n"
        good = head + "Адрес регистрации: г. Макетный, ул. Учебная, д. 7, кв. 1"
        damaged = head + "Адрес регистрации: г. Макетный, ул. Учебная"
        card = parse([good, damaged])
        self.assertEqual(card.address.value, "г. Макетный, ул. Учебная, д. 7, кв. 1")
        self.assertEqual(card.address.source, "role_partial")
        self.assertTrue(card.address.warning)
        self.assertEqual(card.address.source_variant, 0)
        self.assertEqual(good[card.address.source_start:card.address.source_end], card.address.value)
        self.assertEqual(card.address_kind.value, "registration")
        self.assertEqual(card.address_kind.source, "role_partial")

    def test_conflicting_complete_addresses_remain_withheld(self) -> None:
        head = "Должник: гражданин Иванов Иван Иванович\n"
        first = head + "Адрес регистрации: г. Макетный, ул. Учебная, д. 7"
        second = head + "Адрес регистрации: г. Макетный, ул. Учебная, д. 8"
        card = parse([first, second])
        self.assertIsNone(card.address.value)
        self.assertTrue(card.address.warning)

    def test_checksum_valid_identity_block_survives_unknown_surname_for_review(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Сведения о лице: Кьюцц Иван Иванович, ИНН 500100001146, "
            "СНИЛС 112-233-445 95."
        )
        card = parse([text])
        self.assertIsNone(card.fio_raw.value)
        self.assertEqual(len(card.review_suggestions), 1)
        candidate = card.review_suggestions[0]
        self.assertEqual(candidate["fields"]["fio"]["value"], "Кьюцц Иван Иванович")
        self.assertEqual(candidate["fields"]["inn"]["value"], "500100001146")

    def test_identity_block_does_not_cross_another_participant_role(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Кьюцц Иван Иванович, представитель кредитора, "
            "ИНН 500100001146, СНИЛС 112-233-445 95."
        )
        card = parse([text])
        self.assertFalse(card.review_suggestions)
        self.assertIsNone(card.fio_raw.value)

    def test_person_inn_does_not_cross_next_name_in_either_direction(self) -> None:
        first = "Кьюцц Иван Иванович"
        second = "Зьюцц Пётр Петрович"
        forward = (
            f"Дело А40-12345/2026\nГражданин {first}. "
            f"Гражданин {second}, ИНН 500100001146, СНИЛС 112-233-445 95."
        )
        reverse = (
            f"Дело А40-12345/2026\nИНН 500100001146, СНИЛС 112-233-445 95. "
            f"Гражданин {first}. Гражданин {second}."
        )
        for text, owner, foreign in ((forward, second, first), (reverse, first, second)):
            with self.subTest(owner=owner):
                card = parse([text])
                assigned = {
                    item["fields"]["fio"]["value"]: item["fields"].get("inn", {}).get("value")
                    for item in card.review_suggestions if "fio" in item["fields"]
                }
                self.assertEqual(assigned.get(owner), "500100001146")
                self.assertNotEqual(assigned.get(foreign), "500100001146")

    def test_damaged_second_name_still_bounds_first_person_id(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Гражданин Кьюцц Иван Иванович. "
            "Гражданин Зьюцц Пётр Тестовый, ИНН 500100001146."
        )
        card = parse([text])
        for item in card.review_suggestions:
            if item["fields"].get("fio", {}).get("value") == "Кьюцц Иван Иванович":
                self.assertNotIn("inn", item["fields"])

    def test_judge_name_without_personal_identifier_is_not_a_candidate(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Судья Кьюцц Иван Иванович рассмотрел заявление о банкротстве должника."
        )
        self.assertFalse(parse([text]).review_suggestions)

    def test_explicit_applicant_is_separate_from_person_debtor(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Заявитель: ООО «Учебная организация»\n"
            "Должник: гражданин Иванов Иван Иванович ИНН 500100001146\n"
        )
        card = parse([text])
        evidence = card.applicant_evidence
        self.assertEqual(card.fio_raw.value, "Иванов Иван Иванович")
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["role_source"], "applicant_role:v1")
        self.assertEqual(evidence["name"], "ООО «Учебная организация»")
        self.assertEqual(evidence["case_number"], "А40-12345/2026")
        self.assertEqual(text[evidence["source_start"]:evidence["source_end"]], evidence["name"])

    def test_explicit_applicant_is_separate_from_legal_debtor(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Заявитель: гражданин Петров Пётр Петрович\n"
            "Должник: ООО «Синтетическая компания» ИНН 7700000016\n"
        )
        card = parse_entity([text])
        self.assertEqual(card.legal_name.value, "ООО «Синтетическая компания»")
        self.assertEqual(card.applicant_evidence["name"], "гражданин Петров Пётр Петрович")

    def test_parenthetical_legal_debtor_role_is_reviewable(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "ООО «Синтетическая компания» (далее — должник), ИНН 7700000016\n"
        )
        card = parse_entity([text])
        self.assertEqual(card.entity_kind.value, "legal_entity")
        self.assertEqual(card.legal_name.value, "ООО «Синтетическая компания»")
        self.assertEqual(card.inn.value, "7700000016")

    def test_wrapped_legal_name_with_comma_role_annotation_is_reviewable(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Общество с ограниченной ответственностью\n"
            "«Синтетическая компания» (далее — должник, ИНН 7700000016)\n"
        )
        card = parse_entity([text])
        self.assertEqual(card.entity_kind.value, "legal_entity")
        self.assertEqual(
            card.legal_name.value,
            "Общество с ограниченной ответственностью «Синтетическая компания»",
        )
        self.assertEqual(card.inn.value, "7700000016")

    def test_noisy_repeated_entity_role_keeps_majority_name_review_only(self) -> None:
        agreed = (
            "Дело А40-12345/2026\n"
            "ООО «Синтетическая компания» (далее — должник, ИНН 7700000016)"
        )
        noisy = (
            "Дело А40-12345/2026\n"
            "ООО «Синтетическая компация» (далее — должник, ИНН 7700000016)"
        )
        card = parse_entity([agreed, agreed, noisy, "Неразборчивый текст"])
        self.assertEqual(card.entity_kind.value, "legal_entity")
        self.assertEqual(card.entity_kind.source, "debtor_entity_candidate:v1")
        self.assertEqual(card.legal_name.value, "ООО «Синтетическая компания»")
        self.assertEqual(card.inn.value, "7700000016")
        self.assertTrue(card.legal_name.warning)
        self.assertEqual(len(card.review_suggestions), 3)

    def test_entity_inn_cannot_move_from_minority_name_to_majority_name(self) -> None:
        majority = (
            "Дело А40-12345/2026\n"
            "ООО «Учебная компания» (далее — должник)"
        )
        minority = (
            "Дело А40-12345/2026\n"
            "ООО «Учебная компация» (далее — должник, ИНН 7700000016)"
        )
        card = parse_entity([majority, majority, majority, minority, minority])
        self.assertEqual(card.inn.value, "7700000016")
        self.assertEqual(card.legal_name.value, "ООО «Учебная компация»")
        self.assertEqual(card.entity_kind.source, "debtor_entity_candidate:v1")
        self.assertEqual(card.inn.source_variant, card.legal_name.source_variant)
        self.assertTrue(card.legal_name.warning)
        for item in card.review_suggestions:
            fields = item["fields"]
            if fields.get("legal_name", {}).get("value") == "ООО «Учебная компания»":
                self.assertNotIn("inn", fields)

    def test_distinct_noisy_legal_names_share_one_id_only_as_review_occurrence(self) -> None:
        variants = [
            f"Дело А40-12345/2026\nООО «Учебная {suffix}» (далее — должник, ИНН 7700000016)"
            for suffix in ("компания", "кампания", "компанна", "компаниа")
        ] + ["Неразборчивый текст"]
        card = parse_entity(variants)
        self.assertEqual(card.entity_kind.value, "legal_entity")
        self.assertEqual(card.entity_kind.source, "debtor_entity_candidate:v1")
        self.assertEqual(card.inn.value, "7700000016")
        self.assertEqual(
            {card.entity_kind.source_variant, card.legal_name.source_variant, card.inn.source_variant},
            {card.legal_name.source_variant},
        )
        selected = variants[card.legal_name.source_variant]
        self.assertEqual(selected[card.legal_name.source_start:card.legal_name.source_end], card.legal_name.value)
        self.assertEqual(selected[card.inn.source_start:card.inn.source_end], card.inn.value)
        self.assertTrue(card.legal_name.warning)

    def test_different_valid_legal_ids_withhold_noisy_name_candidate(self) -> None:
        first = "ООО «Учебная компания» (далее — должник, ИНН 7700000016)"
        second = "ООО «Учебная компация» (далее — должник, ИНН 7700000023)"
        card = parse_entity([first, first, second, second])
        self.assertIsNone(card.entity_kind.value)
        self.assertIsNone(card.legal_name.value)
        self.assertIsNone(card.inn.value)

    def test_hidden_multi_debtor_variant_blocks_unique_id_cohort(self) -> None:
        first = "Должник: ООО «Учебная компания», ИНН 7700000016"
        noisy = "Должник: ООО «Учебная компация», ИНН 7700000016"
        conflict = (
            first + "\n"
            "Должник: ООО «Другая компания», ИНН 7700000023"
        )
        card = parse_entity([first, noisy, conflict])
        self.assertIsNone(card.entity_kind.value)
        self.assertIsNone(card.legal_name.value)
        self.assertIsNone(card.inn.value)

    def test_claimant_company_is_not_inferred_as_debtor_entity(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "По заявлению ПАО «Учебный заявитель» ИНН 7700000016\n"
            "о признании гражданина Иванова Ивана Ивановича банкротом"
        )
        card = parse_entity([text, text])
        self.assertIsNone(card.entity_kind.value)
        self.assertFalse(card.review_suggestions)

    def test_petitioning_company_and_bankruptcy_subject_keep_separate_roles(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "По заявлению ПАО «Учебный заявитель» (ИНН 7700000016) "
            "о признании гражданина Иванова Ивана Ивановича банкротом"
        )
        card = parse([text, text])
        self.assertEqual(card.fio_raw.value, "Иванова Ивана Ивановича")
        self.assertEqual(card.fio_raw.source, "debtor_role:v1")
        self.assertEqual(card.applicant_evidence["role_source"], "applicant_role:v1")
        self.assertEqual(card.applicant_evidence["name"], "ПАО «Учебный заявитель»")

    def test_bankruptcy_claim_without_predicate_remains_review_only(self) -> None:
        card = parse([
            "Дело А40-12345/2026\n"
            "Заявление о признании гражданина Иванова Ивана Ивановича."
        ])
        self.assertIsNone(card.fio_raw.value)

    def test_conflicting_bankruptcy_subjects_withhold_typed_role(self) -> None:
        first = "Дело А40-12345/2026\nО банкротстве гражданина Иванова Ивана Ивановича."
        second = "Дело А40-12345/2026\nО банкротстве гражданина Петрова Петра Петровича."
        card = parse([first, second])
        self.assertIsNone(card.fio_raw.value)
        self.assertTrue(card.fio_raw.warning)

    def test_genitive_subject_and_nominative_role_share_one_identity(self) -> None:
        variants = [
            "Дело А40-12345/2026\nО банкротстве гражданина Иванова Ивана Ивановича.",
            "Дело А40-12345/2026\nДолжник: Иванов Иван Иванович",
        ]
        card = parse(variants)
        self.assertEqual(card.fio_raw.value, "Иванов Иван Иванович")
        self.assertEqual(card.fio_raw.source, "debtor_role:v1")
        self.assertEqual(card.fio_raw.source_variant, 1)
        self.assertEqual(
            variants[card.fio_raw.source_variant][card.fio_raw.source_start:card.fio_raw.source_end],
            card.fio_raw.value,
        )

    def test_one_noisy_petition_company_is_not_typed_applicant(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "По заявлению ПАО «Учебный заявитель» "
            "о признании гражданина Иванова Ивана Ивановича банкротом"
        )
        self.assertIsNone(parse([text]).applicant_evidence)

    def test_conflicting_entity_identifiers_do_not_get_candidate_field(self) -> None:
        first = "ООО «Учебная компания» (далее — должник), ИНН 7700000016"
        second = "ООО «Учебная компания» (далее — должник), ИНН 7700000017"
        card = parse_entity([first, second, "Неразборчивый текст"])
        self.assertIsNone(card.entity_kind.value)
        self.assertIsNone(card.inn.value)

    def test_applicant_requires_explicit_label_and_matching_case(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Рассмотрев заявление гражданина Петрова Петра Петровича\n"
            "Должник: гражданин Иванов Иван Иванович"
        )
        self.assertIsNone(parse([text]).applicant_evidence)
        self.assertIsNone(parse_entity([
            "Заявитель: ООО «Учебная организация»\nДолжник: ООО «Синтетика»"
        ]).applicant_evidence)

    def test_conflicting_applicants_are_withheld(self) -> None:
        first = "Дело А40-12345/2026\nЗаявитель: ООО «Синтетика-1»\nДолжник: ООО «Должник»"
        second = "Дело А40-12345/2026\nЗаявитель: ООО «Синтетика-2»\nДолжник: ООО «Должник»"
        self.assertIsNone(parse_entity([first, second]).applicant_evidence)

    def test_second_unreadable_applicant_label_blocks_typed_evidence(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Заявитель: ООО «Синтетика»\n"
            "Заявитель: ?\n"
            "Должник: ООО «Должник»"
        )
        self.assertIsNone(parse_entity([text]).applicant_evidence)

    def test_representative_is_not_applicant_label(self) -> None:
        text = (
            "Дело А40-12345/2026\n"
            "Представитель заявителя: Петров Пётр Петрович\n"
            "Должник: гражданин Иванов Иван Иванович"
        )
        self.assertIsNone(parse([text]).applicant_evidence)
