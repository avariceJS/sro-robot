"""Synthetic coherent review cards, scoped feedback and immutable raw snapshots."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sro_robot.core.extract import add_review_candidates, parse_variants_for_review
from sro_robot.research.review_schema import FIELDS
from sro_robot.research import review_projection as projection
from sro_robot.research.review_store import ReviewStore, ReviewConflict, ReviewError, field_map, encoded, sha


def field(value, **extra):
    return {'value': value, 'confidence': .6, 'source_variant': 0, 'source_start': 10, 'source_end': 30, 'source_page': 1, **extra}


def person(name='Тестов Тест Тестович', inn='000000000000', address='г. Тестоград, д. 1', partial=False):
    last, first, middle = name.split()
    fields = {'fio': field(name), 'last_name': field(last), 'first_name': field(first), 'middle_name': field(middle), 'inn': field(inn), 'address': field(address), 'address_kind': field('registration')}
    return {'kind': 'application_person', 'fields': fields, 'copyable_fields': ['last_name','first_name','middle_name','inn'] + ([] if partial else ['address','address_kind'])}


def entity(kind='legal_entity', name='ООО «Синтетический образец»', inn='0000000000'):
    target = 'legal_name' if kind == 'legal_entity' else 'debtor_name'
    return {'kind': 'entity_candidate', 'fields': {'entity_kind': field(kind), target: field(name), 'inn': field(inn)}, 'copyable_fields': [target, 'inn']}


class ProjectionTests(unittest.TestCase):
    def project(self, observed, **kwargs):
        return projection.project(observed, field_map(observed), **kwargs)

    @staticmethod
    def operative_text(name='Макетова Анна Петровна', biography=None, ending='.'):
        if biography is None:
            biography = ('дата рождения: 01.01.1980; ИНН: 123456789047; (СНИЛС): 11223344595; '
                         'адрес регистрации: г. Тестоград, ул. Макетная, д. 7, кв. 42/2а')
        return f'РЕШЕНИЕ\r\n\f  Р Е Ш И Л:\r\n  Признать   {name} ({biography}) несостоятельной (банкротом){ending}'

    def operative_projection(self, variants):
        from dataclasses import asdict
        card = parse_variants_for_review(variants)
        before = asdict(card)
        add_review_candidates(card, variants)
        observed = asdict(card)
        self.assertEqual({k: v for k, v in observed.items() if k != 'review_suggestions'},
                         {k: v for k, v in before.items() if k != 'review_suggestions'})
        return self.project(observed)

    def test_direct_bankruptcy_disposition_fills_one_review_bundle_with_exact_spans(self):
        raw = self.operative_text(ending='.\nУтвердить финансового управляющего. ИНН 772881234508.')
        result = self.operative_projection([raw])
        self.assertEqual(result['subject_role'], 'debtor')
        self.assertFalse(result['selection_required'])
        self.assertEqual(result['entity_kind'], 'person')
        self.assertTrue(raw[slice(*[result['subject_role_location'][x] for x in ('source_start', 'source_end')])].startswith('Признать'))
        for key, value, literal in (
            ('inn', '123456789047', '123456789047'),
            ('snils', '112-233-445 95', '11223344595'),
            ('address', 'г. Тестоград, ул. Макетная, д. 7, кв. 42/2а', 'г. Тестоград, ул. Макетная, д. 7, кв. 42/2а'),
        ):
            field = result['fields'][key]
            self.assertEqual(field['value'], value)
            self.assertTrue(field['review_only'])
            self.assertEqual(field['source'], 'review-candidate:operative_person')
            self.assertEqual(field['source_page'], 2)
            self.assertEqual(raw[field['source_start']:field['source_end']], literal)

    def test_operative_cannot_use_foreign_records_quoted_orders_or_conflicting_readings(self):
        raw = self.operative_text()
        for variants in (
            [raw.replace('РЕШЕНИЕ\r\n', 'РЕШЕНИЕ\r\n«\r\n') + '\n»'],
            [raw.replace('РЕШЕНИЕ\r\n', 'РЕШЕНИЕ\r\nСуд ранее постановил: "\r\n') + '\n"'],
            [raw.replace('(банкротом).', '(банкротом) далее')],
            [self.operative_text(biography='супруга Петрова Анна Петровна, ИНН 123456789047')],
            [self.operative_text(biography='нотариус (ИНН 123456789047)')],
            [self.operative_text(biography='адрес опекуна: г. Тестоград, ул. Макетная, д. 7')],
            [raw, raw + '\nПризнать другого гражданина банкротом.'],
        ):
            with self.subTest():
                result = self.operative_projection(variants)
                self.assertFalse(any(x['kind'] == 'operative_person' for x in result['candidates']))
        for biography, field_name in (
            ('ИНН банка: 123456789047', 'inn'),
            ('бухгалтер: ИНН 123456789047', 'inn'),
            ('адрес регистрации матери: г. Тестоград, ул. Лесная, д. 1', 'address'),
        ):
            result = self.operative_projection([self.operative_text(biography=biography)])
            candidate = next(x for x in result['candidates'] if x['kind'] == 'operative_person')
            self.assertNotIn(field_name, candidate['fields'])
        result = self.operative_projection([raw, self.operative_text(name='Петрова Анна Петровна')])
        self.assertTrue(result['selection_required'])
        self.assertIsNone(result['selected_candidate_id'])

    def test_operative_recognition_retains_requisites_before_procedure(self):
        raw = self.operative_text(
            biography=('ИНН – 123456789047, СНИЛС — 112-233-445-95; '
                       'адрес регистрации: г. Тестоград, ул. Макетная, д. 7, кв. 42'),
            ending=' и ввести в отношении гражданина процедуру реализации имущества '
                   'гражданина сроком на шесть месяцев.',
        )
        result = self.operative_projection([raw])
        for field, original in (('inn', '123456789047'), ('snils', '112-233-445-95')):
            row = result['fields'][field]
            self.assertEqual(row['source'], 'review-candidate:operative_person')
            self.assertEqual(raw[row['source_start']:row['source_end']], original)
        foreign = raw.replace('в отношении гражданина', 'в отношении другого гражданина')
        self.assertFalse(any(c['kind'] == 'operative_person'
                             for c in self.operative_projection([foreign])['candidates']))

    def test_operative_procedure_pronoun_requires_recognition_gender(self):
        for name, ending, pronoun in (
            ('Макетову Анну Петровну', 'ой', 'неё'),
            ('Макетову Анну Петровну', 'ой', 'нее'),
            ('Иванова Ивана Ивановича', 'ым', 'него'),
        ):
            with self.subTest(pronoun=pronoun):
                raw = self.operative_text(name=name, ending=(
                    f' и ввести в отношении {pronoun} процедуру реализации имущества '
                    'гражданина сроком на шесть месяцев, до 25.11.2099.'
                )).replace('несостоятельной', 'несостоятельн' + ending)
                result = self.operative_projection([raw])
                self.assertEqual(result['subject_role'], 'debtor')
                field = result['fields']['inn']
                self.assertEqual(field['source'], 'review-candidate:operative_person')
                self.assertEqual(raw[field['source_start']:field['source_end']], '123456789047')
                wrong = 'него' if ending == 'ой' else 'неё'
                for altered in (
                    raw.replace(f'в отношении {pronoun}', f'в отношении {wrong}'),
                    raw.replace(f'в отношении {pronoun}', 'в отношении Ивановой Анны Петровны'),
                    raw.replace('процедуру реализации имущества', 'процедуру реструктуризации долгов'),
                    raw.replace('шесть месяцев, до', 'шесть месяцев неизвестного лица, до'),
                ):
                    self.assertFalse(any(candidate['kind'] == 'operative_person'
                                         for candidate in self.operative_projection([altered])['candidates']))

    def test_operative_direct_procedure_deadline_requires_valid_date_and_terminal(self):
        raw = self.operative_text(ending=(
            ' и ввести в отношении неё процедуру реализации имущества сроком до 25.11.2099.'
        ))
        for positive in (raw, raw.replace('25.11.2099', '29.02.2096'),
                         raw.replace('25.11.2099', '25 ноября 2099 года')):
            result = self.operative_projection([positive])
            self.assertEqual(result['subject_role'], 'debtor')
            inn = result['fields']['inn']
            self.assertEqual(inn['source'], 'review-candidate:operative_person')
            self.assertEqual(positive[inn['source_start']:inn['source_end']], '123456789047')
        for altered in (
            raw.replace('25.11.2099', '31.11.2099'),
            raw.replace('25.11.2099', '29.02.2099'),
            raw.replace('25.11.2099', '25.11.20999'),
            raw.replace('25.11.2099', '31 ноября 2099 года'),
            raw.replace('25.11.2099', '25 ноября 2099'),
            raw.replace('25.11.2099.', '25.11.2099 неизвестного лица.'),
            raw.replace('в отношении неё', 'в отношении него'),
            raw.replace('процедуру реализации имущества', 'процедуру реструктуризации долгов'),
        ):
            self.assertFalse(any(item['kind'] == 'operative_person'
                                 for item in self.operative_projection([altered])['candidates']))

    def test_operative_rural_address_preserves_postcode_house_and_units_without_street(self):
        address = '999999\r\nМакетная область, Учебный район, д.Банкротово, дом 17, корп. 2, стр. 3, кв. 42/2а'
        raw = self.operative_text(biography=f'адрес регистрации: {address}; (СНИЛС): 11223344595')
        result = self.operative_projection([raw])
        field = result['fields']['address']
        self.assertEqual(field['value'], ' '.join(address.split()))
        self.assertTrue(field['review_only'])
        self.assertFalse(field.get('address_partial', False))
        self.assertEqual(raw[field['source_start']:field['source_end']], address)
        self.assertEqual(result['fields']['snils']['value'], '112-233-445 95')

    def test_operative_nested_registration_label_preserves_owned_field_spans(self):
        address = '999999, Россия, Макетная область, д. Условная, д. 17'
        raw = self.operative_text(biography=(
            f'адрес регистрации (проживания): {address}; ИНН: 123456789047; СНИЛС: 11223344595'
        ))
        fields = self.operative_projection([raw])['fields']
        for name, literal in (('address', address), ('inn', '123456789047'), ('snils', '11223344595')):
            row = fields[name]
            self.assertEqual(row['source'], 'review-candidate:operative_person')
            self.assertEqual(raw[row['source_start']:row['source_end']], literal)
        self.assertEqual(fields['address']['value'], address)
        self.assertEqual(fields['address_kind']['value'], 'registration')
        for altered in (
            raw.replace('(проживания)', '(проживания матери)'),
            raw.replace('(проживания)', '(Петровой Анны Петровны)'),
            raw.replace('РЕШЕНИЕ\r\n', 'РЕШЕНИЕ\r\n«\r\n') + '\n»',
        ):
            with self.subTest():
                self.assertFalse(any(candidate['kind'] == 'operative_person'
                                     for candidate in self.operative_projection([altered])['candidates']))

    def test_operative_rural_address_requires_explicit_ownership_and_delimiters(self):
        address = '999999 Макетная область, д. Условная, д. 17'
        for biography in (
            f'место рождения: {address}',
            f'адрес регистрации взыскателя: {address}',
            f'адрес регистрации: {address}, кв. неизвестна',
            f'адрес регистрации: {address}, кв. 12 просит исключить из описи',
            f'адрес регистрации: {address}, кв. 2, квартира 3',
            f'адрес регистрации: {address}; адрес проживания: {address}',
        ):
            with self.subTest(biography=biography):
                result = self.operative_projection([self.operative_text(biography=biography)])
                self.assertFalse(any('address' in candidate['fields'] for candidate in result['candidates']))

    def test_operative_after_application_ruling_fills_exact_address_and_name_components(self):
        preamble = 'Заявление Ивановой Анны Петровны о признании её несостоятельной (банкротом) признать обоснованным.'
        address = '999999, Россия, Макетная область, г. Тестоград, ул. Учебная, д. 12 А, корп. 2, кв.\r\n42/2а, комн. 3'
        raw = self.operative_text(
            name='Иванову Анну Петровну',
            biography=f'адрес регистрации: {address}; (СНИЛС): № 11223344595; ИНН 123456789047',
            ending=' и ввести процедуру реализации имущества гражданина сроком на шесть месяцев — до 05.12.2099.',
        ).replace('РЕШЕНИЕ', 'Р Е Ш Е Н И Е').replace('  Признать', preamble + '\r\n  Признать')
        result = self.operative_projection([raw])
        fields = result['fields']
        self.assertEqual(result['subject_role'], 'debtor')
        self.assertEqual(fields['fio_raw']['value'], 'Иванова Анна Петровна')
        self.assertEqual(fields['inn']['value'], '123456789047')
        self.assertEqual(fields['snils']['value'], '112-233-445 95')
        self.assertEqual(fields['address_kind']['value'], 'registration')
        self.assertEqual(fields['address']['value'], ' '.join(address.split()))
        self.assertEqual(raw[fields['address']['source_start']:fields['address']['source_end']], address)
        for altered in (
            raw.replace('Ивановой', 'Петровой'),
            raw.replace('её несостоятельной', 'его несостоятельной'),
            raw.replace('Р Е Ш Е Н И Е', 'Изложение решения другого суда'),
            raw.replace('Р Е Ш Е Н И Е', 'О П Р Е Д Е Л Е Н И Е\nР Е Ш Е Н И Е'),
            raw.replace('Р Е Ш Е Н И Е', 'Р Е Ш Е Н И Е\nСуд цитирует: "Текст\nПродолжение') + '"',
            raw.replace('процедуру реализации имущества', 'процедуру реструктуризации долгов'),
            raw.replace('гражданина сроком на шесть месяцев — до 05.12.2099', 'гражданина Петрова Петра Петровича'),
        ):
            with self.subTest():
                self.assertFalse(any(c['kind'] == 'operative_person' for c in self.operative_projection([altered])['candidates']))

    def test_operative_exact_numbered_application_then_recognition(self):
        raw = self.operative_text().replace('  Признать', '1. Заявление удовлетворить.\r\n2. Признать')
        self.assertEqual(self.operative_projection([raw])['subject_role'], 'debtor')
        for altered in (
            raw.replace('1. Заявление', '2. Заявление'),
            raw.replace('2. Признать', '3. Признать'),
            raw.replace('2. Признать', 'Промежуточные сведения.\r\n2. Признать'),
        ):
            self.assertFalse(any(item['kind'] == 'operative_person'
                                 for item in self.operative_projection([altered])['candidates']))

    def test_operative_named_application_biography_never_supplies_recognition_fields(self):
        for terminal in ('удовлетворить', 'признать обоснованным'):
            with self.subTest(terminal=terminal):
                preamble = ('Заявление гражданки Макетовой Анны Петровны '
                            '(дата рождения: 01.01.1980; ИНН: 123456789047; '
                            'адрес регистрации (проживания): г. Тестоград, ул. Первая, д. 99) '
                            f'о признании её несостоятельной (банкротом) {terminal}.')
                raw = self.operative_text(name='Макетову Анну Петровну').replace('  Признать', preamble + '\r\n  Признать')
                result = self.operative_projection([raw])
                inn = result['fields']['inn']
                self.assertEqual(inn['value'], '123456789047')
                self.assertEqual(raw[inn['source_start']:inn['source_end']], '123456789047')
                self.assertGreater(inn['source_start'], raw.index('Признать'))
                missing = self.operative_projection([raw.replace('ИНН: 123456789047; (СНИЛС)', '(СНИЛС)')])
                candidate = next(item for item in missing['candidates'] if item['kind'] == 'operative_person')
                self.assertNotIn('inn', candidate['fields'])
                for altered in (
                    raw.replace('Макетовой', 'Петровой'),
                    raw.replace('её несостоятельной', 'его несостоятельной'),
                    raw.replace('гражданки', 'гражданина'),
                    raw.replace('дата рождения: 01.01.1980; ИНН: 123456789047',
                                'супруга Петрова Анна Петровна; ИНН: 123456789047', 1),
                    raw.replace(preamble, preamble + '\r\nПромежуточные сведения.'),
                    raw.replace('ИНН: 123456789047', 'ИНН: 500100732259', 1),
                    raw.replace('дата рождения: 01.01.1980', 'дата рождения: 02.01.1980', 1),
                    raw.replace('ИНН: 123456789047', 'ИНН: 123456789047; СНИЛС: 123-456-789-64', 1),
                    raw.replace('ИНН: 123456789047', 'ИНН: 123456789047; ИНН: 500100732259', 1),
                    raw.replace('ИНН: 123456789047', 'СНИЛС: 112-233-445-95; СНИЛС: 123-456-789-64', 1),
                    raw.replace('дата рождения: 01.01.1980', 'дата рождения: 01.01.1980; дата рождения: 02.01.1980', 1),
                    raw.replace('дата рождения: 01.01.1980; ИНН: 123456789047',
                                'дата рождения: 01.01.1980\nИНН: 500100732259', 1),
                    raw.replace('ИНН: 123456789047', '500100732259; ИНН: 500100732259', 1),
                    raw.replace('ИНН: 123456789047', 'ИНН:500100732259', 1),
                    raw.replace('дата рождения: 01.01.1980; ИНН: 123456789047',
                                'дата рождения: 01.01.1980\nСНИЛС: 123-456-789-64', 1),
                    raw.replace('ИНН: 123456789047', '123-456-789-64; СНИЛС: 123-456-789-64', 1),
                    raw.replace('ИНН: 123456789047', '\nСНИЛС — 12345678964', 1),
                    raw.replace('ИНН: 123456789047', '\nдата рождения: 02.01.1980', 1),
                ):
                    self.assertFalse(any(item['kind'] == 'operative_person'
                                         for item in self.operative_projection([altered])['candidates']))
                for prefix in (
                    'место рождения: Тестоград',
                    'дата рождения: 31.02.1980; ИНН: 123456789048; СНИЛС: 11223344594',
                    'дата рождения: 01/01/1980; ИНН: 123456789047; СНИЛС: 112-233-445-95',
                    '500100732259; ИНН: 123456789047; 123-456-789-64; СНИЛС: 112-233-445-95',
                    '112-233-445-95; СНИЛС: 112-233-445-95; 123456789047; ИНН: 123456789047',
                ):
                    original = preamble[preamble.index('(') + 1:preamble.rindex(') о признании')]
                    permitted = raw.replace(original, prefix, 1)
                    self.assertTrue(any(item['kind'] == 'operative_person'
                                        for item in self.operative_projection([permitted])['candidates']))

    def test_named_application_checks_owned_disposition_dates_without_borrowing(self):
        preamble = ('Заявление гражданки Макетовой Анны Петровны '
                    '(дата рождения: 01.01.1980; ИНН: 123456789047) '
                    'о признании её несостоятельной (банкротом) удовлетворить.')
        raw = self.operative_text(name='Макетову Анну Петровну').replace('  Признать', preamble + '\r\n  Признать')
        prefix, clause = raw.split('  Признать', 1)
        for changed in (
            clause.replace('дата рождения: 01.01.1980', 'место рождения: Тестоград\r\nдата рождения: 02.01.1980'),
            clause.replace('дата рождения: 01.01.1980', 'дата рождения: 01.01.1980; дата рождения: 02.01.1980'),
            clause.replace('ИНН: 123456789047', '123456789047; ИНН: 500100732259'),
        ):
            self.assertFalse(any(item['kind'] == 'operative_person' for item in
                                 self.operative_projection([prefix + '  Признать' + changed])['candidates']))
        for changed in (
            clause.replace('дата рождения: 01.01.1980; ', ''),
            clause.replace('дата рождения: 01.01.1980', 'дата рождения: 31.02.1980'),
        ):
            result = self.operative_projection([prefix + '  Признать' + changed])
            candidate = next(item for item in result['candidates'] if item['kind'] == 'operative_person')
            self.assertNotIn('birth_date', candidate['fields'])
        result = self.operative_projection([raw + '\r\nУтвердить финансового управляющего. Дата рождения: 02.01.1980.'])
        inn = result['fields']['inn']
        self.assertGreater(inn['source_start'], raw.index('  Признать'))
        self.assertEqual(raw[inn['source_start']:inn['source_end']], '123456789047')

    def test_operative_postal_duplicate_unit_is_visible_only_as_partial(self):
        raw = self.operative_text(biography=(
            'адрес регистрации: 999999, г. Тестоград, ул. Макетная, д. 7, кв. 42, квартира 54; ИНН 123456789047'
        ))
        address = self.operative_projection([raw])['fields']['address']
        self.assertTrue(address['address_partial'])
        self.assertTrue(address['review_only'])

    def test_bankruptcy_completion_fills_the_named_company_from_its_registry_block(self):
        raw = ('ОПРЕДЕЛЕНИЕ\nО П Р Е Д Е Л И Л:\n'
               'Завершить конкурсное производство в отношении ООО «Синтетический образец» '
               '(ИНН 7700000016, ОГРН 1027700132195).\n'
               'Конкурсный управляющий: ООО «Другая сторона», ИНН 7700000017.')
        result = self.operative_projection([raw])
        self.assertEqual(result['subject_role'], 'debtor')
        self.assertEqual(result['fields']['entity_kind']['value'], 'legal_entity')
        self.assertEqual(result['fields']['legal_name']['value'], 'ООО «Синтетический образец»')
        self.assertEqual(result['fields']['inn']['value'], '7700000016')
        self.assertEqual(result['fields']['ogrn']['value'], '1027700132195')
        for altered in (
            raw.replace('ОПРЕДЕЛЕНИЕ\n', 'ОПРЕДЕЛЕНИЕ\n«\n') + '»',
            raw.replace('ОПРЕДЕЛЕНИЕ\n', 'ОПРЕДЕЛЕНИЕ\nСуд воспроизводит цитату "Текст\n') + '"',
            raw.replace('1027700132195', '1027700132190'),
            raw.replace('1027700132195).', '1027700132195, без закрытия.'),
            raw + '\nЗавершить конкурсное производство в отношении ООО «Второй участник».',
            raw.replace('Завершить', 'Не завершать'),
        ):
            self.assertFalse(any(c['kind'] == 'entity_candidate' for c in self.operative_projection([altered])['candidates']))

    def test_caption_ip_keeps_claimant_separate_and_cannot_replace_existing_subject(self):
        raw = ('РЕШЕНИЕ\nРассмотрев дело по иску ООО «Макет» (ИНН 7700000016) к '
               'индивидуальному предпринимателю Иванову Ивану Ивановичу (ИНН 500100001146) о взыскании.\n'
               'УСТАНОВИЛ:\n')
        result = self.operative_projection([raw])
        candidate = next(c for c in result['candidates'] if c['kind'] == 'caption_participant')
        self.assertEqual(candidate['role'], 'defendant')
        self.assertEqual(candidate['entity_kind'], 'sole_proprietor')
        self.assertEqual(candidate['fields']['inn']['value'], '500100001146')
        previous = entity(inn='7700000016')
        alternative = {**candidate, 'copyable_fields': list(candidate['fields'])}
        fixed = self.project({'review_suggestions': [previous, alternative]})
        self.assertEqual(fixed['fields']['inn']['value'], '7700000016')
        for altered in (
            raw.replace('ИНН 7700000016) к индивидуальному предпринимателю Иванову Ивану Ивановичу (ИНН 500100001146)',
                        'ссылка к ООО «Не ответчик») к гражданину Иванову Ивану Ивановичу'),
            raw.replace('к индивидуальному', 'к гражданину Петрову Петру Петровичу и к индивидуальному'),
            raw.replace('Ивановичу', 'Макетоград'),
        ):
            self.assertFalse(any(c['kind'] == 'caption_participant' for c in self.operative_projection([altered])['candidates']))

    def test_maintained_person_occurrence_populates_primary_review_fields_not_raw(self):
        observed = {'review_suggestions': [person()]}
        result = self.project(observed)
        self.assertEqual(result['entity_kind'], 'person')
        self.assertEqual(result['fields']['fio_raw']['value'], 'Тестов Тест Тестович')
        self.assertEqual(result['fields']['inn']['value'], '000000000000')
        self.assertTrue(result['fields']['inn']['review_only'])
        self.assertEqual(result['fields']['inn']['source_start'], 10)
        self.assertNotIn('fio_raw', observed)
        self.assertIsNotNone(result['selected_candidate_id'])

    def test_partial_address_is_visible_but_explicitly_incomplete(self):
        result = self.project({'review_suggestions': [person(partial=True)]})
        address = result['fields']['address']
        self.assertEqual(address['value'], 'г. Тестоград, д. 1')
        self.assertTrue(address['address_partial'])
        self.assertIn('Неполный', address['warning'])

    def test_ambiguous_people_and_conflicting_same_name_identifiers_are_not_combined(self):
        for second in (person(name='Примеров Пример Примерович'), person(inn='111111111111')):
            with self.subTest(second=second['fields']['inn']['value']):
                result = self.project({'review_suggestions': [person(), second]})
                self.assertIsNone(result['selected_candidate_id'])
                self.assertTrue(result['selection_required'])
                self.assertIsNone(result['fields']['fio_raw']['value'])
                self.assertEqual(len(result['candidates']), 2)

    def test_existing_authoritative_identity_or_identifier_cannot_mix_with_candidate(self):
        for anchor in ({'inn': field('111111111111', source='debtor_role:v1')}, {'fio_raw': field('Примеров Пример Примерович', source='debtor_role:v1')}, {'snils': field('synthetic-id', source='owner-feedback')}):
            result = self.project({**anchor, 'review_suggestions': [person()]})
            self.assertIsNone(result['selected_candidate_id'])
            for name, value in anchor.items():
                self.assertEqual(result['fields'][name]['value'], value['value'])

    def test_legal_and_ip_have_canonical_types_without_person_or_inn_length_guessing(self):
        for kind, name in (('legal_entity', 'ООО «Синтетический образец»'), ('sole_proprietor', 'ИП Тестов Тест Тестович')):
            result = self.project({'review_suggestions': [entity(kind, name)]})
            self.assertEqual(result['entity_kind'], kind)
            target = 'legal_name' if kind == 'legal_entity' else 'debtor_name'
            self.assertEqual(result['fields'][target]['value'], name)
            self.assertNotIn('fio_raw', result['applicable_fields'])
            self.assertNotIn('snils', result['applicable_fields'])
        self.assertIsNone(self.project({'inn': field('0000000000')})['entity_kind'])

    def test_owner_clears_and_explicit_type_hide_incompatible_raw_anchor(self):
        observed = {'review_suggestions': [person()], 'entity_kind': field('person'), 'ogrn': field('hidden-company-id')}
        current = field_map(observed)
        current['fio_raw'] = field('', source='owner-feedback')
        current['entity_kind'] = field('', source='owner-feedback')
        result = projection.project(observed, current)
        self.assertEqual(result['fields']['fio_raw']['value'], '')
        self.assertIsNone(result['entity_kind'])
        result = self.project(observed)
        self.assertIsNotNone(result['selected_candidate_id'])
        self.assertEqual(result['fields']['ogrn']['value'], 'hidden-company-id')
        self.assertNotIn('ogrn', result['applicable_fields'])

    def test_unknown_and_malformed_confidence_remain_reviewable(self):
        self.assertEqual(self.project({})['applicable_fields'], list(projection.COMMON))
        item = person(); item['fields']['inn']['confidence'] = 'not-a-number'
        result = self.project({'review_suggestions': [item]})
        self.assertEqual(result['fields']['inn']['confidence'], 0.)

    def test_research_projection_import_never_loads_production_settings_database(self):
        code = 'import sys;import sro_robot.research.review_store;assert "app.settings" not in sys.modules;assert "app.db.base" not in sys.modules'
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, 'Local review import must not initialize production settings/database')


class ProjectionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = ReviewStore(root / 'review')
        self.asset = root / 'original.pdf'; self.asset.write_bytes(b'%PDF synthetic projection\n%%EOF\n')
        self.key = hashlib.sha256(self.asset.read_bytes()).hexdigest()
        self.capture = {'input_sha256': self.key, 'capture': {'revision': 'a' * 40, 'source_dirty': False, 'dirty_sha256': '0' * 64, 'runtime': {}, 'accepted_parser_identity': True}, 'observed_pipeline': {'review_suggestions': [person()]}, 'primary_pages': [{'page': 1}]}
        self.store.import_capture(self.asset, self.capture)

    def tearDown(self):
        self.temp.cleanup()

    def body(self, doc=None, **changes):
        doc = doc or self.store.get_document(self.key, expose=False)
        result = {'action_id': 'synthetic-review-action', 'expected_revision': doc['revision'], 'expected_snapshot_sha256': doc['snapshot_sha256'], 'reviewed_fields': doc['review_projection']['applicable_fields'], 'decision': 'approved', 'corrected_fields': {}, 'incorrect_fields': [], 'comment': ''}
        result.update(changes)
        return result

    def test_display_version_binds_old_tabs_without_mutating_raw_capture(self):
        old = self.store.get_document(self.key, expose=False)
        with patch.object(projection, 'PROJECTION_VERSION', 'owner-review-card:vNEXT'):
            changed = self.store.get_document(self.key, expose=False)
            self.assertNotEqual(changed['snapshot_sha256'], old['snapshot_sha256'])
            self.assertEqual(changed['raw_capture_sha256'], old['raw_capture_sha256'])
            with self.assertRaises(ReviewConflict):
                self.store.apply_review(self.key, self.body(old))
        self.assertEqual(self.store.stats()['pending_review'], 1)

    def test_identical_capture_in_different_store_cannot_consume_pending_action(self):
        original = self.store.get_document(self.key, expose=False)
        other = ReviewStore(Path(self.temp.name) / 'different-store')
        other.import_capture(self.asset, self.capture)
        different = other.get_document(self.key, expose=False)
        self.assertEqual(original['raw_capture_sha256'], different['raw_capture_sha256'])
        self.assertNotEqual(original['snapshot_sha256'], different['snapshot_sha256'])
        with self.assertRaises(ReviewConflict):
            other.apply_review(self.key, self.body(original))
        restarted = ReviewStore(self.store.root)
        self.assertEqual(restarted.get_document(self.key, expose=False)['snapshot_sha256'], original['snapshot_sha256'])

    def test_scope_and_immutable_projection_audit_match_values_owner_saw(self):
        old = self.store.get_document(self.key, expose=False)
        self.store.apply_review(self.key, self.body(old, decision='rejected', incorrect_fields=['address'], corrected_fields={'address': 'г. Тестоград, д. 1, кв. 4'}, comment='Synthetic apartment omission'))
        with self.store.connection() as db:
            audit = json.loads(db.execute('SELECT projection_json FROM review_projections').fetchone()[0])
            diagnostic = db.execute('SELECT original_value_json FROM diagnostics').fetchone()[0]
            raw = db.execute('SELECT capture_json FROM documents').fetchone()[0]
        self.assertEqual(audit['fields']['address']['value'], old['fields']['address']['value'])
        self.assertEqual(json.loads(diagnostic), old['fields']['address']['value'])
        self.assertNotIn('кв. 4', raw)
        corrected = self.store.get_document(self.key, expose=False)
        self.assertEqual(corrected['fields']['address']['value'], 'г. Тестоград, д. 1, кв. 4')
        self.assertEqual(corrected['fields']['address']['source'], 'owner-feedback')

    def test_empty_scope_or_hidden_correction_cannot_be_approved(self):
        with self.assertRaises(ReviewConflict):
            self.store.apply_review(self.key, self.body(reviewed_fields=[]))
        with self.assertRaises(ReviewError):
            self.store.apply_review(self.key, self.body(decision='rejected', incorrect_fields=['legal_name'], corrected_fields={'legal_name': 'hidden'}, comment='synthetic'))

    def test_approved_person_does_not_confirm_hidden_company_fields(self):
        with self.store.connection(write=True) as db:
            fields = json.loads(db.execute('SELECT effective_fields_json FROM documents').fetchone()[0])
            fields['ogrn'] = field('hidden-company-id')
            fields['entity_kind'] = field('person')
            db.execute('UPDATE documents SET effective_fields_json=?', (encoded(fields),))
        self.store.apply_review(self.key, self.body(reviewed_fields=list(FIELDS)))
        with self.store.connection() as db:
            states = json.loads(db.execute('SELECT field_states_json FROM reviews').fetchone()[0])
        self.assertEqual(states['ogrn'], 'unreviewed')
        self.assertEqual(states['fio_raw'], 'unreviewed')
        self.assertEqual(states['last_name'], 'owner-confirmed-present')

    def test_old_accepted_action_replays_before_new_scope_and_hash_requirements(self):
        legacy = self.body(); legacy.pop('reviewed_fields')
        legacy['expected_snapshot_sha256'] = self.store.get_document(self.key, expose=False)['raw_capture_sha256']
        request_sha = sha(encoded({'document_id': self.key, **legacy}).encode())
        with self.store.connection(write=True) as db:
            row = db.execute('SELECT snapshot_sha256 FROM documents').fetchone()
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (legacy['action_id'], self.key, request_sha, 1, 'approved', row[0], '{}', '[]', '', 'synthetic-time', '{}', '{}', 2))
            db.execute("UPDATE documents SET status='approved',revision=2")
        self.assertTrue(self.store.apply_review(self.key, legacy)['replayed'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
