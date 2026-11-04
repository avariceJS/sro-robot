from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.services.fields import review_fields
from sro_robot.core.extract import parse, parse_file
from sro_robot.core.entity_extract import parse_entity
from sro_robot.core.models import card_to_fields
from app.services.assignments import automatic_preparation_reason
from sro_robot.core.models import Field


class ReviewFieldEvidenceTests(unittest.TestCase):
    def test_image_locations_survive_storage_and_unchanged_review_values(self) -> None:
        card = parse(["РЕШЕНИЕ. Дело № А41-61776/2026."])
        boxes = [{"page": 1, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.04}]
        card.case_number.source_boxes = boxes
        task = SimpleNamespace(scenario="person_card", recognized=card_to_fields(card),
                               values={"case_number": card.case_number.value})
        row = {item.attr: item for item in review_fields(task)}["case_number"]
        self.assertEqual(row.source_boxes, boxes)
        self.assertEqual(row.source_value, card.case_number.value)
        task.values["case_number"] = "А99-999/2099"
        changed = {item.attr: item for item in review_fields(task)}["case_number"]
        self.assertEqual(changed.source_boxes, [])

    def test_review_candidate_keeps_its_own_image_locations(self) -> None:
        boxes = [{"page": 1, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.04}]
        task = SimpleNamespace(scenario="person_card", values={}, recognized={
            "_review_suggestions_v1": [{
                "kind": "application_person", "copyable_fields": ["last_name"],
                "fields": {"last_name": {"value": "Макетов", "confidence": 0.6,
                    "source_variant": 0, "source_start": 20, "source_end": 27,
                    "source_boxes": boxes, "source_page": 1}},
            }],
        })
        row = {item.attr: item for item in review_fields(task)}["last_name"]
        self.assertEqual(row.source_boxes, boxes)
        self.assertEqual(row.source_value, "Макетов")
        self.assertEqual(row.evidence_status, "uncertain")

    def test_person_name_and_inn_keep_role_bound_ocr_spans(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года. Дело № А41-61776/2026.\n"
            "Заявитель: Петров Пётр Иванович, ИНН 500100001146\n"
            "Должник: Иванов Иван Иванович, ИНН 772881234508, СНИЛС 112-233-445 95\n\n"
            "Адрес регистрации: 123456, г. Макетный,\nулица Учебная, дом 7"
        )
        card = parse([text])
        task = SimpleNamespace(scenario="person_card", recognized=card_to_fields(card), values={})

        fields = {item.attr: item for item in review_fields(task)}

        for name in ("last_name", "first_name", "middle_name", "inn", "snils", "address"):
            with self.subTest(field=name):
                self.assertEqual(fields[name].source_variant, 0)
                self.assertLess(fields[name].source_start, fields[name].source_end)
                excerpt = " ".join(fields[name].source_excerpt.split())
                self.assertIn(fields[name].value, excerpt)
        for name in ("last_name", "first_name", "middle_name"):
            self.assertEqual(
                text[fields[name].source_start:fields[name].source_end],
                "Иванов Иван Иванович",
            )
        self.assertEqual(text[fields["inn"].source_start:fields["inn"].source_end], "772881234508")
        self.assertEqual(
            text[fields["snils"].source_start:fields["snils"].source_end],
            "112-233-445 95",
        )
        self.assertEqual(
            " ".join(text[fields["address"].source_start:fields["address"].source_end].split()),
            fields["address"].value,
        )

    def test_source_offsets_survive_multiple_bounded_personal_continuations(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 14 октября 2099 года. Дело № А99-12345/2099.\n"
            "Должник: Иванов Иван Иванович\n\n"
            "ИНН 772881234508, СНИЛС 112-233-445 95\n\n"
            "Адрес регистрации: 101000, г. Макетный,\nулица Учебная, дом 7"
        )
        card = parse([text])
        task = SimpleNamespace(scenario="person_card", recognized=card_to_fields(card), values={})

        fields = {item.attr: item for item in review_fields(task)}

        self.assertEqual(text[fields["inn"].source_start:fields["inn"].source_end], "772881234508")
        self.assertEqual(
            text[fields["snils"].source_start:fields["snils"].source_end],
            "112-233-445 95",
        )
        address_source = text[fields["address"].source_start:fields["address"].source_end]
        self.assertEqual(" ".join(address_source.split()), fields["address"].value)
        self.assertTrue(fields["inn"].warning)
        self.assertTrue(fields["snils"].warning)
        self.assertTrue(fields["address"].warning)

    def test_entity_review_fields_expose_metadata_and_court_source_spans(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года. Дело № А41-61776/2026.\n"
            "Арбитражный суд города Москвы.\n"
            "Должник: ООО «Учебная организация» ИНН 7700000016"
        )
        card = parse_entity([text])
        task = SimpleNamespace(scenario="entity_review", recognized=card_to_fields(card), values={})

        fields = {item.attr: item for item in review_fields(task)}

        for name in ("doc_type", "doc_date", "case_number", "court"):
            with self.subTest(field=name):
                self.assertEqual(fields[name].source_variant, 0)
                self.assertLess(fields[name].source_start, fields[name].source_end)
                expected_text = "14 августа 2026" if name == "doc_date" else fields[name].value
                self.assertIn(expected_text.casefold(), fields[name].source_excerpt.casefold())

    def test_multipage_pdf_spans_identify_the_page_with_each_value(self) -> None:
        cover = "Письмо о направлении документов. Сопроводительный лист без реквизитов."
        act = (
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года. Дело № А41-61776/2026.\n"
            "Арбитражный суд города Москвы.\n"
            "Должник: ООО «Учебная организация» ИНН 7700000016"
        )
        text = f"{cover}\f\n{act}"
        with patch("sro_robot.core.ocr.recognize", return_value=[text]):
            card = parse_file("synthetic-multipage.pdf")
        task = SimpleNamespace(scenario="entity_review", recognized=card_to_fields(card), values={})

        fields = {item.attr: item for item in review_fields(task)}

        for name in ("doc_type", "doc_date", "case_number", "court", "legal_name", "inn"):
            with self.subTest(field=name):
                self.assertEqual(fields[name].source_page, 2)
                source = text[fields[name].source_start:fields[name].source_end]
                self.assertTrue(source)
                expected = "14 августа 2026" if name == "doc_date" else fields[name].value
                self.assertIn(expected.casefold(), source.casefold())

    def test_non_paginated_recognizer_variant_does_not_claim_a_page_number(self) -> None:
        card = parse(["РЕШЕНИЕ. Дело № А41-61776/2026."])
        task = SimpleNamespace(scenario="person_card", recognized=card_to_fields(card), values={})

        fields = {item.attr: item for item in review_fields(task)}

        self.assertIsNone(fields["case_number"].source_page)

    def test_person_role_spans_keep_pdf_page_after_a_cover(self) -> None:
        text = (
            "Сопроводительное письмо о направлении приложений.\f\n"
            "Должник: Иванов Иван Иванович, ИНН 772881234508"
        )
        card = parse([text])
        task = SimpleNamespace(scenario="person_card", recognized=card_to_fields(card), values={})

        fields = {item.attr: item for item in review_fields(task)}

        for name in ("last_name", "first_name", "middle_name", "inn"):
            with self.subTest(field=name):
                field = fields[name]
                self.assertEqual(field.source_page, 2)
                self.assertIn(field.value, text[field.source_start:field.source_end])

    def test_court_review_shows_variant_span_for_conflicting_captions(self) -> None:
        text = (
            "РЕШЕНИЕ\nАрбитражный суд г. Москвы\n"
            "Дело передано для рассмотрения в Арбитражный суд Московской области."
        )
        card = parse_entity([text])
        task = SimpleNamespace(scenario="entity_review", recognized=card_to_fields(card), values={})

        court = {item.attr: item for item in review_fields(task)}["court"]

        self.assertIn("расходится", court.warning)
        self.assertEqual(court.evidence_status, "uncertain")
        self.assertEqual(court.source_variant, 0)
        self.assertEqual(text[court.source_start:court.source_end], court.value)
        self.assertIn(court.value, court.source_excerpt)

    def test_entity_review_fields_show_ocr_variant_offsets_and_address_review_state(self) -> None:
        card = parse_entity([
            "Должник: ООО «Учебная организация»\nАдрес: 101000, вымышленный город, дом 1"
        ])
        task = SimpleNamespace(scenario="entity_review", recognized=card_to_fields(card), values={})

        fields = {item.attr: item for item in review_fields(task)}

        self.assertEqual(fields["legal_name"].source_variant, 0)
        self.assertIn("ООО «Учебная организация»", fields["legal_name"].source_excerpt)
        self.assertEqual(fields["address"].evidence_status, "uncertain")
        self.assertEqual(fields["address_kind"].evidence_status, "missing")
        self.assertIn("явно не указан", fields["address_kind"].warning)

    def test_truncated_entity_name_warning_is_visible_in_review_fields(self) -> None:
        card = parse_entity(["Должник: ООО " + "А" * 520])
        task = SimpleNamespace(scenario="entity_review", recognized=card_to_fields(card), values={})

        fields = {item.attr: item for item in review_fields(task)}

        self.assertEqual(len(fields["legal_name"].value), 512)
        self.assertIn("обрезано", fields["legal_name"].warning)
        self.assertEqual(fields["legal_name"].evidence_status, "uncertain")

    def test_missing_and_warning_fields_are_distinguished_for_staff(self) -> None:
        card = parse([
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года. Дело № А41-61776/2026. "
            "Должник: Иванов Иван Иванович"
        ])
        task = SimpleNamespace(scenario="person_card", recognized=card_to_fields(card), values={})

        fields = {item.attr: item for item in review_fields(task)}

        self.assertEqual(fields["inn"].evidence_status, "missing")
        self.assertEqual(fields["last_name"].evidence_status, "recognized")
        self.assertIn("ИНН", {item.title for item in fields.values()})

    def test_invalid_confidence_is_presented_as_uncertain(self) -> None:
        card = parse([
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года. Дело № А41-61776/2026. "
            "Должник: Иванов Иван Иванович"
        ])
        card.doc_date.confidence = 1.2
        task = SimpleNamespace(scenario="person_card", recognized=card_to_fields(card), values={})

        fields = {item.attr: item for item in review_fields(task)}

        self.assertEqual(fields["doc_date"].evidence_status, "uncertain")
        self.assertTrue(fields["doc_date"].uncertain)

    def test_recovered_inn_stays_a_warning_only_suggestion(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года. Дело № А41-61776/2026. "
            "Должник: Иванов Иван Иванович, ИНН 77288123450"
        )
        recovered = text.replace("77288123450", "772881234508")
        with (
            patch("sro_robot.core.ocr.recognize", return_value=[text, text, text]),
            patch("sro_robot.core.ocr.recover_role_bound_inn", return_value=[recovered]),
        ):
            card = parse_file("synthetic-scan.pdf")

        self.assertEqual(card.inn.value, "772881234508")
        self.assertEqual(card.inn.source, "role_partial")
        self.assertIsNotNone(card.inn.warning)
        self.assertNotEqual(automatic_preparation_reason(card), None)

    def test_pdf_retries_damaged_role_bound_inn_at_lower_dpi_without_auto_preparing(self) -> None:
        text = (
            "РЕШЕНИЕ 14 октября 2098 года. Дело № А98-740008/2098. "
            "Должник: Смирнова Наталья Владимировна, ИНН 7810000100850"
        )
        high_dpi = text.replace("7810000100850", "7810000010850")
        lower_dpi = text.replace("7810000100850", "781000010850")
        with (
            patch("sro_robot.core.ocr.recognize", return_value=[text, text, text]),
            patch(
                "sro_robot.core.ocr.recover_role_bound_inn",
                side_effect=[[high_dpi], [lower_dpi]],
            ) as recovery,
        ):
            card = parse_file("synthetic-scan.pdf")

        self.assertEqual(card.inn.value, "781000010850")
        self.assertEqual(card.inn.source, "role_partial")
        self.assertIsNotNone(card.inn.warning)
        self.assertNotEqual(automatic_preparation_reason(card), None)
        self.assertEqual([call.kwargs["dpi"] for call in recovery.call_args_list], [300, 200])

    def test_low_resolution_date_crosscheck_keeps_alternate_as_manual_review_evidence(self) -> None:
        high_resolution = (
            "ОПРЕДЕЛЕНИЕ 1 июля 2099 года. Дело № А99-900091/2099. "
            "Должник: Гранитберегова Анна Петровна, ИНН 990000009037"
        )
        low_resolution = high_resolution.replace("1 июля", "7 июля")
        with (
            patch("sro_robot.core.ocr.recognize", return_value=[high_resolution] * 4),
            patch(
                "sro_robot.core.ocr.recover_low_resolution_header",
                return_value=[low_resolution] * 4,
            ) as crosscheck,
            patch("sro_robot.core.ocr.recover_role_bound_inn") as inn_recovery,
        ):
            card = parse_file("synthetic-low-resolution.pdf")

        self.assertEqual(card.doc_date.value, "07.07.2099")
        self.assertEqual(card.doc_date.source, "ocr_scale_crosscheck")
        self.assertIsNotNone(card.doc_date.warning)
        self.assertEqual(card.fio_raw.value, "Гранитберегова Анна Петровна")
        self.assertEqual(card.inn.value, "990000009037")
        self.assertNotEqual(automatic_preparation_reason(card), None)
        crosscheck.assert_called_once()
        inn_recovery.assert_not_called()

    def test_date_crosscheck_is_skipped_for_text_only_pdf_fast_path(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года. Дело № А41-61776/2026. "
            "Должник: Иванов Иван Иванович, ИНН 772881234508"
        )
        with (
            patch("sro_robot.core.ocr.recognize", return_value=[text]),
            patch("sro_robot.core.ocr.recover_low_resolution_header") as crosscheck,
        ):
            card = parse_file("synthetic-searchable.pdf")

        self.assertEqual(card.doc_date.value, "14.08.2026")
        crosscheck.assert_not_called()

    def test_inn_retry_is_not_run_when_document_has_no_inn_token(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года. Дело № А41-61776/2026. "
            "Должник: Иванов Иван Иванович"
        )
        ambiguous_card = SimpleNamespace(
            fio_raw=Field("Иванов Иван Иванович", 0.75, "debtor_role:v1"),
            inn=Field(source="role_ambiguous", warning="Повреждённый реквизит"),
            doc_date=Field("14.08.2026", 0.8, "ocr"),
        )
        with (
            patch("sro_robot.core.ocr.recognize", return_value=[text, text, text]),
            patch("sro_robot.core.extract.parse", return_value=ambiguous_card),
            patch("sro_robot.core.ocr.recover_role_bound_inn", return_value=[]) as recovery,
            patch("sro_robot.core.ocr.recover_low_resolution_header", return_value=[]),
        ):
            card = parse_file("synthetic-scan.pdf")

        recovery.assert_not_called()
        self.assertIsNone(card.inn.value)


if __name__ == "__main__":
    unittest.main()
