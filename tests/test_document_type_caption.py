"""Synthetic typed captions outrank body mentions without losing provenance."""

import unittest

from sro_robot.core.extract import DOC_TYPES, _locate_doc_type, find_doc_type, parse


class DocumentTypeCaptionTests(unittest.TestCase):
    def test_same_line_spaced_captions_outrank_earlier_body_words(self):
        for marker, expected in DOC_TYPES.items():
            for separator in (' ', '  ', '\t'):
                with self.subTest(marker=marker, separator=repr(separator)):
                    title = separator.join(marker)
                    text = f'Ранее поступило заявление в суд.\n{title}\nРассмотрев заявление стороны.'
                    self.assertEqual(find_doc_type(text), expected)
                    self.assertEqual(_locate_doc_type(text, expected), (text.index(title), text.index(title) + len(title)))

    def test_product_fields_bind_to_exact_caption_in_the_selected_reading(self):
        for title, expected in (('Р Е Ш Е Н И Е', 'решение'),
                                ('О П Р Е Д Е Л Е Н И Е', 'определение'),
                                ('З А Я В Л Е Н И Е', 'заявление')):
            with self.subTest(title=title):
                text = ('Арбитражный суд\n  ' + title
                        + '  \nРассмотрев заявление стороны.\fСинтетическая вторая страница')
                card = parse([text])
                self.assertEqual(card.doc_type.value, expected)
                self.assertEqual(card.raw_text[card.doc_type.source_start:card.doc_type.source_end], title)
                self.assertEqual(card.doc_type.source_page, 1)
                self.assertEqual(card.doc_type.source_variant, 0)

    def test_quoted_or_reproduced_caption_cannot_outrank_the_current_caption(self):
        for reference in ('Суд цитирует:\n«\nР Е Ш Е Н И Е\n»\n',
                          'Суд цитирует:\n“\nР Е Ш Е Н И Е\n”\n',
                          'Текст решения:\nР Е Ш Е Н И Е\n',
                          'Суд цитирует:\nР Е Ш Е Н И Е\n',
                          'Суд приводит:\nР Е Ш Е Н И Е\n'):
            with self.subTest(reference=reference):
                title = 'О П Р Е Д Е Л Е Н И Е'
                text = reference + title + '\nРассмотрев заявление стороны.'
                self.assertEqual(find_doc_type(text), 'определение')
                start, end = _locate_doc_type(text, 'определение')
                self.assertEqual(text[start:end], title)

    def test_later_page_and_cross_page_letters_do_not_establish_current_caption(self):
        for text in ('Поступило заявление стороны.\fР Е Ш Е Н И Е\n',
                     'Р Е Ш Е\fН И Е\nРассмотрев заявление стороны.'):
            with self.subTest(text=text):
                self.assertEqual(find_doc_type(text), 'заявление')

    def test_first_nonempty_page_has_its_own_quote_state_and_provenance(self):
        text = ' \n\fР Е Ш Е Н И Е\nРассмотрев заявление стороны.'
        card = parse([text])
        self.assertEqual(card.doc_type.value, 'решение')
        self.assertEqual(card.doc_type.source_page, 2)
        self.assertEqual(text[card.doc_type.source_start:card.doc_type.source_end], 'Р Е Ш Е Н И Е')

    def test_exact_caption_does_not_consume_prose_or_other_lines(self):
        cases = (
            ('Р Е Ш Е Н И Е суда ранее цитировалось.\nПоступило заявление.', 'заявление'),
            ('Р Е Ш Е\nН И Е\nПоступило заявление.', 'заявление'),
            ('ОПРЕДЕЛЕНИЕ\nР Е Ш Е Н И Е\nПоступило заявление.', 'определение'),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(find_doc_type(text), expected)

    def test_no_qualified_caption_retains_the_earliest_word_fallback(self):
        text = 'Ранее вынесенное определение содержит заявление стороны и решение по другому делу.'
        self.assertEqual(find_doc_type(text), 'определение')
        start, end = _locate_doc_type(text, 'определение')
        self.assertEqual(text[start:end], 'определение')

    def test_artificial_scan_limit_cannot_complete_a_title_line(self):
        prefix = 'Заявление стороны.\n' + ' ' * (1500 - len('Заявление стороны.\n') - len('РЕШЕНИЕ'))
        text = prefix + 'РЕШЕНИЕ суда процитировано далее.'
        self.assertEqual(find_doc_type(text), 'заявление')


if __name__ == '__main__':
    unittest.main()
