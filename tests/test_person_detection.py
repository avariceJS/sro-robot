"""Regression checks for review-only generic personal-detail detection.

All values and document fragments in this module are synthetic.  A generic
match must remain a review suggestion and must never populate debtor fields.
"""

from __future__ import annotations

import unittest

from sro_robot.core.extract import parse


INN12 = "772881234508"  # checksum-valid synthetic personal INN
SNILS = "112-233-445 95"  # checksum-valid synthetic SNILS


def suggestions(text: str) -> list[dict]:
    return [
        item for item in parse([text]).review_suggestions
        if item.get("kind") == "application_person"
    ]


class GenericPersonDetectionTests(unittest.TestCase):
    def test_arbitrary_prose_finds_inflected_name_and_unlabeled_identifiers(self) -> None:
        text = (
            "В синтетическом описании упомянута гражданка Ивановой Анны Сергеевны; "
            f"личные номера {INN12} и {SNILS}."
        )

        card = parse([text])
        found = suggestions(text)

        self.assertEqual(len(found), 1)
        fields = found[0]["fields"]
        self.assertEqual(fields["fio"]["value"], "Ивановой Анны Сергеевны")
        self.assertEqual(fields["inn"]["value"], INN12)
        self.assertEqual(fields["snils"]["value"], SNILS)
        self.assertIsNone(card.fio_raw.value)
        self.assertIsNone(card.inn.value)
        self.assertIsNone(card.snils.value)
        self.assertIsNone(card.address.value)

    def test_nominative_female_name_is_not_masculinized(self) -> None:
        text = f"Сведения о гражданке Иванова Анна Сергеевна; номер {INN12}."

        found = suggestions(text)

        self.assertEqual(len(found), 1)
        fields = found[0]["fields"]
        # Preserve the source form for review; normalized component fields
        # should retain the explicitly feminine surname.
        self.assertEqual(fields["fio"]["value"], "Иванова Анна Сергеевна")
        if "last_name" in fields:
            self.assertEqual(fields["last_name"]["value"], "Иванова")

    def test_oblique_female_and_male_names_normalize_for_review(self) -> None:
        cases = (
            ("Ивановой Марии Сергеевне", "Иванова", "Мария", "Сергеевна"),
            ("Иванову Ивану Ивановичу", "Иванов", "Иван", "Иванович"),
        )
        for source_name, surname, given, patronymic in cases:
            with self.subTest(source_name=source_name):
                text = f"Относительно гражданина {source_name}, номер {INN12}."
                found = suggestions(text)
                self.assertEqual(len(found), 1)
                fields = found[0]["fields"]
                self.assertEqual(fields["fio"]["value"], source_name)
                self.assertEqual(fields["last_name"]["value"], surname)
                self.assertEqual(fields["first_name"]["value"], given)
                self.assertEqual(fields["middle_name"]["value"], patronymic)

    def test_full_synthetic_address_includes_apartment_one(self) -> None:
        text = (
            "Гражданка Ивановой Анны Сергеевны, адрес проживания: "
            "123456, вымышленный город Учебный, улица Макетная, дом 7, квартира 1."
        )

        found = suggestions(text)

        self.assertEqual(len(found), 1)
        address = found[0]["fields"]["address"]["value"]
        self.assertIn("город Учебный", address)
        self.assertIn("улица Макетная", address)
        self.assertIn("дом 7", address)
        self.assertRegex(address, r"(?:квартира|кв\.?)[ ,]*1\b")
        self.assertIsNone(parse([text]).address.value)

    def test_two_people_keep_their_own_nearby_identifiers_and_address(self) -> None:
        text = (
            "Гражданка Ивановой Анны Сергеевны, номер " + INN12 + ".\n"
            "Гражданин Сидорова Петра Ивановича, номер 500100732259, "
            "адрес: 654321, вымышленный город Тестовый, улица Вторая, дом 9, кв. 2."
        )

        card = parse([text])
        found = suggestions(text)

        self.assertEqual(len(found), 2)
        by_name = {item["fields"]["fio"]["value"]: item["fields"] for item in found}
        first = next(fields for name, fields in by_name.items() if "Ивановой Анны" in name)
        second = next(fields for name, fields in by_name.items() if "Сидорова Петра" in name)
        self.assertEqual(first["inn"]["value"], INN12)
        self.assertNotIn("address", first)
        self.assertEqual(second["inn"]["value"], "500100732259")
        self.assertIn("кв. 2", second["address"]["value"])
        self.assertIsNone(card.fio_raw.value)
        self.assertIsNone(card.inn.value)
        self.assertIsNone(card.address.value)

    def test_source_spans_point_to_original_text_after_name_normalization(self) -> None:
        text = f"Запись: Ивановой Анны Сергеевны; номер {INN12}."

        found = suggestions(text)

        self.assertEqual(len(found), 1)
        fields = found[0]["fields"]
        for name in ("fio", "inn"):
            field = fields[name]
            start, end = field["source_start"], field["source_end"]
            self.assertIsInstance(start, int)
            self.assertIsInstance(end, int)
            self.assertLessEqual(0, start)
            self.assertLess(start, end)
            source = text[start:end]
            if name == "fio":
                self.assertEqual(source, "Ивановой Анны Сергеевны")
            else:
                self.assertEqual(source, INN12)
            self.assertIn(source, field["source_excerpt"])

    def test_unlabeled_checksum_invalid_person_identifiers_are_not_candidates(self) -> None:
        text = "Синтетические записи 772881234509 и 112-233-445 96."

        self.assertEqual(suggestions(text), [])
        card = parse([text])
        self.assertIsNone(card.inn.value)
        self.assertIsNone(card.snils.value)

    def test_oversized_name_token_is_ignored_without_becoming_a_candidate(self) -> None:
        oversized_surname = "С" + "и" * 49
        text = f"Гражданка {oversized_surname} Анна Сергеевна, номер {INN12}."

        card = parse([text])

        self.assertEqual(suggestions(text), [])
        self.assertIsNone(card.fio_raw.value)
        self.assertIsNone(card.inn.value)

    def test_court_heading_birthplace_and_company_inn_do_not_create_person(self) -> None:
        samples = (
            "Судья Иванов Иван Иванович рассмотрел дело № А99-12345/2099.",
            "Судебная коллегия установила обстоятельства; номер дела А99-12345/2099.",
            "Место рождения: Иванов Иван Иванович; запись составлена условно.",
            "ООО Учебная компания, ИНН 7701234567, место нахождения: Учебный город.",
        )
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(suggestions(text), [])

    def test_existing_debtor_role_path_remains_authoritative_and_unchanged(self) -> None:
        text = f"Должник: Иванова Анна Сергеевна, ИНН {INN12}"

        card = parse([text])

        self.assertEqual(card.fio_raw.value, "Иванова Анна Сергеевна")
        self.assertEqual(card.fio_raw.source, "debtor_role:v1")
        self.assertEqual(card.inn.value, INN12)
        self.assertEqual(card.inn.source, "debtor_role:v1")
        self.assertEqual(suggestions(text), [])

    def test_generic_candidate_scan_is_bounded_and_reports_incompleteness(self) -> None:
        names = (
            "Ивановой Анны Сергеевны", "Петровой Марии Ивановны",
            "Сидорова Петра Ивановича", "Кузнецовой Елены Викторовны",
            "Васильева Павла Андреевича", "Смирновой Ольги Петровны",
            "Орлова Ивана Сергеевича", "Поповой Ирины Алексеевны",
            "Морозовой Натальи Петровны", "Волкова Дмитрия Сергеевича",
            "Новиковой Татьяны Андреевны", "Федорова Михаила Ивановича",
        )
        fragments = [f"Гражданин {name}, номер {INN12}." for name in names]
        text = "\n".join(fragments)

        card = parse([text])
        found = suggestions(text)

        self.assertLessEqual(len(found), 8)
        self.assertFalse(card.review_suggestions_complete)
        self.assertIsNone(card.fio_raw.value)
        self.assertIsNone(card.inn.value)

    def test_long_unstructured_text_hits_the_document_scan_bound(self) -> None:
        text = "учебный фрагмент " * 2_500

        card = parse([text])

        self.assertEqual(suggestions(text), [])
        self.assertFalse(card.review_suggestions_complete)


if __name__ == "__main__":
    unittest.main()
