"""Synthetic names retain whole tokens and never invent a patronymic."""
import unittest

from sro_robot.core import morph
from sro_robot.core.extract import parse
from sro_robot.core.source_locations import SourceLocationCollector


INN = '772881234508'


def person_suggestions(card):
    return [item for item in card.review_suggestions if 'fio' in item.get('fields', {})]


class PersonNameBoundaryTests(unittest.TestCase):
    def test_two_part_compound_is_not_split_into_authoritative_three_part_name(self):
        name = 'Иванов-Петров Иван'
        text = f'Должник: {name}, ИНН {INN}.'
        card = parse([text])
        self.assertIsNone(card.fio_raw.value)
        found = person_suggestions(card)
        self.assertEqual(len(found), 1)
        fields = found[0]['fields']
        self.assertEqual(fields['fio']['value'], name)
        self.assertEqual(fields['last_name']['value'], 'Иванов-Петров')
        self.assertEqual(fields['first_name']['value'], 'Иван')
        self.assertNotIn('middle_name', fields)
        self.assertEqual(text[fields['fio']['source_start']:fields['fio']['source_end']], name)

    def test_unrecognized_two_part_compound_cannot_bypass_pair_dictionary_guard(self):
        card = parse([f'Должник: Макетов-Примеров Арвель, ИНН {INN}.'])
        self.assertIsNone(card.fio_raw.value)
        self.assertEqual(person_suggestions(card), [])

    def test_orthographic_hyphen_names_retain_literal_whole_name_and_span(self):
        for separator in ('-', '‐', '‑'):
            with self.subTest(separator=separator):
                name = f'Иванова{separator}Петрова Анна Сергеевна'
                text = f'Должник: {name}, ИНН {INN}.'
                card = parse([text])
                self.assertEqual(card.fio_raw.value, name)
                self.assertEqual(card.last_name.value, f'Иванова{separator}Петрова')
                self.assertEqual(text[card.fio_raw.source_start:card.fio_raw.source_end], name)
                self.assertEqual(card.inn.value, INN)

    def test_known_oblique_compound_uses_canonical_hyphen_only_for_morphology(self):
        for separator in ('-', '‐', '‑'):
            with self.subTest(separator=separator):
                parts = (f'Ивановой{separator}Петровой', 'Анны', 'Сергеевны')
                self.assertEqual(morph.person_triplet(parts), ('Иванова-Петрова', 'Анна', 'Сергеевна'))

    def test_typographic_ocr_variants_do_not_create_different_debtors(self):
        first = f'Должник: Иванова‑Петрова Анна Сергеевна, ИНН {INN}.'
        second = first.replace('‑', '-')
        card = parse([first, second])
        self.assertEqual(card.fio_raw.value, 'Иванова‑Петрова Анна Сергеевна')
        self.assertEqual(card.fio_raw.source, 'debtor_role:v1')
        self.assertEqual(card.inn.value, INN)

    def test_unsupported_or_malformed_dashes_never_seed_a_suffix_person(self):
        for surname in ('Иванова‒Петрова', 'Иванова–Петрова', 'Иванова—Петрова',
                        'Иванова−Петрова', 'Иванова--Петрова', 'Иванова-'):
            with self.subTest(surname=surname):
                card = parse([f'Должник: {surname} Анна Сергеевна, ИНН {INN}.'])
                self.assertIsNone(card.fio_raw.value)
                self.assertEqual(person_suggestions(card), [])

    def test_former_surname_parenthesis_is_an_explicit_name_boundary(self):
        card = parse([f'Должник: Иванов(Петров)Иван Иванович, ИНН {INN}.'])
        self.assertEqual(card.fio_raw.value, 'Иванов Иван Иванович')
        self.assertEqual(card.inn.value, INN)

    def test_quoted_company_name_cannot_seed_a_compound_person(self):
        card = parse([f'Должник: ООО «Иванова‑Петрова Анна Сергеевна, ИНН {INN}».'])
        self.assertIsNone(card.fio_raw.value)
        self.assertEqual(person_suggestions(card), [])

    def test_malformed_next_person_still_owns_their_identifier(self):
        for separator in (';\n', '; ', '.\n', '\n'):
            with self.subTest(separator=separator):
                text = ('Гражданин Иванов Иван Петрович' + separator
                        + f'гражданка Иванова–Петрова Анна Сергеевна, ИНН {INN}.')
                found = person_suggestions(parse([text]))
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]['fields']['fio']['value'], 'Иванов Иван Петрович')
                self.assertNotIn('inn', found[0]['fields'])

    def test_damaged_leading_word_character_cannot_expose_a_suffix_person(self):
        for prefix in ('X', '7', '_'):
            with self.subTest(prefix=prefix):
                card = parse([f'Должник: {prefix}Иванов Иван Иванович, ИНН {INN}.'])
                self.assertIsNone(card.fio_raw.value)
                self.assertEqual(person_suggestions(card), [])

    def test_damaged_next_person_label_still_closes_prior_identifier_scope(self):
        for prefix in ('X', '7', '_'):
            with self.subTest(prefix=prefix):
                text = ('Гражданин Иванов Иван Петрович;\n'
                        + f'гражданка {prefix}Иванова–Петрова Анна Сергеевна, ИНН {INN}.')
                found = person_suggestions(parse([text]))
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]['fields']['fio']['value'], 'Иванов Иван Петрович')
                self.assertNotIn('inn', found[0]['fields'])

    def test_component_boxes_follow_original_unicode_compound_before_normalization(self):
        name = 'Ивановой‑Петровой Анны Сергеевны'
        text = f'В отношении гражданки {name}, ИНН {INN}.'
        tsv = ('left\ttop\twidth\theight\tconf\ttext\n'
               '100\t100\t250\t20\t90\tИвановой‑Петровой\n'
               '400\t100\t100\t20\t90\tАнны\n'
               '550\t100\t200\t20\t90\tСергеевны\n')
        card = parse([text])
        locations = SourceLocationCollector()
        locations.record_variant(0, [(text, tsv, (1000, 1000))])
        locations.bind_card(card)
        self.assertEqual(card.last_name.value, 'Иванова-Петрова')
        for attr, left in (('last_name', .1), ('first_name', .4), ('middle_name', .55)):
            with self.subTest(attr=attr):
                self.assertEqual(len(getattr(card, attr).source_boxes), 1)
                self.assertEqual(getattr(card, attr).source_boxes[0]['x'], left)
