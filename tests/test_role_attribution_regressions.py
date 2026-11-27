"""Synthetic role and field boundaries for multi-page parse and office review."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.services.fields import review_fields
from sro_robot.core.entity_extract import isolated_review_candidates, parse_entity
from sro_robot.core.extract import parse, parse_file, parse_variants_for_review
from sro_robot.core.models import card_to_fields


CAPTION = (
    "Арбитражный суд Учебной области\nРЕШЕНИЕ\n"
    "Дело № А99-1234/2099\n18.12.2099\n"
)
NAME = "Примерова Елена Максовна"
DEBTOR_INN = "990014232035"
MANAGER_INN = "990004321007"
MANAGER_PAGE = (
    f"ИНН: {MANAGER_INN}\nСНИЛС: 112-233-445 95\n"
    "Адрес регистрации: г. Учебный, ул. Макетная, д. 8\n"
    "Финансовый управляющий приступает к исполнению обязанностей."
)
COMPANY = 'ООО «Учебное предприятие»'
COMPANY_INN = "9900142325"
COMPANY_OGRN = "1999900000010"
OTHER_COMPANY_PAGE = (
    "ИНН: 9900243122, ОГРН: 1999900000021, КПП: 999901999\n"
    "Адрес регистрации: г. Учебный, ул. Макетная, д. 12\n"
    "Конкурсный управляющий приступает к исполнению обязанностей."
)


def office_fields(card):
    task = SimpleNamespace(scenario="person_card", recognized=card_to_fields(card), values={})
    return {field.attr: field for field in review_fields(task)}


class RoleAttributionRegressions(unittest.TestCase):
    def test_prior_page_practitioner_does_not_obscure_explicit_debtor(self):
        text = CAPTION + f"Финансовый управляющий\fДолжник: {NAME}, ИНН: {DEBTOR_INN}"

        card = parse([text])

        self.assertEqual(card.inn.value, DEBTOR_INN)
        self.assertEqual(card.inn.source_page, 2)

    def test_judge_or_certificate_closes_requisites_on_same_page(self):
        for boundary in ("Судья А.Б. Макетов", "Подписано электронной подписью."):
            with self.subTest(boundary=boundary):
                card = parse([CAPTION + f"Должник: {NAME}\n{boundary}\nИНН: {MANAGER_INN}"])

                self.assertEqual(card.fio_raw.value, NAME)
                self.assertIsNone(card.inn.value)

    def test_named_practitioner_requisites_are_withheld_even_if_repeated_under_debtor(self):
        appointment = ("РЕШИЛ:\n2. Утвердить финансовым управляющим Макетова Марка Львовича "
                       f"(ИНН {MANAGER_INN}, СНИЛС 112-233-445 95).")
        for debtor_inn in (MANAGER_INN, DEBTOR_INN):
            text = (CAPTION + f"рассмотрев заявление Примеровой Елены Максовны (ИНН {debtor_inn}).\n"
                    f"Должник: {NAME} (ИНН {debtor_inn}, СНИЛС 112-233-445 95).\f{appointment}")
            with self.subTest(debtor_inn=debtor_inn):
                card = parse([text])

                self.assertEqual(card.fio_raw.value, NAME)
                self.assertIsNotNone(card.snils.warning)
                self.assertIsNone(card.snils.value)
                self.assertNotEqual(office_fields(card)["inn"].value, MANAGER_INN)
                self.assertEqual(card.inn.value, None if debtor_inn == MANAGER_INN else DEBTOR_INN)
                for occurrence in card.review_suggestions:
                    self.assertNotEqual(occurrence["fields"].get("inn", {}).get("value"), MANAGER_INN)

    def test_practitioner_role_order_and_mixed_case_ocr_labels(self):
        person = f"Макетова Марка Львовича (Инн: {MANAGER_INN})"
        debtor = CAPTION + f"Должник: {NAME} (ИНН {MANAGER_INN}).\n"
        for clause in (f"Утвердить конкурсным управляющим {person}.",
                       f"Утвердить {person} конкурсным управляющим."):
            with self.subTest(clause=clause):
                self.assertIsNone(parse([debtor + "\f" + clause]).inn.value)
                self.assertEqual(parse([debtor + "\fЦитата: «\n" + clause + "\n»"]).inn.value, MANAGER_INN)

    def test_only_self_reference_makes_the_applicant_the_debtor(self):
        for pronoun, is_self in (("себя", True), ("его", False), ("её", False), ("ее", False)):
            with self.subTest(pronoun=pronoun):
                card = parse([CAPTION + f"заявление Примеровой Елены Максовны о признании {pronoun} банкротом."])

                self.assertEqual(bool(card.fio_raw.value), is_self)

    def test_later_page_does_not_supply_unbound_debtor_attributes(self):
        text = CAPTION + f"Должник: {NAME}\f" + MANAGER_PAGE

        card = parse([text])

        self.assertEqual(card.fio_raw.value, NAME)
        self.assertEqual(card.fio_raw.source_page, 1)
        for attr in ("inn", "snils", "address", "address_kind"):
            with self.subTest(attr=attr):
                self.assertIsNone(getattr(card, attr).value)

    def test_raster_worker_entrypoint_and_office_form_do_not_inherit_later_page_inn(self):
        text = CAPTION + f"Должник: {NAME}\f" + MANAGER_PAGE

        def two_ocr_pages(_path, *, budget, **_kwargs):
            budget.ocr_pages.update({1, 2})
            return [text]

        with patch("sro_robot.core.ocr.recognize", side_effect=two_ocr_pages):
            card = parse_file("synthetic-two-page-scan.pdf")

        self.assertEqual(card.fio_raw.source, "role_partial")
        fields = office_fields(card)
        self.assertEqual(fields["first_name"].value, "Елена")
        for attr in ("inn", "snils", "address"):
            with self.subTest(attr=attr):
                self.assertEqual(fields[attr].value, "")
                self.assertIsNone(fields[attr].source_page)

    def test_repeated_debtor_with_attributes_preserves_same_page_evidence(self):
        text = CAPTION + (
            f"Должник: {NAME}\nДолжник: {NAME}, ИНН: {DEBTOR_INN}\f" + MANAGER_PAGE
        )

        card = parse([text])

        self.assertEqual(card.fio_raw.value, NAME)
        self.assertEqual(card.inn.value, DEBTOR_INN)
        self.assertEqual(card.inn.source, "debtor_role:v1")
        self.assertEqual(card.inn.source_page, 1)
        self.assertEqual(text[card.inn.source_start:card.inn.source_end], DEBTOR_INN)

    def test_debtor_label_or_name_cannot_join_across_the_page_break(self):
        for split in (f"Должник:\f{NAME}", "Должник: Примерова\fЕлена Максовна",
                      f"{NAME}\f(далее — должник)"):
            with self.subTest(split=split):
                card = parse([CAPTION + split + f", ИНН: {MANAGER_INN}"])

                self.assertIsNone(card.fio_raw.value)
                self.assertIsNone(card.inn.value)

    def test_same_page_judicial_signatory_cannot_supply_debtor_inn(self):
        sections = ("Судья А.Б. Макетов", "Подписано электронной подписью.")
        for section in sections:
            with self.subTest(section=section):
                card = parse([CAPTION + f"Должник: {NAME}\n{section}\nИНН: {MANAGER_INN}"])

                self.assertEqual(card.fio_raw.value, NAME)
                self.assertIsNone(card.inn.value)

    def test_review_candidate_never_copies_other_page_identifier(self):
        for context in ("рассмотрев заявление Примеровой Елены Максовны",
                        f"Физическое лицо: {NAME}"):
            with self.subTest(context=context):
                card = parse([CAPTION + context + "\f" + MANAGER_PAGE])

                self.assertTrue(card.review_suggestions)
                for occurrence in card.review_suggestions:
                    for attr in ("inn", "snils", "address", "address_kind"):
                        self.assertNotIn(attr, occurrence["fields"])
                        self.assertNotIn(attr, occurrence["copyable_fields"])
                self.assertEqual(office_fields(card)["inn"].value, "")


class EntityPageAttributionRegressions(unittest.TestCase):
    def test_court_signature_closes_company_registry_section(self):
        text = CAPTION + f"Должник: {COMPANY}\nСудья А.Б. Макетов\nИНН: {COMPANY_INN}"

        card = parse_entity([text])

        self.assertEqual(card.legal_name.value, COMPANY)
        self.assertIsNone(card.inn.value)

    def test_other_page_cannot_supply_company_or_entrepreneur_identifiers(self):
        subjects = (
            (COMPANY, "legal_entity", "legal_name", OTHER_COMPANY_PAGE),
            (f"ИП {NAME}", "sole_proprietor", "debtor_name", MANAGER_PAGE),
        )
        for subject, kind, field, following_page in subjects:
            with self.subTest(kind=kind):
                card = parse_variants_for_review([CAPTION + f"Должник: {subject}\f" + following_page])

                self.assertEqual(card.entity_kind.value, kind)
                self.assertEqual(getattr(card, field).source_page, 1)
                self.assertIsNone(card.inn.value)
                self.assertIsNone(card.ogrn.value)
                self.assertIsNone(card.address.value)
                self.assertTrue(card.review_suggestions)
                for occurrence in card.review_suggestions:
                    self.assertNotIn("inn", occurrence["fields"])

    def test_same_page_registry_values_survive_conflicting_later_page(self):
        text = CAPTION + (
            f"Должник: {COMPANY}\nИНН: {COMPANY_INN}\nОГРН: {COMPANY_OGRN}\f"
            + OTHER_COMPANY_PAGE
        )

        card = parse_entity([text])

        self.assertEqual(card.legal_name.value, COMPANY)
        for field, expected in ((card.inn, COMPANY_INN), (card.ogrn, COMPANY_OGRN)):
            self.assertEqual(field.value, expected)
            self.assertEqual(field.source, "debtor_entity_role:v1")
            self.assertEqual(field.source_page, 1)
            self.assertEqual(text[field.source_start:field.source_end].strip(), expected)
        self.assertIsNone(card.kpp.value)
        self.assertIsNone(card.address.value)

    def test_civil_organization_preserves_role_without_later_page_fields(self):
        text = CAPTION + f"Ответчик: {COMPANY}\f" + OTHER_COMPANY_PAGE

        card = parse_variants_for_review([text])

        self.assertIsNone(card.legal_name.value)
        participants = [item for item in card.review_suggestions if item["kind"] == "entity_participant"]
        self.assertEqual(len(participants), 1)
        item = participants[0]
        self.assertEqual(item["role"], "defendant")
        self.assertEqual(item["fields"]["legal_name"]["value"], COMPANY)
        for attr in ("inn", "ogrn", "kpp", "address"):
            self.assertNotIn(attr, item["fields"])
            self.assertNotIn(attr, item["copyable_fields"])

    def test_entity_role_and_organization_cannot_join_across_page_break(self):
        fragments = (f"Должник:\f{COMPANY}", f"{COMPANY}\f(далее — должник)")
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                card = parse_entity([CAPTION + fragment + f" ИНН: {COMPANY_INN}"])

                self.assertIsNone(card.entity_kind.value)
                self.assertIsNone(card.inn.value)

    def test_completion_clause_cannot_borrow_registry_block_from_next_page(self):
        text = (
            f"ОПРЕДЕЛЕНИЕ\nОПРЕДЕЛИЛ:\nЗавершить конкурсное производство в отношении {COMPANY}"
            f"\f(ИНН {COMPANY_INN}, ОГРН {COMPANY_OGRN})."
        )

        self.assertEqual(isolated_review_candidates([text]), [])
        self.assertEqual(len(isolated_review_candidates([text.replace("\f", " ")])), 1)


if __name__ == "__main__":
    unittest.main()
