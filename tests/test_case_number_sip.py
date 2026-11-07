"""Synthetic official case-family shapes; no original court identifiers."""
import unittest
from sro_robot.core.extract import find_case_number, parse


class SipCaseNumberTests(unittest.TestCase):
    def test_sip_one_two_three_and_seven_digit_serials_have_exact_source(self):
        for serial in ("7", "42", "123", "1234567"):
            with self.subTest(serial=serial):
                expected = f"СИП-{serial}/2026"
                text = f"РЕШЕНИЕ\nДело № {expected}\n14 ноября 2026 года\f"
                field = parse([text]).case_number
                self.assertEqual(find_case_number(text), expected)
                self.assertEqual(field.value, expected)
                self.assertEqual(text[field.source_start:field.source_end], expected)
                self.assertEqual(field.source_page, 1)
                self.assertIsNone(field.warning)

    def test_primary_sip_caption_precedes_cited_arbitrazh_case_and_keeps_collision(self):
        text = "РЕШЕНИЕ\nДело № СИП-42/2026\nИстец ссылается на дело № А77-123/2025.\f"
        field = parse([text]).case_number
        self.assertEqual(field.value, "СИП-42/2026")
        self.assertEqual(text[field.source_start:field.source_end], "СИП-42/2026")
        self.assertEqual(field.source_page, 1)
        self.assertIsNotNone(field.warning)

    def test_second_physical_page_and_repeated_occurrence_use_existing_source_order(self):
        text = "Синтетическая обложка\fДело № СИП-42/2026\nПовтор: СИП-42/2026."
        field = parse([text]).case_number
        self.assertEqual(field.value, "СИП-42/2026")
        self.assertEqual(field.source_start, text.index("СИП-42/2026"))
        self.assertEqual(text[field.source_start:field.source_end], field.value)
        self.assertEqual(field.source_page, 2)
        self.assertIsNone(field.warning)

    def test_sip_disagreement_across_variants_retains_warning(self):
        field = parse(["Дело № СИП-42/2026", "Дело № СИП-43/2026"]).case_number
        self.assertIsNotNone(field.warning)

    def test_unlabelled_compact_family_and_existing_spaced_label_are_supported(self):
        self.assertEqual(find_case_number("Ссылка: СИП-42/2026."), "СИП-42/2026")
        self.assertEqual(find_case_number("Дело № СИП - 42 / 2026"), "СИП-42/2026")
        self.assertIsNone(find_case_number("Ссылка: А77 - 42 / 2026."))

    def test_invalid_serial_year_unknown_prefix_and_noncase_numbers_are_not_substrings(self):
        for text in ("Дело № СИП-12345678/2026", "СИП-12345678/2026", "Дело № СИП-42/26",
                     "Дело № СИП-42/202", "Дело № СИП-42/20260", "Дело № А77-42/20260",
                     "Дело № СИПП-42/2026", "СИП77-42/2026", "SIP-42/2026", "XСИП-42/2026",
                     "Платёж № 42/2026", "14.11.2026"):
            with self.subTest(text=text):
                self.assertIsNone(find_case_number(text))


if __name__ == "__main__":
    unittest.main()
