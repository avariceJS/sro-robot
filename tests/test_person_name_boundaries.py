"""Synthetic names retain whole tokens and never invent a patronymic."""
import unittest

from sro_robot.core import morph
from sro_robot.core.extract import _personal_section_end, parse
from sro_robot.core.source_locations import SourceLocationCollector


INN = '772881234508'


def person_suggestions(card):
    return [item for item in card.review_suggestions if 'fio' in item.get('fields', {})]


class PersonNameBoundaryTests(unittest.TestCase):
    def test_grouped_pair_inn_keeps_the_same_narrow_ownership_boundary(self):
        for prefix in ('Должник:', 'Гражданин'):
            for name in ('Иванов Иван Петрович', 'Макетов Иван Петрович'):
                for inn in ('7728 812345 08', '77 28 81 23 45 08', '7728 812345\n08',
                            '7728 812345\r\n 08', '7728\n8123\n4508', '77\n2881\n234508'):
                    with self.subTest(prefix=prefix, name=name, inn=inn):
                        text = f'{prefix} {name}; Дюма Анна, ИНН {inn}.'
                        card = parse([text])
                        self.assertIsNone(card.inn.value)
                        self.assertFalse(any('inn' in item['fields'] for item in person_suggestions(card)))

    def test_standalone_leading_grouped_inn_is_retained_without_pair(self):
        text = 'Сведения о гражданине: ИНН 7728 812345 08, Макетов Иван Петрович.'
        found = person_suggestions(parse([text]))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['fields']['fio']['value'], 'Макетов Иван Петрович')
        self.assertEqual(found[0]['fields']['inn']['value'], INN)

    def test_identifier_fallback_cannot_bypass_following_pair_id_boundary(self):
        for prefix in ('Должник:', 'Гражданин'):
            for separator in ('; ', ';\n', '.\n', '\n', ', '):
                with self.subTest(prefix=prefix, separator=separator):
                    text = f'{prefix} Макетов Иван Петрович{separator}Дюма Анна, ИНН {INN}.'
                    card = parse([text])
                    self.assertIsNone(card.inn.value)
                    self.assertFalse(any('inn' in item['fields'] for item in person_suggestions(card)))

    def test_qualified_pair_identifier_cannot_move_to_later_unknown_name(self):
        for prefix in ('Должник:', 'Гражданин'):
            for heading in ('', 'Сведения:\n', 'Сведения, '):
                with self.subTest(prefix=prefix, heading=heading):
                    text = f'{heading}Дюма Анна, ИНН {INN};\n{prefix} Макетов Иван Петрович.'
                    card = parse([text])
                    self.assertIsNone(card.inn.value)
                    self.assertFalse(any('inn' in item['fields'] for item in person_suggestions(card)))

    def test_identifier_fallback_keeps_own_id_before_and_after_unknown_surname(self):
        for text in (
            f'Гражданин Макетов Иван Петрович, ИНН {INN}.',
            f'Сведения о гражданине: ИНН {INN}, Макетов Иван Петрович.',
            f'ИНН {INN}, СНИЛС 112-233-445 95, Макетов Иван Петрович.',
            f'Гражданин Макетов Иван Петрович, ИНН {INN}; Дюма Анна, ИНН 500100732259.',
            f'Гражданин Макетов Иван Петрович, ИНН {INN}, Дюма Анна, ИНН 500100732259.',
        ):
            with self.subTest(text=text):
                found = person_suggestions(parse([text]))
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]['fields']['fio']['value'], 'Макетов Иван Петрович')
                self.assertEqual(found[0]['fields']['inn']['value'], INN)

    def test_unlabeled_pair_with_own_inn_stops_earlier_person_ownership(self):
        for prefix in ('Должник:', 'Гражданин'):
            for separator in ('; ', ';\n', '.\n', '\n', ', '):
                with self.subTest(prefix=prefix, separator=separator):
                    text = f'{prefix} Иванов Иван Петрович{separator}Дюма Анна, ИНН {INN}.'
                    card = parse([text])
                    self.assertEqual(card.fio_raw.value, 'Иванов Иван Петрович' if prefix == 'Должник:' else None)
                    self.assertIsNone(card.inn.value)
                    found = person_suggestions(card)
                    self.assertFalse(any('inn' in item['fields'] for item in found))
                    self.assertFalse(any(item['fields']['fio']['value'] == 'Дюма Анна' for item in found))

    def test_unlabeled_pair_boundary_retains_earlier_owned_inn(self):
        for prefix in ('Должник:', 'Гражданин'):
            for separator in ('; ', ', '):
                with self.subTest(prefix=prefix, separator=separator):
                    text = (f'{prefix} Иванов Иван Петрович, ИНН {INN}{separator}'
                            'Дюма Анна, ИНН 500100732259.')
                    card = parse([text])
                    if prefix == 'Должник:':
                        self.assertEqual(card.inn.value, INN)
                    else:
                        found = person_suggestions(card)
                        self.assertEqual(len(found), 1)
                        self.assertEqual(found[0]['fields']['inn']['value'], INN)

    def test_pair_id_boundary_needs_clause_known_name_and_valid_id(self):
        for tail in (
            f' и Дюма Анна, ИНН {INN}.',
            f' — Дюма Анна, ИНН {INN}.',
            f'; Архивный Фальштест, ИНН {INN}.',
            '; Дюма Анна, ИНН 772881234509.',
            '; Дюма Анна, ИНН 7728 812345 09.',
            '; Дюма Анна, ИНН 7728 812345 080.',
            '; Дюма Анна, ИНН 7728 812345\n080.',
            '; Дюма Анна, ИНН 7728 812345\n09.',
            '; Дюма Анна, ИНН 7728 812345\n\n08.',
            '; Дюма Анна, ИНН 7728 812345\nСНИЛС 08.',
            f'; Дюма Анна, ИНН\n{INN}.',
            f'; Дюма Анна, номер {INN}.',
            f'; «Дюма Анна, ИНН {INN}».',
        ):
            with self.subTest(tail=tail):
                text = 'Иванов Иван Петрович' + tail
                self.assertEqual(_personal_section_end(text, 0, len(text)), len(text))

    def test_wrapped_pair_identifier_never_crosses_physical_page(self):
        text = 'Иванов Иван Петрович; Дюма Анна, ИНН 7728 812345\f08.'
        self.assertEqual(_personal_section_end(text, 0, len(text)), text.index('\f'))

    def test_pair_id_boundary_never_moves_before_context_start_inside_name(self):
        text = f'Предыдущий текст; Дюма Анна, ИНН {INN}.'
        start = text.index('Дюма') + 2
        self.assertEqual(_personal_section_end(text, start, len(text)), len(text))

    def test_pair_id_boundary_cannot_use_numeric_prefix_at_scan_edge(self):
        for suffix in ('0.', '\n0.'):
            with self.subTest(suffix=suffix):
                text = f'Иванов Иван Петрович; Дюма Анна, ИНН {INN}{suffix}'
                end = text.index(INN) + len(INN)
                self.assertEqual(_personal_section_end(text, 0, end), end)

    def test_multipart_role_name_is_never_an_authoritative_triplet_prefix(self):
        for name in (
            'Де Ла Круз Анна Петровна',
            'Ван Дюма Анна Сергеевна',
            'Мамедов Али Рашид Оглы',
            'Мамедов Али Рашид оглы',
            'Мамедова Анна Рашид кызы',
            'Иванов Иван Петрович Иванович',
        ):
            with self.subTest(name=name):
                card = parse([f'Должник: {name}, ИНН {INN}.'])
                self.assertIsNone(card.fio_raw.value)
                self.assertIsNone(card.inn.value)
                self.assertIsNone(card.last_name.value)
                self.assertIsNone(card.first_name.value)
                self.assertIsNone(card.middle_name.value)

    def test_inline_personal_fields_and_role_labels_close_a_triplet(self):
        name = 'Иванов Иван Петрович'
        for continuation in (
            f'ИНН {INN}',
            'СНИЛС 112-233-445 95',
            'Дата рождения: 01.02.1980',
            'Место рождения: г. Тестоград',
            'Адрес: г. Тестоград, ул. Учебная, д. 1',
            'Проживающий по адресу: г. Тестоград, ул. Учебная, д. 1',
            'Финансовый управляющий Сидоров Петр Иванович',
        ):
            with self.subTest(continuation=continuation):
                card = parse([f'Должник: {name} {continuation}.'])
                self.assertEqual(card.fio_raw.value, name)

    def test_overlapping_surname_last_match_neither_crashes_nor_owns_later_inn(self):
        text = f'Гражданин Иванов Иван Петрович\nДюма Анна, ИНН {INN}.'
        card = parse([text])
        self.assertIsNone(card.fio_raw.value)
        self.assertIsNone(card.inn.value)
        found = person_suggestions(card)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['fields']['fio']['value'], 'Иванов Иван Петрович')
        self.assertNotIn('inn', found[0]['fields'])

    def test_disjoint_surname_last_reading_keeps_its_own_inn(self):
        text = f'Гражданин Иван Петрович Дюма, ИНН {INN}.'
        found = person_suggestions(parse([text]))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['fields']['fio']['value'], 'Дюма Иван Петрович')
        self.assertEqual(found[0]['fields']['inn']['value'], INN)

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
