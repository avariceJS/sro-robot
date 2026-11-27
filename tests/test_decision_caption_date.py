"""Synthetic OCR caption layouts retain full-text date and source ownership."""
import unittest

from sro_robot.core.extract import _decision_full_text_date, parse


def caption(separator=' ', blank='', newline='\n'):
    return newline.join((
        'Арбитражный суд Учебной области',
        'РЕШЕНИЕ',
        f'«25» ноября 2099 года{separator}Дело № А99-123/2099',
        'Резолютивная часть решения объявлена 24 ноября 2099 года',
        blank + 'Полный текст решения изготовлен 25 ноября 2099 года',
        'Рассмотрев заявление гражданина.',
        'УСТАНОВИЛ:',
        '',
    ))


class DecisionCaptionDateTests(unittest.TestCase):
    def test_reconstructed_case_column_and_blank_lines_keep_exact_full_date_span(self):
        for separator in (' ', '  ', '\t'):
            for blank in ('', '\n', ' \t\n', '\n\n'):
                with self.subTest(separator=separator, blank=blank):
                    text = caption(separator, blank)
                    card = parse([text, text])
                    self.assertEqual(card.case_number.value, 'А99-123/2099')
                    self.assertEqual(card.doc_date.value, '25.11.2099')
                    self.assertIsNone(card.doc_date.warning)
                    self.assertEqual(card.doc_date.source_variant, 0)
                    self.assertEqual(text[card.doc_date.source_start:card.doc_date.source_end],
                                     '25 ноября 2099')

    def test_windows_line_breaks_and_numeric_full_date_keep_source_offsets(self):
        text = caption(blank=' \t\r\n', newline='\r\n').replace(
            'изготовлен 25 ноября 2099 года', 'изготовлен 25.11.2099 года')
        selected = _decision_full_text_date(text, 'А99-123/2099')
        self.assertIsNotNone(selected)
        self.assertEqual(selected[0], '25.11.2099')
        self.assertEqual(text[selected[1]:selected[2]], '25.11.2099')

    def test_explicit_manufacture_date_with_quoted_and_plain_case_rows(self):
        for row in ('25 ноября 2099 г.', '«25» ноября 2099 г.', '«25» ноября 2099 года'):
            with self.subTest(row=row):
                text = caption(blank='\n').replace('«25» ноября 2099 года', row).replace(
                    'Полный текст решения изготовлен', 'Решение изготовлено в полном объёме'
                ).replace('\nРезолютивная часть', '\n \t\nРезолютивная часть')
                selected = _decision_full_text_date(text, 'А99-123/2099')
                self.assertEqual(selected[0], '25.11.2099')
                self.assertEqual(text[selected[1]:selected[2]], '25 ноября 2099')
                self.assertIsNone(parse([text]).doc_date.warning)

    def test_alternate_caption_requires_an_exact_dated_pair_and_case_identity(self):
        old_layout = caption().replace('«25» ноября 2099 года', '25 ноября 2099 г.').replace(
            'Полный текст решения изготовлен', 'Решение изготовлено в полном объеме'
        )
        for text in (
            old_layout.replace('25 ноября 2099 г.', '«25] ноября 2099 г.'),
            old_layout.replace('25 ноября 2099 г.', '|25] ноября 2099 г.'),
            old_layout.replace('25 ноября 2099 г.', '26 ноября 2099 г.'),
            old_layout.replace('25 ноября 2099 г.', 'Упомянут город Учебный'),
            old_layout.replace('Рассмотрев', 'В копии указано 26.11.2099.\nРассмотрев'),
            old_layout.replace('25 ноября 2099 года', '25 ноября 2099 по сообщению стороны'),
            old_layout.replace('Решение изготовлено', '\fРешение изготовлено'),
            'Суд цитирует:\n' + old_layout,
        ):
            with self.subTest(text=text):
                self.assertIsNone(_decision_full_text_date(text))
        self.assertIsNone(_decision_full_text_date(old_layout, 'А99-124/2099'))

    def test_intervening_prose_and_new_date_are_not_blank_layout(self):
        for blank in ('\nСогласно материалам дела.\n', '\nПредыдущее решение суда:\n',
                      '\nДолжник: Макетов Иван Петрович.\n',
                      '\nДата заседания: 26.11.2099.\n'):
            with self.subTest(blank=blank):
                self.assertIsNone(_decision_full_text_date(caption(blank=blank)))

    def test_column_and_chronology_contradictions_remain_unqualified(self):
        source = caption(blank='\n')
        for text in (source.replace('«25»', '«24»'),
                     source.replace('объявлена 24 ноября', 'объявлена 26 ноября'),
                     source.replace('изготовлен 25 ноября', 'изготовлен 31 ноября'),
                     source.replace('Рассмотрев', 'Полный текст решения изготовлен 26.11.2099.\nРассмотрев')):
            with self.subTest(text=text):
                self.assertIsNone(_decision_full_text_date(text))

    def test_expected_case_and_competing_case_remain_required(self):
        source = caption(blank='\n')
        self.assertIsNone(_decision_full_text_date(source, 'А99-456/2099'))
        self.assertIsNone(_decision_full_text_date(source.replace(
            'Резолютивная часть', 'Дело № А99-456/2099\nРезолютивная часть')))

    def test_physical_page_break_cannot_join_caption_evidence(self):
        for text in (caption(blank='\f\n'), caption().replace('РЕШЕНИЕ\n', 'РЕШЕНИЕ\n\f\n')):
            with self.subTest(text=text):
                self.assertIsNone(_decision_full_text_date(text))

    def test_quoted_decision_heading_cannot_supply_current_full_date(self):
        for opening, closing in (('«', '»'), ('"', '"'), ('“', '”'), ('„', '“'), ('‘', '’')):
            with self.subTest(opening=opening):
                text = 'Приведена цитата ' + opening + '\n' + caption(blank='\n') + closing
                self.assertIsNone(_decision_full_text_date(text))

    def test_other_act_type_and_missing_consideration_anchor_remain_unqualified(self):
        source = caption(blank='\n')
        self.assertIsNone(_decision_full_text_date(source.replace('РЕШЕНИЕ\n', 'ОПРЕДЕЛЕНИЕ\n')))
        self.assertIsNone(_decision_full_text_date(source.replace('Рассмотрев', 'Изучив')))

    def test_explicit_reference_lead_in_cannot_qualify_a_current_caption(self):
        for marker in ('Ранее решение суда:', 'Предыдущее решение суда:',
                       'Обжалуемое решение суда:', 'Цитируется решение суда:',
                       'Текст решения суда:', 'Цитата из решения суда:',
                       'Выдержка из решения суда:', 'Выписка из решения суда:',
                       'Решением суда постановлено:', 'Суд цитирует:', 'Суд приводит:'):
            for court in (True, False):
                with self.subTest(marker=marker, court=court):
                    source = caption(blank='\n')
                    if not court:
                        source = source.split('\n', 1)[1]
                    text = marker + '\n' + source
                    self.assertIsNone(_decision_full_text_date(text))
                    field = parse([text]).doc_date
                    self.assertEqual(field.value, '24.11.2099')
                    self.assertIsNotNone(field.warning)

    def test_reference_guard_stays_in_its_bounded_page_and_introduction_block(self):
        for prefix in ('Ранее решение суда принято.\n',
                       'Ранее решение суда:\nДалее исследуется самостоятельный документ.\n',
                       'Ранее решение суда:\n\f\n'):
            with self.subTest(prefix=prefix):
                text = prefix + caption(blank='\n')
                self.assertEqual(_decision_full_text_date(text)[0], '25.11.2099')
