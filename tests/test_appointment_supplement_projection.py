"""Optional appointment schedule evidence, using synthetic readings only."""
import unittest

from sro_robot.research.review_projection import appointment_projection
from sro_robot.research.review_schema import HEARING_RESCAN_PROTOCOL


PREFIX = ('ОПРЕДЕЛЕНИЕ\nДело № А99-4321/2099\n5 мая 2099 г.\n'
          'Рассмотрев заявление о признании гражданина банкротом.\n')
PARTIAL = PREFIX + 'ОПРЕДЕЛИЛ:\nНазначить судебное заседание в 09:20.'
FULL = PREFIX + '\f\nОПРЕДЕЛИЛ:\nНазначить судебное заседание на 21.06.2099 в 09:20.'
SOURCE_SHA = 'a' * 64


def capture(reading=FULL, **changes):
    return {'variants': [PARTIAL], 'input_sha256': SOURCE_SHA,
            'capture': {'protocol': HEARING_RESCAN_PROTOCOL},
            'appointment_hearing_readings': [reading], 'appointment_hearing_source_sha256': SOURCE_SHA,
            'readings': [{'config': '--psm 4', 'text': reading}], **changes}


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
