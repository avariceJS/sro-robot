import unittest

from sro_robot.core.appointment_extract import extract_appointment_fields, has_hearing_appointment


CAPTION = ('Арбитражный суд Учебной области\nОПРЕДЕЛЕНИЕ\n'
           'Дело № А99-4321/2099\n5 мая 2099 г.\n')
RECITAL = ('Рассмотрев заявление гражданки Примеровой Елены Максовны '
           'о признании себя несостоятельной (банкротом), суд установил:\n')
OPERATIVE = '\nОПРЕДЕЛИЛ:\n'


class AppointmentExtractionTests(unittest.TestCase):
    def test_live_hearing_distinct_from_past_hearing_and_deadline(self):
        text = (CAPTION + RECITAL + 'Ранее просила утвердить управляющего. '
                'Прошлое судебное заседание состоялось 02.05.2099 в 16:30.\n' + OPERATIVE
                + 'Назначить судебное заседание на 10:45 31.05.2099 в помещении суда.\n'
                '2. Предложить представить документы до 24.05.2099.\n\f')
        fields = extract_appointment_fields([text, text])
        self.assertEqual({k: v['value'] for k, v in fields.items()}, {
            'case_number': 'А99-4321/2099', 'court': 'Арбитражный суд Учебной области',
            'doc_date': '05.05.2099', 'applicant': 'Примерова Елена Максовна',
            'debtor_name': 'Примерова Елена Максовна',
            'hearing_date': '31.05.2099', 'hearing_time': '10:45',
        })
        for item in fields.values():
            self.assertEqual(item['source_variant'], 0)
            self.assertEqual(item['source_page'], 1)
            self.assertTrue(text[item['source_start']:item['source_end']])
        hearing = fields['hearing_date']
        self.assertEqual(text[hearing['source_start']:hearing['source_end']], '31.05.2099')

    def test_hearing_as_review_of_application_and_ocr_punctuation(self):
        text = (CAPTION + RECITAL + OPERATIVE
                + '2. Рассмотрение «обоснованности заявления должника о признании банкротом '
                'назначить на 21 июня 2099 г. на 09 час, 20\n| мин. в зале суда.\n')
        fields = extract_appointment_fields([text])
        self.assertEqual(fields['hearing_date']['value'], '21.06.2099')
        self.assertEqual(fields['hearing_time']['value'], '09:20')

    def test_named_practitioner_and_applicant_are_different_roles(self):
        text = (CAPTION + 'Рассмотрев заявление Образцова Марка Львовича о признании '
                'иного гражданина банкротом.' + OPERATIVE
                + '1. Утвердить Макетову Лидию Семёновну финансовым управляющим.\n')
        fields = extract_appointment_fields([text])
        self.assertEqual(fields['applicant']['value'], 'Образцов Марк Львович')
        self.assertEqual(fields['practitioner']['value'], 'Макетова Лидия Семёновна')
        self.assertNotIn('debtor_name', fields)
        self.assertNotIn('hearing_date', fields)

    def test_incomplete_or_conflicting_readings_do_not_make_a_complete_hearing(self):
        prefix = CAPTION + RECITAL + OPERATIVE + 'Назначить судебное заседание '
        date_only = prefix + 'на 21 июня 2099 г. в зале суда.'
        time_only = prefix + 'на 09:20 в зале суда.'
        fields = extract_appointment_fields([date_only, time_only])
        self.assertIn('hearing_date', fields)
        self.assertNotIn('hearing_time', fields)
        self.assertTrue(has_hearing_appointment([date_only, time_only]))
        complete = prefix + 'на 21.06.2099 в 09:20 в зале суда.'
        different = prefix + 'на 22.06.2099 в 09:20 в зале суда.'
        self.assertNotIn('hearing_date', extract_appointment_fields([complete, different]))

    def test_preface_does_not_override_conflicting_explicit_party_labels(self):
        labels = 'Заявитель: Образцов Марк Львович\nДолжник: Макетова Лидия Семёновна\n'
        fields = extract_appointment_fields([CAPTION + labels + RECITAL])
        self.assertNotIn('applicant', fields)
        self.assertNotIn('debtor_name', fields)

    def test_old_or_missing_schedule_and_no_operative_heading(self):
        prefix = CAPTION + RECITAL
        for text in (
            prefix + 'Назначить судебное заседание на 31.05.2099 в 12:30.',
            prefix + OPERATIVE + 'Назначить судебное заседание на 03.05.2099 в 12:30.',
            prefix + OPERATIVE + 'Назначить судебное заседание; подготовить к 21.05.2099.',
            prefix + OPERATIVE + 'Назначить судебное заседание на 31.02.2099 в 64:90.',
        ):
            with self.subTest(text=text):
                fields = extract_appointment_fields([text])
                self.assertNotIn('hearing_date', fields)
                self.assertNotIn('hearing_time', fields)

    def test_following_sentence_does_not_supply_hearing_time(self):
        text = (CAPTION + RECITAL + OPERATIVE + 'Назначить судебное заседание на 31.05.2099. '
                'Сведения необходимо представить до 10 часов 30 минут.')
        fields = extract_appointment_fields([text])
        self.assertIn('hearing_date', fields)
        self.assertNotIn('hearing_time', fields)

    def test_quoted_old_heading_does_not_supply_current_hearing(self):
        text = (CAPTION + RECITAL + 'Ранее суд принял следующее определение:' + OPERATIVE
                + 'Назначить судебное заседание на 31.05.2099 в 12:30.\n'
                'Суд отменяет приведенное определение. В назначении заседания отказать.')
        self.assertFalse(has_hearing_appointment([text]))
        self.assertNotIn('hearing_date', extract_appointment_fields([text]))

    def test_incoming_application_date_is_not_the_act_date(self):
        text = ('ОПРЕДЕЛЕНИЕ\nДело № А99-4321/2099\nВходящее заявление от 15.10.2099\n'
                + RECITAL + OPERATIVE + 'Утвердить временным управляющим Макетову Лидию Семёновну.')
        fields = extract_appointment_fields([text])
        self.assertIn('practitioner', fields)
        self.assertNotIn('doc_date', fields)

    def test_capture_reader_bounds(self):
        self.assertEqual(extract_appointment_fields([]), {})
        self.assertEqual(extract_appointment_fields(['банкротство'] * 6), {})
        self.assertEqual(extract_appointment_fields([None]), {})


if __name__ == '__main__':
    unittest.main()
