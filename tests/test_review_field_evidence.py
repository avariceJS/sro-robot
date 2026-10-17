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

    def test_inn_retry_is_not_run_when_document_has_no_inn_token(self) -> None:
        text = (
            "ОПРЕДЕЛЕНИЕ 14 августа 2026 года. Дело № А41-61776/2026. "
            "Должник: Иванов Иван Иванович"
        )
        ambiguous_card = SimpleNamespace(
            fio_raw=Field("Иванов Иван Иванович", 0.75, "debtor_role:v1"),
            inn=Field(source="role_ambiguous", warning="Повреждённый реквизит"),
        )
        with (
            patch("sro_robot.core.ocr.recognize", return_value=[text, text, text]),
            patch("sro_robot.core.extract.parse", return_value=ambiguous_card),
            patch("sro_robot.core.ocr.recover_role_bound_inn", return_value=[]) as recovery,
        ):
            card = parse_file("synthetic-scan.pdf")

        recovery.assert_not_called()
        self.assertIsNone(card.inn.value)


if __name__ == "__main__":
    unittest.main()
