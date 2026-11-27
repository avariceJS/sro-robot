"""Optional appointment schedule evidence, using synthetic readings only."""
import unittest
from copy import deepcopy
from unittest.mock import patch

from sro_robot.research.review_projection import appointment_projection
from sro_robot.research.review_schema import CAPTION_RESCAN_PROTOCOL, HEARING_RESCAN_PROTOCOL


PREFIX = ('ОПРЕДЕЛЕНИЕ\nДело № А99-4321/2099\n5 мая 2099 г.\n'
          'Рассмотрев заявление о признании гражданина банкротом.\n')
PARTIAL = PREFIX + 'ОПРЕДЕЛИЛ:\nНазначить судебное заседание в 09:20.'
FULL = PREFIX + '\f\nОПРЕДЕЛИЛ:\nНазначить судебное заседание на 21.06.2099 в 09:20.'
SOURCE_SHA = 'a' * 64
CAPTION = ('Арбитражный суд Учебной области\nОПРЕДЕЛЕНИЕ\n'
           'Дело № А99-4321/2099\n5 мая 2099 г.\n'
           'Судья: Макетова М. Л.\n'
           'Должник: Примерова Елена Максовна (ИНН 990014232035, '
           'СНИЛС 112-233-445 95, дата рождения: 17.04.1970).\n'
           'Рассмотрев заявление Примеровой Елены Максовны '
           'о признании себя несостоятельной (банкротом).\n')


def capture(reading=FULL, **changes):
    return {'variants': [PARTIAL], 'input_sha256': SOURCE_SHA,
            'capture': {'protocol': HEARING_RESCAN_PROTOCOL},
            'appointment_hearing_readings': [reading], 'appointment_hearing_source_sha256': SOURCE_SHA,
            'readings': [{'config': '--psm 4', 'text': reading}], **changes}


def caption_capture(captions=(CAPTION, CAPTION), **changes):
    return {'variants': [PARTIAL], 'input_sha256': SOURCE_SHA,
            'capture': {'protocol': CAPTION_RESCAN_PROTOCOL},
            'appointment_caption_readings': [text.strip() for text in captions], 'appointment_caption_source_sha256': SOURCE_SHA,
            'readings': [{'config': config, 'text': text, 'purpose': 'appointment-caption',
                          'crop': 'top-three-fifths', 'transform': 'grayscale-autocontrast'}
                         for config, text in zip(('--psm 6', '--psm 11'), captions)], **changes}


class AutomatedAppointmentCaptionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.baseline = appointment_projection({'variants': [PARTIAL]})

    def test_same_original_matching_crops_add_fields_and_preserve_every_prior_span(self):
        captured = caption_capture()
        actual = appointment_projection(captured)
        self.assertGreater(len(actual['fields']), len(self.baseline['fields']))
        for name, field in self.baseline['fields'].items():
            self.assertEqual(actual['fields'][name], field)
        for name in ('court', 'judge_name', 'applicant', 'debtor_name', 'debtor_inn', 'debtor_snils', 'debtor_birth_date'):
            field = actual['fields'][name]
            self.assertEqual(field['source_variant'], 1)
            self.assertTrue(CAPTION[field['source_start']:field['source_end']])
        self.assertEqual(actual['appointment_event'], self.baseline['appointment_event'])
        self.assertFalse(actual['approval_allowed'])

    def test_missing_malformed_or_mismatched_original_hash_rejects_automatic_crops(self):
        for changes in ({'appointment_caption_source_sha256': None}, {'input_sha256': None},
                        {'appointment_caption_source_sha256': 'b' * 64},
                        {'appointment_caption_source_sha256': 'g' * 64, 'input_sha256': 'g' * 64},
                        {'appointment_caption_source_sha256': 'A' * 64, 'input_sha256': 'A' * 64},
                        {'appointment_caption_source_sha256': 'a' * 63, 'input_sha256': 'a' * 63}):
            with self.subTest(changes=changes):
                self.assertEqual(appointment_projection(caption_capture(**changes)), self.baseline)

    def test_protocol_cannot_downgrade_modern_crop_metadata_to_legacy(self):
        for metadata in (None, {}, {'protocol': 'standard'}, {'protocol': HEARING_RESCAN_PROTOCOL}):
            with self.subTest(metadata=metadata):
                self.assertEqual(appointment_projection(caption_capture(capture=metadata)), self.baseline)

    def test_only_recorded_crop_tail_with_matching_config_transform_and_text_is_used(self):
        normal = caption_capture()['readings']
        altered_text = deepcopy(normal)
        altered_text[-1]['text'] += '\nAdditional synthetic OCR'
        for records in (None, [], [None], normal[:-1], list(reversed(normal)),
                        [{**normal[0], 'config': '--psm 4'}, normal[1]],
                        [{**normal[0], 'purpose': 'full-reading'}, normal[1]],
                        [{**normal[0], 'crop': 'full-image'}, normal[1]],
                        [{**normal[0], 'transform': None}, normal[1]], altered_text,
                        [{**normal[0], 'config': '--psm 11'}],
                        [*normal, {'config': '--psm 3', 'text': CAPTION}]):
            with self.subTest(records=records):
                self.assertEqual(appointment_projection(caption_capture(readings=records)), self.baseline)
        self.assertEqual(appointment_projection(caption_capture(appointment_hearing_readings=[FULL])), self.baseline)
        for captions in (None, 'invalid', [None], [CAPTION.strip()] * 3, [''], [' ' + CAPTION.strip()]):
            with self.subTest(captions=captions):
                self.assertEqual(appointment_projection(caption_capture(appointment_caption_readings=captions)), self.baseline)

    def test_primary_and_each_accepted_crop_need_a_unique_parsed_common_case(self):
        for text in (CAPTION.replace('А99-4321', 'А99-1234'),
                     CAPTION.replace('Дело № А99-4321/2099\n', ''),
                     CAPTION + '\nДело № А99-1234/2099',
                     CAPTION.replace('Дело № А99-4321/2099\n', '') + '\nСсылка на дело А99-4321/2099.'):
            with self.subTest(text=text):
                self.assertEqual(appointment_projection(caption_capture((text,))), self.baseline)
        for primary in (PARTIAL.replace('Дело № А99-4321/2099\n', ''),
                        PARTIAL + '\nСсылка на дело А99-1234/2099.'):
            with self.subTest(primary=primary):
                baseline = appointment_projection({'variants': [primary]})
                self.assertEqual(appointment_projection(caption_capture(variants=[primary])), baseline)

    def test_partial_ocr_success_and_individually_rejected_crop_preserve_evidence_slots(self):
        captured = caption_capture((CAPTION,))
        self.assertIn('court', appointment_projection(captured)['fields'])
        blank = {**captured['readings'][0], 'text': '\n\f'}
        captured['readings'][0]['config'] = '--psm 11'
        captured['readings'].insert(0, blank)
        self.assertIn('court', appointment_projection(captured)['fields'])
        captured = caption_capture((CAPTION,))
        blank = {**captured['readings'][0], 'config': '--psm 11', 'text': '\n\f'}
        captured['readings'].append(blank)
        captured['readings'][0]['text'] = '\n' + CAPTION + '\n\f'
        self.assertIn('court', appointment_projection(captured)['fields'])
        wrong = CAPTION.replace('А99-4321', 'А99-1234')
        actual = appointment_projection(caption_capture((wrong, CAPTION)))
        self.assertEqual(actual['fields']['court']['source_variant'], 2)
        actual = appointment_projection(caption_capture((CAPTION,), variants=['', PARTIAL]))
        self.assertEqual(actual['fields']['court']['source_variant'], 2)
        self.assertEqual(actual['fields']['hearing_time']['source_variant'], 1)

    def test_conflicting_ownership_or_prior_fields_retains_baseline(self):
        primary = PARTIAL.replace('Рассмотрев', 'Должник: Примерова Елена Максовна (ИНН 990014232035).\nРассмотрев')
        baseline = appointment_projection({'variants': [primary]})
        for text in (CAPTION.replace('Примерова Елена Максовна', 'Макетова Лидия Семёновна'),
                     CAPTION.replace('990014232035', '990004321007')):
            with self.subTest(text=text):
                self.assertEqual(appointment_projection(caption_capture((text,), variants=[primary])), baseline)

    def test_supplement_cannot_change_schedule_or_any_existing_field_metadata(self):
        from sro_robot.core.appointment_extract import extract_appointment_fields
        baseline = extract_appointment_fields([PARTIAL])
        for name, key, change in (('hearing_time', 'value', '10:30'),
                                  ('case_number', 'source_variant', 1),
                                  ('hearing_time', 'source_start', 1)):
            proposed = deepcopy(baseline)
            proposed[name][key] = change
            with self.subTest(name=name, key=key), patch(
                    'sro_robot.core.appointment_extract.extract_appointment_fields',
                    side_effect=[baseline, extract_appointment_fields([CAPTION]),
                                 extract_appointment_fields([CAPTION]), proposed]):
                self.assertEqual(appointment_projection(caption_capture()), self.baseline)

    def test_automatic_name_additions_need_complete_party_identity(self):
        for label in ('Заявитель: конкурсный управляющий', 'Заявитель: Арбитражный конкурсный управляющий',
                      'Должник: ИНН 990014232035', 'Должник: Дата Подачи Заявления',
                      'Должник: Дата Подачи Заявления (ИНН 990014232035)',
                      '«\nДолжник: Примерова Елена Максовна (ИНН 990014232035).\n»'):
            text = PREFIX + label + '\n'
            with self.subTest(label=label):
                self.assertEqual(appointment_projection(caption_capture((text,))), self.baseline)
        for name in ('ООО «Синтетический заявитель»', 'ИП Примеров Марк Львович', 'Примеров Марк Львович'):
            text = PREFIX + 'Заявитель: ' + name + '\n'
            with self.subTest(name=name):
                self.assertEqual(appointment_projection(caption_capture((text,)))['fields']['applicant']['value'], name)

    def test_legacy_manual_captions_retain_their_existing_behavior(self):
        text = PREFIX + 'Заявитель: конкурсный управляющий\n'
        actual = appointment_projection({'variants': [PARTIAL], 'appointment_caption_readings': [text]})
        self.assertEqual(actual['fields']['applicant']['value'], 'конкурсный управляющий')


class AppointmentSupplementProjectionTests(unittest.TestCase):
    def test_schedule_supplement_uses_its_own_evidence_slot_after_captions(self):
        captured = capture(appointment_caption_readings=[PREFIX, PREFIX])
        projection = appointment_projection(captured)
        self.assertTrue(projection['approval_allowed'])
        self.assertEqual(projection['appointment_event'], 'hearing')
        self.assertEqual(projection['missing_required_fields'], [])
        date = projection['fields']['hearing_date']
        self.assertEqual(date['source_variant'], 3)
        self.assertEqual(date['source_page'], 2)
        self.assertEqual(FULL[date['source_start']:date['source_end']], '21.06.2099')
        self.assertEqual(projection['fields']['hearing_time']['source_variant'], 0)

    def test_blank_primary_slots_are_preserved_before_supplemental_offsets(self):
        projection = appointment_projection(capture(variants=['', PARTIAL], appointment_caption_readings=[PREFIX]))
        self.assertTrue(projection['approval_allowed'])
        self.assertEqual(projection['fields']['hearing_time']['source_variant'], 1)
        date = projection['fields']['hearing_date']
        self.assertEqual(date['source_variant'], 3)
        self.assertEqual(FULL[date['source_start']:date['source_end']], '21.06.2099')

    def test_invalid_primary_container_cannot_shift_supplemental_evidence(self):
        for variants in ([PARTIAL] * 6, [None, PARTIAL], ['x' * 200_001, PARTIAL]):
            with self.subTest(slots=len(variants)):
                projection = appointment_projection(capture(variants=variants))
                self.assertEqual(projection['fields'], {})
                self.assertFalse(projection['approval_allowed'])

    def test_wrong_case_incomplete_or_conflicting_supplement_cannot_allow_approval(self):
        baseline = appointment_projection({'variants': [PARTIAL]})
        for readings in (
            [FULL.replace('А99-4321', 'А99-1234')],
            [FULL.replace('Дело № А99-4321/2099\n', '')],
            [FULL.replace('09:20', '10:30')],
            [FULL.replace('в 09:20', '')],
            [FULL.replace('5 мая 2099', '6 мая 2099')],
            [FULL, FULL], [None], 'invalid',
        ):
            with self.subTest(readings=readings):
                reading = readings[0] if isinstance(readings, list) and len(readings) == 1 and isinstance(readings[0], str) else FULL
                actual = appointment_projection(capture(reading, appointment_hearing_readings=readings))
                self.assertEqual(actual, baseline)
                self.assertFalse(actual['approval_allowed'])

    def test_complete_primary_and_practitioner_event_stay_unchanged(self):
        practitioner = PREFIX + 'ОПРЕДЕЛИЛ:\nУтвердить финансовым управляющим Макетова Марка Львовича.'
        for primary in (FULL, practitioner):
            with self.subTest(primary=primary):
                baseline = appointment_projection({'variants': [primary]})
                supplemented = appointment_projection(capture(FULL.replace('09:20', '10:30'), variants=[primary]))
                self.assertEqual(supplemented, baseline)

    def test_supplement_requires_same_original_and_final_psm4_capture_record(self):
        baseline = appointment_projection({'variants': [PARTIAL]})
        for changes in (
            {'capture': None}, {'capture': {'protocol': 'standard'}},
            {'appointment_hearing_source_sha256': None}, {'input_sha256': None},
            {'appointment_hearing_source_sha256': 'b' * 64},
            {'appointment_hearing_source_sha256': 'A' * 64, 'input_sha256': 'A' * 64},
            {'appointment_hearing_source_sha256': 'g' * 64, 'input_sha256': 'g' * 64},
            {'appointment_hearing_source_sha256': 'a' * 63, 'input_sha256': 'a' * 63},
            {'readings': []}, {'readings': None}, {'readings': [None]},
            {'readings': [{'config': '--psm 6', 'text': FULL}]},
            {'readings': [{'config': '--psm 4', 'text': 'different synthetic reading'}]},
            {'readings': [{'config': '--psm 4', 'text': FULL}, {'config': '--psm 6', 'text': FULL}]},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(appointment_projection(capture(**changes)), baseline)

    def test_psm4_record_may_retain_outer_whitespace_without_shifting_saved_spans(self):
        projection = appointment_projection(capture(readings=[{'config': '--psm 4', 'text': '\n' + FULL + '\n\f'}]))
        self.assertTrue(projection['approval_allowed'])
        date = projection['fields']['hearing_date']
        self.assertEqual(FULL[date['source_start']:date['source_end']], '21.06.2099')


if __name__ == '__main__':
    unittest.main()
