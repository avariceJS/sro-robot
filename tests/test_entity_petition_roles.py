"""Synthetic entity identity is distinct from the petition's procedural stage."""

import unittest

from sro_robot.core.entity_extract import has_explicit_entity_marker, parse_entity


class EntityPetitionRoleTests(unittest.TestCase):
    def test_explicit_debtor_identity_does_not_require_a_final_bankruptcy_act(self):
        for purpose in ("о принятии заявления", "о назначении рассмотрения заявления",
                        "о признании должника банкротом"):
            for debtor, target, name, inn in (
                ('ООО «Учебное предприятие»', 'legal_name', 'ООО «Учебное предприятие»', '7700000016'),
                ('ИП Иванов Иван Иванович', 'debtor_name', 'Иванов Иван Иванович', '500100001146'),
            ):
                with self.subTest(purpose=purpose, target=target):
                    text = f'ОПРЕДЕЛЕНИЕ\n{purpose}\nДолжник: {debtor} ИНН {inn}'
                    card = parse_entity([text])
                    field = getattr(card, target)
                    self.assertEqual(field.value, name)
                    self.assertEqual(card.inn.value, inn)
                    self.assertEqual(text[field.source_start:field.source_end], name)

    def test_petition_purpose_without_owned_target_cannot_establish_debtor(self):
        for party in ('ООО «Учебное предприятие»', 'ИП Иванов Иван Иванович'):
            with self.subTest(party=party):
                text = ('ОПРЕДЕЛЕНИЕ о принятии заявления\n'
                        f'Рассмотрев заявление {party} о признании себя банкротом,\n'
                        'ОПРЕДЕЛИЛ:\nПринять заявление к производству.')
                self.assertFalse(has_explicit_entity_marker(text))
                card = parse_entity([text])
                self.assertIsNone(card.legal_name.value)
                self.assertIsNone(card.debtor_name.value)

    def test_missing_ip_identity_cannot_take_a_later_narrative_person(self):
        text = ('ОПРЕДЕЛЕНИЕ о принятии заявления\n'
                'Должник: ИП (имя не приведено). В деле упоминается '
                'Иванов Иван Иванович ИНН 500100001146.')
        card = parse_entity([text])
        self.assertIsNone(card.debtor_name.value)
        self.assertIsNone(card.inn.value)
        self.assertFalse(any('debtor_name' in item.get('fields', {})
                             for item in card.review_suggestions))

    def test_immediate_ip_identity_keeps_separators_and_role_owned_identifier(self):
        for separator in (': ', '— ', '– ', '- ', ''):
            with self.subTest(separator=separator):
                text = ('ОПРЕДЕЛЕНИЕ о назначении рассмотрения заявления\n'
                        f'Должник: ИП {separator}Иванов Иван Иванович ИНН 500100001146\n'
                        'Кредитор: ИП Петров Петр Петрович ИНН 772881234508')
                card = parse_entity([text])
                self.assertEqual(card.debtor_name.value, 'Иванов Иван Иванович')
                self.assertEqual(card.inn.value, '500100001146')
                field = card.debtor_name
                self.assertEqual(text[field.source_start:field.source_end], field.value)


if __name__ == '__main__':
    unittest.main()
