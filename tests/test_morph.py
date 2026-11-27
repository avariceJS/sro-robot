"""Synthetic personal-name agreement, including dictionary indeclinable surnames."""
import unittest

from sro_robot.core.morph import normalize_fio, person_triplet


class PersonTripletTests(unittest.TestCase):
    def test_fio_normalization_respects_gender_and_case_agreement(self):
        self.assertEqual(normalize_fio('Макетова Анна Петровна'), ('Макетова', 'Анна', 'Петровна'))
        self.assertEqual(normalize_fio('Макетова Ивана Петровича'), ('Макетов', 'Иван', 'Петрович'))

    def test_hyphenated_name_components_retain_each_initial(self):
        for source, expected in (
            (('Иванов-Петров', 'Иван', 'Петрович'), ('Иванов-Петров', 'Иван', 'Петрович')),
            (('Ивановой-Петровой', 'Анны', 'Сергеевны'), ('Иванова-Петрова', 'Анна', 'Сергеевна')),
            (('Петровой', 'Анны-Марии', 'Сергеевны'), ('Петрова', 'Анна-Мария', 'Сергеевна')),
        ):
            with self.subTest(source=source):
                self.assertEqual(person_triplet(source), expected)
                self.assertEqual(normalize_fio(' '.join(source)), expected)

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
