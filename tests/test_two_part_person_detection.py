"""Synthetic two-component personal names must stay literal review suggestions."""

import unittest

from sro_robot.core.extract import parse


class TwoPartPersonDetectionTests(unittest.TestCase):
    @staticmethod
    def candidates(text):
        return [item for item in parse([text]).review_suggestions
                if item.get('kind') == 'application_person']

    def test_explicit_pair_without_inn_keeps_literal_names_and_missing_patronymic(self):
        text = 'Гражданка Дюма Анна; СНИЛС 112-233-445 95.'
        found = self.candidates(text)
        self.assertEqual(len(found), 1)
        fields = found[0]['fields']
        self.assertEqual(fields['fio']['value'], 'Дюма Анна')
        self.assertEqual(fields['last_name']['value'], 'Дюма')
        self.assertEqual(fields['first_name']['value'], 'Анна')
        self.assertEqual(fields['snils']['value'], '112-233-445 95')
        self.assertNotIn('middle_name', fields)
        self.assertNotIn('inn', fields)
        for field in ('fio', 'last_name', 'first_name'):
            self.assertEqual(text[fields[field]['source_start']:fields[field]['source_end']], 'Дюма Анна')
        card = parse([text])
        for field in ('fio_raw', 'last_name', 'first_name', 'middle_name', 'inn', 'snils'):
            self.assertIsNone(getattr(card, field).value)

    def test_explicit_pair_at_document_end_needs_no_identifier(self):
        found = self.candidates('Должник: Иванов Иван')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['fields']['fio']['value'], 'Иванов Иван')
        self.assertIsNone(parse(['Должник: Иванов Иван']).fio_raw.value)

    def test_oblique_pair_is_preserved_without_guessing_nominative(self):
        found = self.candidates('В отношении гражданки Ивановой Анны, дата рождения: 01.02.1980.')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['fields']['last_name']['value'], 'Ивановой')
        self.assertEqual(found[0]['fields']['first_name']['value'], 'Анны')

    def test_pair_and_triple_do_not_borrow_each_others_fields(self):
        text = ('гражданин Иванов Иван Петрович; дата рождения: 01.02.1980.\n'
                'гражданка Дюма Анна, СНИЛС 112-233-445 95.')
        found = self.candidates(text)
        self.assertEqual(len(found), 2)
        by_name = {item['fields']['fio']['value']: item['fields'] for item in found}
        self.assertNotIn('snils', by_name['Иванов Иван Петрович'])
        self.assertEqual(by_name['Дюма Анна']['snils']['value'], '112-233-445 95')
        self.assertNotIn('birth_date', by_name['Дюма Анна'])

    def test_triples_compounds_and_other_person_roles_are_not_truncated(self):
        for text in (
            'Гражданин Иванов Иван Петрович; СНИЛС 112-233-445 95.',
            'Гражданин Иванов Иван Петрович Олегович; СНИЛС 112-233-445 95.',
            'Представитель должника: Дюма Анна; СНИЛС 112-233-445 95.',
            'Судья Дюма Анна; СНИЛС 112-233-445 95.',
            'Место рождения: Дюма Анна; СНИЛС 112-233-445 95.',
        ):
            with self.subTest(text=text):
                self.assertFalse(any(item['fields']['fio']['value'] in ('Иванов Иван', 'Дюма Анна')
                                     for item in self.candidates(text)))

    def test_organization_and_unrecognized_given_name_are_not_person_pairs(self):
        for text in ('Заявитель: Банк России', 'Должник: Арбитражный Суд',
                     'Гражданин Макетов Фальштест;'):
            with self.subTest(text=text):
                self.assertEqual(self.candidates(text), [])

    def test_wrapped_representative_modifier_does_not_become_a_person_anchor(self):
        for separator in ('\n', '\r\n', '\n    '):
            with self.subTest(separator=separator):
                text = f'Представитель{separator}должника: Дюма Анна; ИНН 772881234508.'
                self.assertEqual(self.candidates(text), [])

    def test_unqualified_wrapped_heading_and_separate_clause_keep_person_pair(self):
        for prefix in ('ОПРЕДЕЛЕНИЕ\n', 'Представитель не явился.\n',
                       'Представитель\f', 'Представитель\n\n'):
            with self.subTest(prefix=prefix):
                found = self.candidates(prefix + 'Должник: Дюма\nАнна;')
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]['fields']['fio']['value'], 'Дюма Анна')

    def test_pair_anchors_inside_quoted_company_titles_are_ignored(self):
        for opening, closing in (('«', '»'), ('“', '”'), ('"', '"'), ('„', '“'), ('‘', '’')):
            with self.subTest(opening=opening):
                text = f'Должник: ООО {opening}Гражданка Дюма Анна; ИНН 772881234508{closing}'
                self.assertEqual(self.candidates(text), [])

    def test_quoted_following_identifier_does_not_belong_to_person_pair(self):
        for opening, closing in (('«', '»'), ('“', '”'), ('"', '"'), ('„', '“'), ('‘', '’')):
            with self.subTest(opening=opening):
                text = f'Гражданка Дюма Анна; в цитате {opening}ИНН 772881234508{closing}.'
                found = self.candidates(text)
                self.assertEqual(len(found), 1)
                self.assertNotIn('inn', found[0]['fields'])

    def test_nested_title_cannot_create_person_and_closed_quote_releases_next_anchor(self):
        quoted = 'Должник: ООО «Внешнее “Гражданка Дюма Анна; ИНН 772881234508”»'
        self.assertEqual(self.candidates(quoted), [])
        found = self.candidates(quoted + '.\nГражданин Иванов Иван;')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['fields']['fio']['value'], 'Иванов Иван')

    def test_quote_state_is_not_carried_from_another_physical_page(self):
        found = self.candidates('ООО «Незавершенная шапка\fГражданин Иванов Иван;')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['fields']['fio']['value'], 'Иванов Иван')

    def test_pair_does_not_cross_page_or_take_following_page_identifier(self):
        self.assertEqual(self.candidates('Гражданин Иванов\fИван; СНИЛС 112-233-445 95.'), [])
        found = self.candidates('Гражданин Иванов Иван;\fСНИЛС 112-233-445 95.')
        self.assertEqual(len(found), 1)
        self.assertNotIn('snils', found[0]['fields'])

    def test_missing_person_inn_does_not_take_practitioner_identifier(self):
        found = self.candidates('Гражданка Дюма Анна; финансовый управляющий '
                                'Сидоров Петр Иванович, ИНН 772881234508.')
        self.assertEqual(len(found), 1)
        self.assertNotIn('inn', found[0]['fields'])

    def test_repeated_pair_occurrences_stay_bounded_and_report_incompleteness(self):
        text = '\n'.join(['Гражданка Дюма Анна;'] * 12)
        card = parse([text])
        self.assertEqual(len(self.candidates(text)), 8)
        self.assertFalse(card.review_suggestions_complete)

    def test_pair_scan_reports_unread_document_tail(self):
        text = 'Гражданка Дюма Анна;\n' + 'учебный текст ' * 4000
        card = parse([text])
        self.assertEqual(len(self.candidates(text)), 1)
        self.assertFalse(card.review_suggestions_complete)
