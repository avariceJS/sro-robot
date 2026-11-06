"""Synthetic checks for defect-driven OCR and retained review authority."""
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image

from sro_robot.core import extract, ocr
from sro_robot.core.models import card_to_fields


class RecoveryPolicyTests(unittest.TestCase):
    def test_absent_requisites_without_labels_do_not_request_another_pass(self):
        self.assertIsNone(extract._field_recovery_choice([
            "Должник: Иванов Иван Иванович. Сведения о рассмотрении заявления."
        ]))

    def test_damaged_labelled_identifier_requests_original_detail(self):
        self.assertEqual(extract._field_recovery_choice([
            "Должник: Иванов Иван Иванович, ИНН 12345"
        ]), (6, True, "labelled_identifier_not_extracted"))

    def test_numbered_apartment_does_not_trigger_recovery(self):
        for apartment in ("кв. 17", "кв 17", "квартира № 17", "офис: 17"):
            with self.subTest(apartment=apartment):
                self.assertIsNone(extract._field_recovery_choice([
                    "Адрес: г. Учебный, ул. Тестовая, д. 5, " + apartment
                ]))

    def test_apartment_label_without_digits_requests_original_detail(self):
        self.assertEqual(extract._field_recovery_choice([
            "Должник: Иванов Иван Иванович. Адрес: г. Учебный, ул. Тестовая, д. 5, кв."
        ]), (6, True, "apartment_token_without_number"))

    def test_selected_recovery_runs_once_on_original_with_own_source_variant(self):
        original = Image.new("L", (100, 100))
        prepared = Image.new("L", (200, 200))
        collector = unittest.mock.Mock()
        budget = ocr.OcrBudget()
        try:
            with (
                patch.object(ocr, "preprocess", return_value=prepared),
                patch.object(ocr, "_tesseract_with_source", side_effect=[
                    ("primary sufficiently long readable text", "tsv-primary"),
                    ("alternate text", "tsv-secondary"),
                ]) as run,
            ):
                variants = ocr._recognize_pages(
                    Path("synthetic.png"), [original], budget, collector,
                    recovery_decider=lambda _: (6, True, "synthetic_defect"),
                )
            self.assertEqual(variants, ["primary sufficiently long readable text", "alternate text"])
            self.assertEqual([c.args[0].size for c in run.call_args_list], [(200, 200), (100, 100)])
            self.assertEqual([c.args[1] for c in run.call_args_list], ["--psm 3", "--psm 6"])
            self.assertEqual([c.args[0] for c in collector.record_variant.call_args_list], [0, 1])
            self.assertEqual(budget.recovery_reasons, ["synthetic_defect"])
        finally:
            original.close()
            prepared.close()

    def test_failed_optional_recovery_retains_primary(self):
        page = Image.new("L", (50, 50))
        try:
            with (
                patch.object(ocr, "preprocess", return_value=page),
                patch.object(ocr, "_tesseract", side_effect=[
                    "primary sufficiently long readable text",
                    ocr.OcrProcessingTimeout("Tesseract OCR"),
                ]),
            ):
                self.assertEqual(ocr._recognize_pages(
                    Path("synthetic.png"), [page],
                    recovery_decider=lambda _: (6, False, "synthetic_defect"),
                ), ["primary sufficiently long readable text"])
        finally:
            page.close()

    def test_single_ocr_reading_keeps_values_and_cannot_create_role_authority(self):
        text = ("Должник: Иванов Иван Иванович, ИНН 772881234508, "
                "адрес регистрации: г. Учебный, ул. Тестовая, д. 5, кв. 17")
        expected = extract.parse([text])
        def recognize(_path, **options):
            options["budget"].ocr_pages.add(1)
            return [text]
        with patch.object(ocr, "recognize", side_effect=recognize):
            card = extract.parse_file(Path("synthetic.png"))
        for name in ("fio_raw", "last_name", "first_name", "middle_name", "inn", "address"):
            self.assertEqual(getattr(card, name).value, getattr(expected, name).value)
            self.assertTrue(getattr(card, name).warning)
            self.assertNotEqual(getattr(card, name).source, extract.ROLE_SOURCE)
        self.assertIn("кв. 17", card.address.value)

    def test_single_entity_ocr_remains_review_candidate_in_routing(self):
        from sro_robot.core.entity_extract import ENTITY_CANDIDATE_SOURCE
        from sro_robot.core.mixed_classification import classify_card
        def recognize(_path, **options):
            options["budget"].ocr_pages.add(1)
            return ['Должник: ООО «Учебная организация» ИНН 7700000017']
        with patch.object(ocr, "recognize", side_effect=recognize):
            card = extract.parse_file(Path("synthetic.png"))
        self.assertEqual(card.entity_kind.source, ENTITY_CANDIDATE_SOURCE)
        self.assertEqual(classify_card(card).reason, "review_entity_candidate")
        self.assertTrue(card.legal_name.warning)

    def test_tiff_primary_and_recovery_slots_remain_distinct_across_pages(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.tiff"
            pages = [Image.new("L", (10, 10), value) for value in (10, 20)]
            try:
                pages[0].save(path, save_all=True, append_images=pages[1:])
            finally:
                for page in pages:
                    page.close()
            with (
                patch.object(ocr, "preprocess", side_effect=lambda image, scale: image.copy()),
                patch.object(ocr, "_tesseract", side_effect=[
                    "", "first page recovered", "second page primary text " * 3,
                ]),
            ):
                variants = ocr.recognize(path)
        self.assertEqual(len(variants), 2)
        self.assertEqual([p.strip() for p in variants[0].split("\f")], ["", "second page primary text " * 2 + "second page primary text"])
        self.assertEqual([p.strip() for p in variants[1].split("\f")], ["first page recovered", ""])

    def test_pdf_recovery_timeout_keeps_primary_but_overall_timeout_propagates(self):
        for stage in ("Tesseract OCR", "overall OCR"):
            with self.subTest(stage=stage):
                page = Image.new("L", (50, 50), 0)
                with (
                    patch.object(ocr, "_load_pdf_pages", return_value=[page]),
                    patch.object(ocr, "_tesseract", side_effect=[
                        "primary sufficiently long readable text",
                        ocr.OcrProcessingTimeout(stage),
                    ]),
                ):
                    if stage == "overall OCR":
                        with self.assertRaises(ocr.OcrProcessingTimeout):
                            ocr._recognize_pdf_pages(
                                Path("synthetic.pdf"), [""], 300,
                                recovery_decider=lambda _: (6, False, "synthetic_defect"),
                            )
                    else:
                        self.assertEqual(ocr._recognize_pdf_pages(
                            Path("synthetic.pdf"), [""], 300,
                            recovery_decider=lambda _: (6, False, "synthetic_defect"),
                        ), ["primary sufficiently long readable text"])
                page.close()

    def test_text_pdf_fields_are_unaffected_by_ocr_review_gate(self):
        text = "Должник: Иванов Иван Иванович, ИНН 772881234508"
        expected = extract.parse([text])
        with patch.object(ocr, "recognize", return_value=[text]):
            card = extract.parse_file(Path("synthetic.pdf"))
        self.assertEqual(card_to_fields(card), card_to_fields(expected))


if __name__ == "__main__":
    unittest.main()
