"""Synthetic parser-to-staff-form checks for the general fallback."""
from types import SimpleNamespace
import unittest

from app.services.fields import review_fields
from sro_robot.core.extract import parse
from sro_robot.core.models import card_to_fields


class PersonDetectionReviewTests(unittest.TestCase):
    def test_unanchored_details_reach_editable_review_with_source_and_warning(self):
        text = (
            'Анкетные сведения: Иванова Анна Сергеевна; личный номер 772881234508; '
            '112-233-445 95; г. Макетный, ул. Учебная, д. 7, кв. 1.'
        )
        card = parse([text])
        recognized = card_to_fields(card)
        recognized['_review_suggestions_v1'] = card.review_suggestions
        task = SimpleNamespace(scenario='person_card', recognized=recognized, values={})
        rows = {row.attr: row for row in review_fields(task)}
        expected = {'last_name':'Иванова', 'first_name':'Анна', 'middle_name':'Сергеевна',
                    'inn':'772881234508', 'snils':'112-233-445 95',
                    'address':'г. Макетный, ул. Учебная, д. 7, кв. 1'}
        for attr, value in expected.items():
            with self.subTest(attr=attr):
                self.assertEqual(rows[attr].value, value)
                self.assertEqual(rows[attr].evidence_status, 'uncertain')
                self.assertTrue(rows[attr].warning)
                self.assertIsNotNone(rows[attr].source_start)
                self.assertIsNotNone(rows[attr].source_end)
                self.assertTrue(text[rows[attr].source_start:rows[attr].source_end])
        self.assertFalse(rows['address_kind'].value)
        self.assertIsNone(card.fio_raw.value)
        self.assertIsNone(card.inn.value)
