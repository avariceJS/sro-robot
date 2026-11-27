"""Synthetic debtor address ownership stops before the court's own sections."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sro_robot.core.extract import parse_file
from sro_robot.platform.firm_address import address_field_values


ADDRESS = '123456, Учебная область, г. Макетный, ул. Тестовая, д. 7, корп. 2, кв. 42'
SUBJECTS = ('ООО «Учебная организация»', 'ИП Иванов Иван Иванович')


class EntityAddressSectionScopeTests(unittest.TestCase):
    def parse_text(self, text):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.pdf'
            path.write_bytes(b'synthetic entity section test placeholder')
            with patch('sro_robot.core.ocr.recognize', return_value=[text]):
                return parse_file(path)

    def assert_address_source(self, text, card):
        field = card.address
        self.assertEqual(field.value, ADDRESS)
        self.assertEqual(text[field.source_start:field.source_end], ADDRESS)
        self.assertIsNone(field.warning)

    def test_early_act_headings_do_not_enter_legal_or_ip_registry_address_fields(self):
        for subject in SUBJECTS:
            for heading in ('УСТАНОВИЛ:', 'ОПРЕДЕЛИЛ:', 'Р Е Ш И Л:', 'П О С Т А Н О В И Л А:'):
                with self.subTest(subject=subject, heading=heading):
                    text = ('ОПРЕДЕЛЕНИЕ\nо принятии заявления\n'
                            f'Должник: {subject}\nАдрес регистрации: {ADDRESS}\n'
                            f'{heading}\nПринять заявление к производству.')
                    card = self.parse_text(text)
                    self.assert_address_source(text, card)
                    self.assertEqual(address_field_values(card.address.value), {
                        'firm_addr_index': '123456', 'firm_addr_area': 'Учебная область',
                        'firm_addr_city': 'г. Макетный', 'firm_addr_street': 'ул. Тестовая',
                        'firm_addr_build': 'д. 7', 'firm_addr_box': 'корп. 2', 'firm_addr_room': 'кв. 42',
                    })

    def test_later_court_body_fields_cannot_supply_debtor_requisites(self):
        for subject in SUBJECTS:
            with self.subTest(subject=subject):
                text = (f'Должник: {subject}\nАдрес регистрации: {ADDRESS}\n'
                        'ОПРЕДЕЛИЛ: Назначить рассмотрение заявления.\n'
                        'ИНН 500100001146\nАдрес: г. Другой, ул. Иная, д. 8')
                card = self.parse_text(text)
                self.assert_address_source(text, card)
                self.assertIsNone(card.inn.value)

    def test_legitimate_inline_address_and_quoted_heading_keep_exact_source(self):
        for subject in (*SUBJECTS, 'ООО «Учебный\nОПРЕДЕЛИЛ: Макет»'):
            with self.subTest(subject=subject):
                text = f'Должник: {subject}\nАдрес регистрации: {ADDRESS}'
                card = self.parse_text(text)
                self.assert_address_source(text, card)

    def test_multiple_addresses_inside_one_debtor_section_remain_unresolved(self):
        for subject in SUBJECTS:
            with self.subTest(subject=subject):
                text = (f'Должник: {subject}\nАдрес регистрации: {ADDRESS}\n'
                        'Фактический адрес: г. Другой, ул. Иная, д. 8\n'
                        'ОПРЕДЕЛИЛ:\nНазначить рассмотрение заявления.')
                card = self.parse_text(text)
                self.assertIsNone(card.address.value)
                self.assertIn('несколько адресов', card.address.warning)

    def test_page_provenance_remains_local_and_next_page_address_is_not_borrowed(self):
        for subject in SUBJECTS:
            with self.subTest(subject=subject):
                text = f'Учебная первая страница\fДолжник: {subject}\nАдрес регистрации: {ADDRESS}\nОПРЕДЕЛИЛ:'
                card = self.parse_text(text)
                self.assert_address_source(text, card)
                self.assertEqual(card.address.source_page, 2)
                split = f'Должник: {subject}\fАдрес регистрации: {ADDRESS}'
                self.assertIsNone(self.parse_text(split).address.value)


if __name__ == '__main__':
    unittest.main()
