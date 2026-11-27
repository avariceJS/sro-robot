"""Synthetic address boundaries through the product's OCR-to-card entry point."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sro_robot.core.extract import parse_file


BASE = 'г. Учебный, ул. Макетная, д. 7'


class AddressProductBoundaryTests(unittest.TestCase):
    def parse_text(self, text):
        # Only the OCR result is supplied; routing, ownership, field voting and
        # source attachment execute through the maintained product entry point.
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.pdf'
            path.write_bytes(b'synthetic OCR boundary test placeholder')
            with patch('sro_robot.core.ocr.recognize', return_value=[text]):
                return parse_file(path)

    def assert_source(self, text, field):
        literal = text[field.source_start:field.source_end]
        self.assertEqual(' '.join(literal.split()), field.value)

    def test_explicit_apartment_colon_and_separate_phone_keep_full_owned_address(self):
        for suffix, expected in (
            ('кв.: 42', 'кв.: 42'), ('кв. : 42/2а', 'кв. : 42/2а'),
            ('кв.\n42\nтелефон: +7 (000) 000-00-00', 'кв. 42'),
            ('кв.:\n42\nтел.: +7 (000) 000-00-00', 'кв.: 42'),
            ('кв.\n42\nТелефон 0000000', 'кв. 42'),
        ):
            with self.subTest(suffix=suffix):
                text = f'Должник: Иванов Иван Иванович\nАдрес регистрации: {BASE}, {suffix}'
                card = self.parse_text(text)
                self.assertEqual(card.address.value, f'{BASE}, {expected}')
                self.assertEqual(card.address_kind.value, 'registration')
                self.assertIsNone(card.address.warning)
                self.assert_source(text, card.address)

    def test_review_candidate_keeps_full_address_copyability_and_exact_span(self):
        for suffix in ('кв.: 42', 'кв.\n42\nтелефон: +7 (000) 000-00-00'):
            with self.subTest(suffix=suffix):
                text = ('Рассмотрев заявление гражданина Иванова Ивана Ивановича; '
                        f'Адрес регистрации: {BASE}, {suffix}')
                card = self.parse_text(text)
                self.assertIsNone(card.address.value)
                item = next(item for item in card.review_suggestions if item['kind'] == 'application_person')
                field = item['fields']['address']
                expected = f'{BASE}, ' + ('кв.: 42' if suffix.startswith('кв.:') else 'кв. 42')
                self.assertEqual(field['value'], expected)
                self.assertIn('address', item['copyable_fields'])
                self.assertEqual(' '.join(text[field['source_start']:field['source_end']].split()), expected)

    def test_phone_exception_cannot_complete_other_units_or_numbered_prose(self):
        for suffix in ('кв.\n42. Обязать заявителя представить документы.',
                       'кв.\n42\nтелефон: неизвестен', 'офис\n42\nтелефон: +7 0000000',
                       'корп.\n42\nтелефон: +7 0000000'):
            with self.subTest(suffix=suffix):
                text = f'Должник: Иванов Иван Иванович\nАдрес регистрации: {BASE}, {suffix}'
                card = self.parse_text(text)
                self.assertEqual(card.address.value, BASE)
                self.assertIsNotNone(card.address.warning)

    def test_creditor_contact_cannot_supply_an_apartment(self):
        text = (f'Должник: Иванов Иван Иванович\nАдрес регистрации: {BASE}\n'
                'Контакт кредитора: кв.: 42, телефон: +7 0000000')
        card = self.parse_text(text)
        self.assertEqual(card.address.value, BASE)
        self.assert_source(text, card.address)

    def test_household_and_office_inline_values_remain_literal(self):
        for address in (BASE + '/2, кв. 42', BASE + ', офис 42'):
            with self.subTest(address=address):
                text = f'Должник: Иванов Иван Иванович\nАдрес регистрации: {address}'
                card = self.parse_text(text)
                self.assertEqual(card.address.value, address)
                self.assert_source(text, card.address)

    def test_second_location_clause_does_not_extend_registration_to_another_address(self):
        for label in ('место проживания:', 'фактически проживает:',
                      'Адрес для корреспонденции:', 'почтовый адрес:'):
            with self.subTest(label=label):
                text = ('Должник: Иванов Иван Иванович\n'
                        f'Адрес регистрации: г. Учебный; {label} '
                        'г. Макетный, ул. Иная, д. 8, кв. 42.')
                card = self.parse_text(text)
                self.assertIsNone(card.address.value)
                self.assertIn('Несколько адресов', card.address.warning)
                self.assertIsNone(card.address.source_start)

    def test_phone_cannot_bridge_locality_to_a_later_street_or_house(self):
        for prefix in ('Должник: Иванов Иван Иванович\n',
                       'Рассмотрев заявление гражданина Иванова Ивана Ивановича; '):
            with self.subTest(prefix=prefix):
                text = (prefix + 'Адрес регистрации: г. Учебный, '
                        'телефон: +7 (000) 000-00-00; ул. Макетная, д. 7.')
                card = self.parse_text(text)
                self.assertIsNone(card.address.value)
                for item in card.review_suggestions:
                    address = item.get('fields', {}).get('address')
                    if address:
                        self.assertNotIn('телефон', address['value'])
                        self.assertNotIn('д. 7', address['value'])
                        self.assertNotIn('address', item['copyable_fields'])

    def test_locality_only_and_known_distinct_address_labels_stay_unresolved(self):
        for biography in ('Адрес регистрации: г. Учебный.',
                          f'Адрес регистрации: {BASE}; Адрес проживания: г. Макетный, ул. Иная, д. 8.'):
            with self.subTest(biography=biography):
                text = 'Должник: Иванов Иван Иванович\n' + biography
                card = self.parse_text(text)
                self.assertIsNone(card.address.value)
                self.assertIsNotNone(card.address.warning)


if __name__ == '__main__':
    unittest.main()
