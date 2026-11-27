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
                + 'Назначить судебное заседание на 10:45 мск 31.05.2099 в помещении суда.\n'
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

    def test_embedded_hearing_request_is_not_an_operative_appointment(self):
        for request in (
            'Отказать в удовлетворении ходатайства назначить судебное заседание',
            'Не назначить судебное заседание',
            'Указать, что ранее было определено назначить судебное заседание',
            'Отказать в удовлетворении ходатайства\nназначить судебное заседание',
            'Указать, что ранее было определено:\nназначить судебное заседание',
            'Указать, что ранее было определено:\n1. Назначить судебное заседание',
        ):
            text = (CAPTION + RECITAL + OPERATIVE + request + ' на 31.05.2099 в 12:30.\n'
                    'Утвердить финансовым управляющим Макетова Марка Львовича.')
            with self.subTest(request=request):
                fields = extract_appointment_fields([text])
                self.assertFalse(has_hearing_appointment([text]))
                self.assertNotIn('hearing_date', fields)
                self.assertNotIn('hearing_time', fields)
                self.assertEqual(fields['practitioner']['value'], 'Макетов Марк Львович')

    def test_direct_hearing_clause_can_follow_another_operative_sentence(self):
        for separator in ('. ', '; ', '.\n2. ', '.\n2) '):
            text = (CAPTION + RECITAL + OPERATIVE + 'Принять заявление к производству' + separator
                    + 'Назначить судебное заседание на 31.05.2099 в 12:30.')
            with self.subTest(separator=separator):
                fields = extract_appointment_fields([text])
                self.assertTrue(has_hearing_appointment([text]))
                self.assertEqual(fields['hearing_date']['value'], '31.05.2099')
                self.assertEqual(fields['hearing_time']['value'], '12:30')

    def test_acceptance_and_hearing_can_share_one_operative_clause(self):
        hearing = 'назначить судебное заседание на 31.05.2099 в 12:30.'
        for acceptance in ('Принять заявление к производству, ', '1. Принять заявление к производству, ',
                           'Принять заявление к производству,\n'):
            text = CAPTION + RECITAL + OPERATIVE + acceptance + hearing
            with self.subTest(acceptance=acceptance):
                fields = extract_appointment_fields([text])
                self.assertTrue(has_hearing_appointment([text]))
                self.assertEqual(fields['hearing_date']['value'], '31.05.2099')
                self.assertEqual(fields['hearing_time']['value'], '12:30')
                date = fields['hearing_date']
                self.assertEqual(text[date['source_start']:date['source_end']], '31.05.2099')
        for introduction in (
            'Принять заявление к производству, предложить ',
            'Принять заявление к производству, но не ',
            'Отказать в ходатайстве принять заявление к производству, ',
            'Указать, что ранее было постановлено:\nПринять заявление к производству, ',
            'Предложить представить документы\n',
        ):
            text = CAPTION + RECITAL + OPERATIVE + introduction + hearing
            with self.subTest(introduction=introduction):
                fields = extract_appointment_fields([text])
                self.assertFalse(has_hearing_appointment([text]))
                self.assertNotIn('hearing_date', fields)
                self.assertNotIn('hearing_time', fields)

    def test_practitioner_approval_requires_a_direct_operative_instruction(self):
        approval = 'Утвердить финансовым управляющим Макетова Марка Львовича.'
        for introduction in (
            'Отказать в удовлетворении ходатайства\n',
            'Указать, что прежним определением было предписано:\n',
            'Указать, что прежним определением было предписано:\n1. ',
        ):
            with self.subTest(introduction=introduction):
                fields = extract_appointment_fields([CAPTION + RECITAL + OPERATIVE + introduction + approval])
                self.assertNotIn('practitioner', fields)
        for introduction in ('', '1. ', 'Принять заявление к производству. ',
                             'Принять заявление к производству; ', 'Принять заявление к производству.\n2) '):
            with self.subTest(introduction=introduction):
                text = CAPTION + RECITAL + OPERATIVE + introduction + approval
                fields = extract_appointment_fields([text])
                practitioner = fields['practitioner']
                self.assertEqual(practitioner['value'], 'Макетов Марк Львович')
                self.assertEqual(text[practitioner['source_start']:practitioner['source_end']],
                                 'Макетова Марка Львовича')

    def test_revised_or_cancelled_hearing_does_not_keep_the_original_schedule(self):
        for revision in (
            'Исправить дату судебного заседания: вместо 31.05.2099 читать 01.06.2099.',
            'Исправить в определении от 05.05.2099 дату судебного заседания: '
            'вместо 31.05.2099 читать 01.06.2099.',
            'Изменить время судебного заседания на 13:30.',
            'Дату судебного заседания исправить на 01.06.2099.',
            'Время судебного заседания изменить на 13:30.',
            'Место проведения судебного заседания уточнить: зал 4.',
            'Внести исправления в дату судебного заседания: '
            'вместо 31.05.2099 читать 01.06.2099.',
            'Внести изменение во время судебного заседания: вместо 12:30 читать 13:30.',
            'Перенести судебное заседание на 01.06.2099 в 13:30.',
            'Отменить назначенное судебное заседание.',
        ):
            text = (CAPTION + RECITAL + OPERATIVE
                    + 'Назначить судебное заседание на 31.05.2099 в 12:30.\n2. ' + revision)
            with self.subTest(revision=revision):
                fields = extract_appointment_fields([text])
                self.assertTrue(has_hearing_appointment([text]))
                self.assertNotIn('hearing_date', fields)
                self.assertNotIn('hearing_time', fields)

    def test_unrelated_corrections_do_not_remove_an_explicit_schedule(self):
        for correction in (
            'Изменить порядок рассмотрения заявления.',
            'Исправить дату рождения должника на 17.04.1970.',
            'Исправить запись в протоколе судебного заседания об адресе должника.',
            'Дату протокола судебного заседания исправить на 06.05.2099.',
            'Внести исправления в дату составления протокола судебного заседания.',
            'Внести изменения в запись протокола судебного заседания об адресе должника.',
        ):
            text = (CAPTION + RECITAL + OPERATIVE
                    + 'Назначить судебное заседание на 31.05.2099 в 12:30.\n' + correction)
            with self.subTest(correction=correction):
                fields = extract_appointment_fields([text])
                self.assertEqual(fields['hearing_date']['value'], '31.05.2099')
                self.assertEqual(fields['hearing_time']['value'], '12:30')

    def test_a_revision_reading_blocks_old_schedule_from_another_reading(self):
        prefix = CAPTION + RECITAL + OPERATIVE
        original = prefix + 'Назначить судебное заседание на 31.05.2099 в 12:30.'
        revised = original + '\nИзменить время судебного заседания на 13:30.'
        revision_only = prefix + 'Перенести судебное заседание на 01.06.2099 в 13:30.'
        for variants in ([original, revised], [revised, original], [revision_only]):
            with self.subTest(variants=variants):
                self.assertTrue(has_hearing_appointment(variants))
                fields = extract_appointment_fields(variants)
                self.assertNotIn('hearing_date', fields)
                self.assertNotIn('hearing_time', fields)

    def test_caption_reading_adds_debtor_details_without_overriding_case_or_schedule(self):
        primary = CAPTION + RECITAL + OPERATIVE + 'Назначить судебное заседание на 21.06.2099 в 09:20.'
        caption = (CAPTION
                   + 'Судья Арбитражного суда Учебной области Макетов А. Б., рассмотрев заявление о банкротстве.\n'
                   + 'Должник Примерова Елена Максовна (ИНН 990014232035, СНИЛС 112-233-445 95, '
                   'дата рождения: 17.04.1970, адрес регистрации: г. Учебный, ул. Макетная, д. 7, кв. 12).\n'
                   + OPERATIVE + 'Назначить судебное заседание на 23.06.2099 в 10:20.')
        fields = extract_appointment_fields([primary], [caption])
        self.assertEqual(fields['case_number']['value'], 'А99-4321/2099')
        self.assertEqual(fields['hearing_date']['value'], '21.06.2099')
        self.assertEqual(fields['hearing_time']['value'], '09:20')
        self.assertEqual(fields['judge_name']['value'], 'Макетов А. Б.')
        self.assertEqual(fields['debtor_inn']['value'], '990014232035')
        self.assertEqual(fields['debtor_snils']['value'], '112-233-445 95')
        self.assertEqual(fields['debtor_birth_date']['value'], '17.04.1970')
        address = fields['debtor_registration_address']
        self.assertEqual(address['value'], 'г. Учебный, ул. Макетная, д. 7, кв. 12')
        self.assertEqual(address['source_variant'], 1)
        self.assertEqual(caption[address['source_start']:address['source_end']], address['value'])
        different_case = caption.replace('А99-4321', 'А99-1234')
        self.assertNotIn('debtor_inn', extract_appointment_fields([primary], [different_case]))

    def test_debtor_biography_does_not_inherit_unlabelled_or_foreign_attributes(self):
        first = (CAPTION + RECITAL
                 + 'Должник Примерова Елена Максовна (СНИЛС 112-233-445 95, 990014232035).\n')
        different = first.replace('Примерова Елена Максовна (', 'Макетова Анна Олеговна (')
        quoted = first.replace('СНИЛС ', 'представитель СНИЛС ')
        self.assertNotIn('debtor_inn', extract_appointment_fields([first]))
        self.assertNotIn('debtor_snils', extract_appointment_fields([first, different]))
        self.assertNotIn('debtor_snils', extract_appointment_fields([quoted]))

    def test_debtor_id_conflicts_across_readings_or_roles_are_withheld(self):
        primary = CAPTION + RECITAL + 'Должник: Примерова Елена Максовна (ИНН 990014232035).\n'
        conflicting_id = primary.replace('990014232035', '990004321007')
        result = extract_appointment_fields([primary], [conflicting_id])
        self.assertEqual(result['debtor_name']['value'], 'Примерова Елена Максовна')
        self.assertNotIn('debtor_inn', result)
        appointment = primary + OPERATIVE + (
            'Утвердить финансовым управляющим Макетова Марка Львовича (Инн: 990014232035).'
        )
        self.assertNotIn('debtor_inn', extract_appointment_fields([appointment]))

    def test_debtor_scope_excludes_a_relatives_address(self):
        text = (CAPTION + RECITAL + 'Должник: Примерова Елена Максовна '
                '(адрес регистрации супруга: г. Учебный, ул. Макетная, д. 7, кв. 12).')
        fields = extract_appointment_fields([text])
        self.assertIn('debtor_name', fields)
        self.assertNotIn('debtor_registration_address', fields)

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
        for deadline in ('. Сведения необходимо представить до 10 часов 30 минут.',
                         ', обязав заявителя представить сведения до 10 часов 30 минут.',
                         ' и установить срок представления сведений до 10 часов 30 минут.'):
            text = CAPTION + RECITAL + OPERATIVE + 'Назначить судебное заседание на 31.05.2099' + deadline
            with self.subTest(deadline=deadline):
                fields = extract_appointment_fields([text])
                self.assertIn('hearing_date', fields)
                self.assertNotIn('hearing_time', fields)

    def test_quoted_old_heading_does_not_supply_current_hearing(self):
        for quote in ('Ранее суд принял следующее определение:', 'Определением суда постановлено:'):
            text = (CAPTION + RECITAL + quote + OPERATIVE
                    + 'Назначить судебное заседание на 31.05.2099 в 12:30.\n'
                    'Суд отменяет приведенное определение. В назначении заседания отказать.')
            self.assertFalse(has_hearing_appointment([text]))
            self.assertNotIn('hearing_date', extract_appointment_fields([text]))

    def test_incoming_application_date_is_not_the_act_date(self):
        for incoming in ('Входящее заявление от 15.10.2099', '15.10.2099 — дата поступления заявления'):
            text = ('ОПРЕДЕЛЕНИЕ\nДело № А99-4321/2099\n' + incoming + '\n'
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
