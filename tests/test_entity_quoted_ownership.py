from __future__ import annotations

import unittest

from sro_robot.core.entity_extract import detect_entity_kind, parse_entity


class QuotedEntityOwnershipTests(unittest.TestCase):
    def test_wrapped_quoted_title_ends_before_following_prose(self):
        for opening, closing in (("«", "»"), ('"', '"'), ("„", "“"), ("“", "”"), ("‘", "’")):
            for suffix in ("", " признано несостоятельным (банкротом)."):
                with self.subTest(quotes=opening, prose=bool(suffix)):
                    literal = f"ООО {opening}Учебная\nорганизация{closing}"
                    text = "Должник: " + literal + suffix
                    card = parse_entity([text])
                    self.assertEqual(card.legal_name.value, literal.replace("\n", " "))
                    self.assertEqual(text[card.legal_name.source_start:card.legal_name.source_end], literal)
                    self.assertIsNone(card.inn.value)

    def test_role_words_inside_a_title_do_not_cut_its_own_requisites(self):
        for title in ("Управляющая компания Учебная", "Учебный кредитор",
                      "Учебный должник: ООО Пример"):
            with self.subTest(title=title):
                name = f"ООО «{title}»"
                text = (f"Должник: {name} ИНН 7700000016\n"
                        "Кредитор: ООО «Другой участник» ИНН 7700000023")
                card = parse_entity([text])
                self.assertEqual(detect_entity_kind(text), "legal_entity")
                self.assertEqual(card.legal_name.value, name)
                self.assertEqual(card.inn.value, "7700000016")
                self.assertEqual(text[card.inn.source_start:card.inn.source_end], "7700000016")

    def test_quoted_requisite_labels_are_title_content(self):
        for title in ("Учебный ИНН центр", "Учебный в лице центра",
                      "Учебное сокращенное наименование: Макет",
                      "Учебный Адрес: Макетный"):
            with self.subTest(title=title):
                name = f"ООО «{title}»"
                card = parse_entity([f"Должник: {name} ИНН 7700000016"])
                self.assertEqual(card.legal_name.value, name)
                self.assertEqual(card.inn.value, "7700000016")
                self.assertIsNone(card.address.value)
                self.assertIsNone(card.short_name.value)

    def test_identifier_quoted_in_title_cannot_supply_or_conflict_with_own_id(self):
        name = 'ООО «Учебный ИНН 7700000016 ОГРН 1027700132195»'
        for suffix in ("", " ИНН 7700000023"):
            with self.subTest(own_id=bool(suffix)):
                text = "Должник: " + name + suffix
                card = parse_entity([text])
                self.assertEqual(card.legal_name.value, name)
                self.assertEqual(card.inn.value, "7700000023" if suffix else None)
                self.assertIsNone(card.ogrn.value)
                if suffix:
                    self.assertEqual(card.inn.source_start, text.rindex("7700000023"))
                else:
                    self.assertIn("не указан", card.inn.warning)

    def test_quoted_reverse_role_does_not_establish_debtor_ownership(self):
        text = 'ООО «Учебное (далее — должник) предприятие» ИНН 7700000016'
        self.assertIsNone(detect_entity_kind(text))
        card = parse_entity([text])
        self.assertIsNone(card.legal_name.value)
        self.assertIsNone(card.inn.value)

    def test_real_boundaries_and_page_break_remain_ownership_stops(self):
        for separator in ("\nКредитор: ", "\f", "\nСудья "):
            with self.subTest(separator=separator):
                text = ('Должник: ООО «Управляющая компания Учебная»' + separator
                        + 'ООО «Другой участник» ИНН 7700000023')
                card = parse_entity([text])
                self.assertEqual(card.legal_name.value, 'ООО «Управляющая компания Учебная»')
                self.assertIsNone(card.inn.value)

    def test_quote_state_does_not_cross_a_physical_page_boundary(self):
        text = ('Синтетический незакрытый заголовок «\f'
                'Должник: ООО «Учебная организация» ИНН 7700000016')
        card = parse_entity([text])
        self.assertEqual(card.legal_name.value, 'ООО «Учебная организация»')
        self.assertEqual(card.inn.value, '7700000016')
        self.assertEqual(card.legal_name.source_page, 2)

    def test_typographic_quotes_keep_literal_ids_and_nested_roles_in_the_title(self):
        for opening, closing, inner_open, inner_close in (
            ("„", "“", "«", "»"), ("“", "”", "‘", "’"), ("‘", "’", "„", "“"),
        ):
            with self.subTest(quotes=opening):
                name = (f'ООО {opening}Учебный ИНН 7700000016 '
                        f'Кредитор: ООО {inner_open}Поставщик{inner_close}{closing}')
                text = f'Должник: {name} ИНН 7700000023'
                card = parse_entity([text])
                self.assertEqual(card.legal_name.value, name)
                self.assertEqual(card.inn.value, '7700000023')
                self.assertEqual(text[card.legal_name.source_start:card.legal_name.source_end], name)
                self.assertEqual(card.inn.source_start, text.rindex('7700000023'))
                self.assertFalse(any(item.get('kind') == 'entity_participant'
                                     for item in card.review_suggestions))

    def test_reverse_roles_bind_typographically_quoted_outer_names(self):
        for opening, closing in (("„", "“"), ("“", "”"), ("‘", "’")):
            with self.subTest(quotes=opening):
                name = f'ООО {opening}Учебный ИНН центр ООО «Макет»{closing}'
                text = f'{name} (далее — должник) ИНН 7700000023'
                card = parse_entity([text])
                self.assertEqual(card.legal_name.value, name)
                self.assertEqual(card.inn.value, '7700000023')


if __name__ == "__main__":
    unittest.main()
