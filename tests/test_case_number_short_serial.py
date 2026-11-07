"""Independently authored case-number shapes, never original court records."""
import unittest

from sro_robot.core.extract import find_case_number, parse


class ShortCaseSerialTests(unittest.TestCase):
    def test_one_two_three_and_seven_digit_serials_keep_exact_source(self):
        for serial in ("7", "42", "123", "1234567"):
            with self.subTest(serial=serial):
                expected = f"А77-{serial}/2026"
                text = f"ОПРЕДЕЛЕНИЕ\nДело № {expected}\n14 ноября 2026 года\f"
                field = parse([text]).case_number
                self.assertEqual(find_case_number(text), expected)
                self.assertEqual(field.value, expected)
                self.assertEqual(text[field.source_start:field.source_end], expected)
                self.assertEqual(field.source_page, 1)
                self.assertIsNone(field.warning)

    def test_unlabelled_short_shape_preserves_existing_fallback(self):
        self.assertEqual(find_case_number("Ссылка: А77-42/2026."), "А77-42/2026")

    def test_second_page_keeps_exact_physical_source_slot(self):
        text = "Синтетическая первая страница без номера\fДело № А77-42/2026"
        field = parse([text]).case_number
        self.assertEqual(field.value, "А77-42/2026")
        self.assertEqual(field.source_page, 2)
        self.assertEqual(text[field.source_start:field.source_end], field.value)

    def test_cross_page_short_case_disagreement_retains_warning(self):
        text = "Дело № А77-42/2026\fДело № А77-43/2026"
        field = parse([text]).case_number
        self.assertEqual(field.value, "А77-42/2026")
        self.assertIsNotNone(field.warning)

    def test_recognized_case_only_activates_existing_missing_inn_advisory(self):
        card = parse(["Дело № А77-42/2026\nДолжник: Иванов Иван Иванович"])
        self.assertEqual(card.case_number.value, "А77-42/2026")
        self.assertIsNone(card.inn.value)
        self.assertIn("номер дела", card.inn.warning)

    def test_long_serial_and_noncase_date_or_number_are_not_substrings(self):
        for text in ("Дело № А77-12345678/2026", "А77-12345678/2026", "14.11.2026",
                     "Платёж № 77-42/2026", "XA77-42/2026", "А777-42/2026", "А77-42/26"):
            with self.subTest(text=text):
                self.assertIsNone(find_case_number(text))


if __name__ == "__main__":
    unittest.main()
