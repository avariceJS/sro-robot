"""Quoted role statements do not establish authoritative debtor identity."""

import unittest

from sro_robot.core.extract import parse


INN = '772881234508'


class QuotedPersonRoleAuthorityTests(unittest.TestCase):
    def test_quoted_role_and_bankruptcy_predicate_supply_no_primary_name_or_id(self):
        statements = (
            f'Должник: Иванов Иван Иванович, ИНН {INN}',
            'О признании гражданина Иванова Ивана Ивановича банкротом',
            f'В отношении гражданина Иванова Ивана Ивановича, ИНН {INN}',
            'Заявление Иванова Ивана Ивановича о признании себя банкротом',
            f'Иванов Иван Иванович (далее — должник), ИНН {INN}',
        )
        for statement in statements:
            with self.subTest(statement=statement):
                card = parse([f'Суд приводит цитату: «{statement}».'])
                self.assertIsNone(card.fio_raw.value)
                self.assertIsNone(card.inn.value)

    def test_actual_role_immediately_after_a_closed_quote_remains_authoritative(self):
        for name in ('Иванов Иван Иванович', 'Иванов(«Петров»)Иван Иванович'):
            with self.subTest(name=name):
                text = f'«Учебная справочная цитата»Должник: {name}, ИНН {INN}.'
                card = parse([text])
                self.assertEqual(card.fio_raw.value, 'Иванов Иван Иванович')
                self.assertEqual(card.fio_raw.source, 'debtor_role:v1')
                self.assertEqual(card.inn.value, INN)
                self.assertEqual(text[card.inn.source_start:card.inn.source_end], INN)

    def test_contradictory_quoted_actor_cannot_displace_the_actual_debtor(self):
        for quote in ('Должник: Иванов Петр Петрович, ИНН 500100001146',
                      'О признании гражданина Иванова Петра Петровича банкротом'):
            with self.subTest(quote=quote):
                text = f'«{quote}»\nДолжник: Иванов Иван Иванович, ИНН {INN}.'
                card = parse([text])
                self.assertEqual(card.fio_raw.value, 'Иванов Иван Иванович')
                self.assertEqual(card.fio_raw.source, 'debtor_role:v1')
                self.assertEqual(card.inn.value, INN)
                self.assertEqual(card.fio_raw.source_start, text.rindex('Иванов Иван Иванович'))


if __name__ == '__main__':
    unittest.main()
