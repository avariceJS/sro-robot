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

    def test_reverse_operative_hearing_keeps_exact_schedule_spans(self):
        for subject in ('Судебное заседание', 'Предварительное судебное заседание',
                        'Судебное заседание по рассмотрению заявления',
                        'Судебное заседание по рассмотрению обоснованности заявления должника'):
            text = CAPTION + RECITAL + OPERATIVE + subject + ' назначить на 21 июня 2099 г. в 09:20.'
            with self.subTest(subject=subject):
                fields = extract_appointment_fields([text])
                self.assertTrue(has_hearing_appointment([text]))
                for key, value, literal in (('hearing_date', '21.06.2099', '21 июня 2099'),
                                            ('hearing_time', '09:20', '09:20')):
                    source = fields[key]
                    self.assertEqual(source['value'], value)
                    self.assertEqual(source['source_variant'], 0)
                    self.assertEqual(text[source['source_start']:source['source_end']], literal)

    def test_reverse_hearing_request_quote_and_negation_are_not_current_instructions(self):
        for clause in (
            'Ранее судебное заседание назначить на 21.06.2099 в 09:20.',
            'Судебное заседание не назначить на 21.06.2099 в 09:20.',
            'Отказать в просьбе судебное заседание назначить на 21.06.2099 в 09:20.',
            'Указать, что ранее было определено:\nСудебное заседание назначить на 21.06.2099 в 09:20.',
            'Указать, что ранее было определено:\n2. Судебное заседание назначить на 21.06.2099 в 09:20.',
            '«Судебное заседание назначить на 21.06.2099 в 09:20».',
        ):
            text = CAPTION + RECITAL + OPERATIVE + clause
            with self.subTest(clause=clause):
                self.assertFalse(has_hearing_appointment([text]))
                fields = extract_appointment_fields([text])
                self.assertNotIn('hearing_date', fields)
                self.assertNotIn('hearing_time', fields)

    def test_reverse_hearing_conflict_revision_and_past_date_stay_incomplete(self):
        first = 'Судебное заседание назначить на 21.06.2099 в 09:20.'
        for clause in (
            first + '\n2. Судебное заседание назначить на 22.06.2099 в 10:30.',
            first + '\n2. Отменить назначенное судебное заседание.',
            first + '\n2. Дату судебного заседания исправить на 22.06.2099.',
            first.replace('21.06.2099', '03.05.2099'),
        ):
            text = CAPTION + RECITAL + OPERATIVE + clause
            with self.subTest(clause=clause):
                self.assertTrue(has_hearing_appointment([text]))
                fields = extract_appointment_fields([text])
                self.assertNotIn('hearing_date', fields)
                self.assertNotIn('hearing_time', fields)

    def test_reverse_hearing_cancellation_is_distinct_from_a_denied_request_or_quote(self):
        first = 'Судебное заседание назначить на 21.06.2099 в 09:20.\n'
        cancellations = (
            'Отменить назначение судебного заседания.',
            'Назначенное судебное заседание отменить.',
            'Отменить ранее назначенное судебное заседание.',
        )
        for cancellation in cancellations:
            with self.subTest(cancellation=cancellation):
                text = CAPTION + RECITAL + OPERATIVE + first + '2. ' + cancellation
                fields = extract_appointment_fields([text])
                self.assertTrue(has_hearing_appointment([text]))
                self.assertNotIn('hearing_date', fields)
                self.assertNotIn('hearing_time', fields)
        for mention in (
            '2. Отказать в отмене назначения судебного заседания.',
            '2. Отказать в удовлетворении просьбы отменить ранее назначенное судебное заседание.',
            'Указать, что ранее было определено:\n2. Отменить назначение судебного заседания.',
            'Указать, что ранее было определено:\nНазначенное судебное заседание отменить.',
            '«Отменить ранее назначенное судебное заседание».',
        ):
            with self.subTest(mention=mention):
                fields = extract_appointment_fields([CAPTION + RECITAL + OPERATIVE + first + mention])
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

    def test_explicit_citizen_prefix_does_not_conflict_with_the_same_debtor_biography(self):
        for prefix, name in (('гражданин', 'Примеров Марк Львович'), ('гражданка', 'Примерова Елена Максовна')):
            text = (CAPTION + f'Должник: {prefix} {name} (ИНН 990014232035).\n'
                    'Рассмотрев заявление о банкротстве.\n' + OPERATIVE)
            with self.subTest(prefix=prefix):
                fields = extract_appointment_fields([text])
                self.assertEqual(fields['debtor_name']['value'], name)
                self.assertEqual(fields['debtor_inn']['value'], '990014232035')
                source = fields['debtor_name']
                self.assertEqual(text[source['source_start']:source['source_end']], name)

    def test_explicit_party_identities_stop_at_labeled_biography_with_exact_spans(self):
        applicant = 'Образцов Петр Сергеевич'
        debtor = 'общество с ограниченной ответственностью «Учебная Компания (Север)»'
        text = (CAPTION + f'Заявитель: {applicant} (ИНН 990004321007).\n'
                + f'Должник: {debtor} (ИНН 9900142320, ОГРН 1099900000000).\n'
                'Рассмотрев заявление о банкротстве.\n' + OPERATIVE)
        fields = extract_appointment_fields([text])
        self.assertEqual(fields['applicant']['value'], 'Образцов Пётр Сергеевич')
        self.assertEqual(fields['debtor_name']['value'], debtor)
        for key, literal in (('applicant', applicant), ('debtor_name', debtor)):
            source = fields[key]
            self.assertEqual(text[source['source_start']:source['source_end']], literal)
        self.assertNotIn('debtor_inn', fields)

    def test_organization_parentheses_are_not_automatically_a_biography(self):
        for name in ('ООО «Учебная Компания» (Северный филиал)',
                     'ООО «Учебная Компания (ИНН)»', 'ООО «Незакрытое название (ИНН 9900142320)'):
            text = CAPTION + 'Должник: ' + name + '\nРассмотрев заявление о банкротстве.\n' + OPERATIVE
            with self.subTest(name=name):
                fields = extract_appointment_fields([text])
                self.assertEqual(fields['debtor_name']['value'], name)
                source = fields['debtor_name']
                self.assertEqual(text[source['source_start']:source['source_end']], name)

    def test_foreign_notary_identifier_does_not_become_a_debtor_identifier(self):
        text = (CAPTION + 'Должник: Примеров Марк Львович (дата рождения: 17.04.1970, '
                'нотариус: Макетов М. Л., ИНН 990004321007).\n'
                'Рассмотрев заявление о банкротстве.\n' + OPERATIVE)
        fields = extract_appointment_fields([text])
        self.assertEqual(fields['debtor_name']['value'], 'Примеров Марк Львович')
        self.assertEqual(fields['debtor_birth_date']['value'], '17.04.1970')
        self.assertNotIn('debtor_inn', fields)

    def test_explicit_debtor_identifier_before_another_party_stays_source_bound(self):
        for role in ('нотариус', 'представитель', 'супруг'):
            text = (CAPTION + 'Должник: Примеров Марк Львович (ИНН 990014232035, '
                    + role + ': Макетов М. Л., ИНН 990004321007).\n'
                    'Рассмотрев заявление о банкротстве.\n' + OPERATIVE)
            with self.subTest(role=role):
                fields = extract_appointment_fields([text])
                source = fields['debtor_inn']
                self.assertEqual(source['value'], '990014232035')
                self.assertEqual(text[source['source_start']:source['source_end']], '990014232035')

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

    def test_supplemental_hearing_fills_only_missing_values_with_exact_spans(self):
        primary = CAPTION + RECITAL + OPERATIVE + 'Назначить судебное заседание в 09:20.'
        supplemental = (CAPTION + RECITAL.replace('Елены', 'Анны') + '\f' + OPERATIVE
                        + 'Назначить судебное заседание на 21 июня 2099 г. в 09 часов 20 минут.\n'
                        'Утвердить финансовым управляющим Макетова Марка Львовича.')
        baseline = extract_appointment_fields([primary, primary], [CAPTION, CAPTION])
        fields = extract_appointment_fields([primary, primary], [CAPTION, CAPTION], hearing_readings=[supplemental])
        self.assertEqual(set(fields) - set(baseline), {'hearing_date'})
        self.assertEqual({key: fields[key] for key in baseline}, baseline)
        recovered = fields['hearing_date']
        self.assertEqual(recovered['value'], '21.06.2099')
        self.assertEqual(recovered['source_variant'], 4)
        self.assertEqual(recovered['source_page'], 2)
        self.assertEqual(supplemental[recovered['source_start']:recovered['source_end']], '21 июня 2099')
        date_only = primary.replace('в 09:20', 'на 21.06.2099')
        fields = extract_appointment_fields([date_only], hearing_readings=[supplemental])
        self.assertEqual(fields['hearing_date']['source_variant'], 0)
        time = fields['hearing_time']
        self.assertEqual(time['source_variant'], 1)
        self.assertEqual(supplemental[time['source_start']:time['source_end']], '09 часов 20 минут')

    def test_complete_primary_schedule_ignores_supplemental_conflicts(self):
        primary = CAPTION + RECITAL + OPERATIVE + 'Назначить судебное заседание на 21.06.2099 в 09:20.'
        conflict = primary.replace('21.06.2099', '22.06.2099').replace('09:20', '10:30')
        self.assertEqual(extract_appointment_fields([primary], hearing_readings=[conflict]),
                         extract_appointment_fields([primary]))

    def test_supplemental_hearing_cannot_use_another_act_in_the_same_case(self):
        primary = CAPTION + RECITAL + OPERATIVE + 'Назначить судебное заседание в 09:20.'
        full = primary.replace('в 09:20', 'на 21.06.2099 в 09:20')
        different_act = full.replace('5 мая 2099', '6 мая 2099')
        self.assertEqual(extract_appointment_fields([primary], hearing_readings=[different_act]),
                         extract_appointment_fields([primary]))
        self.assertIn('hearing_date', extract_appointment_fields([primary], hearing_readings=[full]))

    def test_supplemental_hearing_requires_a_matching_primary_case(self):
        primary = CAPTION + RECITAL + OPERATIVE + 'Назначить судебное заседание в 09:20.'
        full = primary.replace('в 09:20', 'на 21.06.2099 в 09:20')
        for variants, supplemental in (
            ([primary], full.replace('А99-4321', 'А99-1234')),
            ([primary], full.replace('Дело № А99-4321/2099\n', '')),
            ([primary.replace('Дело № А99-4321/2099\n', '')], full),
            ([primary, primary.replace('А99-4321', 'А99-1234')], full),
        ):
            with self.subTest(variants=variants, supplemental=supplemental):
                self.assertEqual(extract_appointment_fields(variants, hearing_readings=[supplemental]),
                                 extract_appointment_fields(variants))

    def test_supplemental_hearing_cannot_resolve_primary_conflicts_or_revisions(self):
        prefix = CAPTION + RECITAL + OPERATIVE
        full = prefix + 'Назначить судебное заседание на 21.06.2099 в 09:20.'
        for variants in (
            [full.replace('в 09:20', ''), full.replace('21.06.2099 в 09:20', '22.06.2099')],
            [prefix + 'Назначить судебное заседание на 21.06.2099 или 22.06.2099 в 09:20.'],
            [prefix + 'Назначить судебное заседание на 21.06.2099 в 09:20 или 10:30.'],
            [full.replace('в 09:20', '') + '\n2. Назначить судебное заседание на 22.06.2099.'],
            [full.replace('в 09:20', '') + '\nИзменить время судебного заседания на 10:30.'],
            [prefix + 'Назначить судебное заседание в 10:30.'],
            [prefix + 'Утвердить финансовым управляющим Макетова Марка Львовича.'],
        ):
            with self.subTest(variants=variants):
                self.assertEqual(extract_appointment_fields(variants, hearing_readings=[full]),
                                 extract_appointment_fields(variants))

    def test_supplemental_hearing_requires_one_complete_current_pair(self):
        primary = CAPTION + RECITAL + OPERATIVE + 'Назначить судебное заседание в 09:20.'
        full = primary.replace('в 09:20', 'на 21.06.2099 в 09:20')
        for supplemental in (
            [full.replace('в 09:20', '')], [primary], [full.replace('21.06.2099', '03.05.2099')],
            [full.replace('5 мая 2099 г.\n', '').replace('21.06.2099', '03.05.2099')],
            [full.replace('Назначить судебное заседание', 'Отказать в просьбе назначить судебное заседание')],
            [full + '\nИзменить время судебного заседания на 10:30.'],
            [full, full], [], [None], ['x' * 200_001],
        ):
            with self.subTest(readings=len(supplemental)):
                self.assertEqual(extract_appointment_fields([primary], hearing_readings=supplemental),
                                 extract_appointment_fields([primary]))


if __name__ == '__main__':
    unittest.main()
