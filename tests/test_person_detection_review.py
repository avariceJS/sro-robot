"""Synthetic parser-to-staff-form checks for the general fallback."""
from dataclasses import asdict
from types import SimpleNamespace
import unittest

from app.services.fields import review_fields
from sro_robot.core.extract import add_review_candidates, parse
from sro_robot.core.models import card_to_fields
from sro_robot.research.review_projection import expert_projection, project
from sro_robot.research.review_store import field_map


class PersonDetectionReviewTests(unittest.TestCase):
    def test_literal_operative_name_keeps_exact_span_without_assigning_components(self):
        name = 'Фальштестенко Анну Петровну'
        text = (
            'РЕШЕНИЕ\nДело № А99-123/2099\n25.11.2099\n'
            'Рассмотрев дело о банкротстве гражданина, суд\nРЕШИЛ:\n'
            f'Признать {name} (СНИЛС: 112-233-445-95; адрес регистрации: г. Тестоград) '
            'несостоятельной (банкротом).'
        )
        card = parse([text])
        add_review_candidates(card, [text])
        suggestion, = [i for i in card.review_suggestions if i.get('role') == 'debtor']
        field = suggestion['fields']['fio']
        self.assertEqual(field['value'], name)
        self.assertEqual(text[field['source_start']:field['source_end']], name)
        self.assertEqual(suggestion['copyable_fields'], [])
        self.assertEqual(set(suggestion['fields']), {'fio'})
        observed = asdict(card)
        projection = project(observed, field_map(observed))
        self.assertEqual(projection['fields']['fio_raw']['value'], name)
        self.assertIsNone(projection['fields']['inn']['value'])
        self.assertIsNone(card.fio_raw.value)
        self.assertEqual(expert_projection({'variants': [text], 'observed_pipeline': observed}),
                         (None, 'needs_extraction'))

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
