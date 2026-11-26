"""Synthetic personal-name agreement, including dictionary indeclinable surnames."""
import unittest

from sro_robot.core.morph import person_triplet


class PersonTripletTests(unittest.TestCase):
    def test_known_indeclinable_surname_preserves_spelling_for_both_genders(self):
        for source, expected in (
            (('Дюма', 'Анна', 'Петровна'), ('Дюма', 'Анна', 'Петровна')),
            (('Дюма', 'Анны', 'Петровны'), ('Дюма', 'Анна', 'Петровна')),
            (('Дюма', 'Петру', 'Ивановичу'), ('Дюма', 'Пётр', 'Иванович')),
        ):
            with self.subTest(source=source):
                self.assertEqual(person_triplet(source), expected)

    def test_indeclinable_exception_keeps_unknown_and_conflicting_names_rejected(self):
        for source in (
            ('Фальштестенко', 'Анна', 'Петровна'),
            ('Дюма', 'Анна', 'Петрович'),
            ('Дюма', 'Анну', 'Петровны'),
            ('Иванов', 'Анна', 'Петровна'),
        ):
            with self.subTest(source=source):
                self.assertIsNone(person_triplet(source))
