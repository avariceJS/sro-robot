"""Independent synthetic references; no office samples, OCR or real services."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.dev.quality.extraction_check import capture_one, check, compare


class ExtractionQualityTests(unittest.TestCase):
    def test_independently_authored_reference_values(self):
        records = json.loads((Path(__file__).parent / "fixtures/extraction_quality_cases.json").read_text())
        result = check(records)
        self.assertEqual(result["counts"]["wrong"], 0)
        self.assertEqual(result["counts"]["missing"], 0)
        self.assertEqual(result["documents"], 13)
        self.assertEqual(result["counts"]["correct"], 20)
        self.assertEqual(result["counts"]["metadata_wrong"], 0)

    def test_unverified_ocr_never_contributes_to_accuracy(self):
        result = check([dict(text="Должник: Иванов Иван Иванович")])
        self.assertEqual(result["counts"]["unverified_documents"], 1)
        self.assertEqual(result["counts"]["correct"], 0)
        with self.assertRaises(ValueError):
            check([dict(text="Иванов Иван Иванович", expected={"fio_raw": "Иванов Иван Иванович"})])

    def test_comparison_includes_warning_source_and_full_staff_suggestions(self):
        a = {"results": [{"snapshot": {"inn": {"value": "synthetic", "warning": "review", "source": "candidate"},
                "review_suggestions": [{"owner": "synthetic-person", "fields": {"address": "synthetic-address"}}]}}]}
        b = {"results": [{"snapshot": {"inn": {"value": "synthetic", "warning": None, "source": "role"}, "review_suggestions": []}}]}
        result = compare(a, b)
        self.assertEqual(result["warnings_removed"], 1)
        self.assertEqual(result["source_changes"], 1)
        self.assertEqual(result["changed_or_lost_suggestions"], 1)
        self.assertEqual(result["changed_fields"], 0)

    def test_flat_attribute_values_partial_flag_and_location_are_compared(self):
        a = {"results": [{"snapshot": {"address": "synthetic full", "address_partial": False,
                                       "address_location": [0, 14]}}]}
        b = {"results": [{"snapshot": {"address": "synthetic", "address_partial": True,
                                       "address_location": [0, 9]}}]}
        result = compare(a, b)
        self.assertEqual(result["changed_fields"], 1)
        self.assertEqual(result["metadata_changes"], 1)
        self.assertEqual(result["source_changes"], 1)

    def test_capture_keeps_accepted_card_inputs_not_last_rejected_attempt(self):
        import pytesseract
        from sro_robot.core import extract
        original = extract.parse
        accepted_text = "Должник: Иванов Иван Иванович"
        def parse_file(*args, **kwargs):
            pytesseract.pytesseract.run_tesseract()
            accepted = extract.parse([accepted_text])
            extract.parse(["Должник: Петров Петр Петрович"])
            return accepted
        with patch.object(extract, "parse_file", side_effect=parse_file), patch.object(
            pytesseract.pytesseract, "run_tesseract", return_value=None,
        ):
            result = capture_one(Path("synthetic.png"))
        self.assertEqual(result["variants"], [accepted_text])
        self.assertEqual(result["capture"]["parser_attempts"], 2)
        self.assertIs(extract.parse, original)
