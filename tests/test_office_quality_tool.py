from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.dev.quality import evaluate_office_corpus as office_quality


class OfficeQualityToolTests(unittest.TestCase):
    def test_init_writes_private_template_without_field_labels(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents = root / "documents"
            documents.mkdir()
            (documents / "private-case.pdf").write_bytes(b"synthetic source")
            labels = root / "gold.json"

            self.assertEqual(office_quality.initialize(documents, labels), 0)
            self.assertEqual(labels.stat().st_mode & 0o777, 0o600)
            rendered = json.loads(labels.read_text())
            self.assertEqual(rendered["documents"][0]["confirmed_fields"], [])
            self.assertTrue(all(value is None for value in rendered["documents"][0]["expected"].values()))

    def test_evaluation_requires_independent_human_confirmation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents = root / "documents"
            documents.mkdir()
            (documents / "case.pdf").write_bytes(b"synthetic source")
            labels = root / "gold.json"
            labels.write_text(json.dumps({
                "format": "sro-office-quality-gold-v1",
                "reviewer": "",
                "independent_visual_review": False,
                "documents": [],
            }))
            labels.chmod(0o600)
            with self.assertRaises(SystemExit), patch.object(office_quality, "parse_file") as parse:
                office_quality.evaluate(documents, labels, root / "report.json")
            parse.assert_not_called()

    def test_report_contains_counts_but_no_filename_or_recognized_values(self) -> None:
        from types import SimpleNamespace

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents = root / "documents"
            documents.mkdir()
            source = documents / "private-fio-and-inn.pdf"
            source.write_bytes(b"synthetic source")
            labels = root / "gold.json"
            values = {
                "doc_type": "решение", "doc_date": "01.01.2099",
                "case_number": "А99-123456/2099", "fio_raw": "Тестова Анна Ивановна",
                "inn": "990000000041", "address": "г. Макетный, ул. Учебная, д. 1",
            }
            labels.write_text(json.dumps({
                "format": "sro-office-quality-gold-v1",
                "reviewer": "synthetic reviewer",
                "independent_visual_review": True,
                "documents": [{
                    "id": office_quality.digest(source), "filename": source.name,
                    "sha256": office_quality.digest(source),
                    "confirmed_fields": ["doc_type", "fio_raw", "inn"],
                    "expected": {key: value for key, value in values.items()},
                    "expected_disposition": "auto_prepare",
                }],
            }))
            labels.chmod(0o600)
            report_path = root / "report.json"
            fields = {key: SimpleNamespace(value=value) for key, value in values.items()}
            with patch.object(office_quality, "parse_file", return_value=SimpleNamespace(**fields)), \
                    patch.object(office_quality, "automatic_preparation_reason", return_value=None):
                self.assertEqual(office_quality.evaluate(documents, labels, report_path), 0)
            report = report_path.read_text()
            self.assertNotIn(source.name, report)
            self.assertNotIn("Тестова Анна Ивановна", report)
            self.assertNotIn("990000000041", report)
            self.assertEqual(json.loads(report)["field_counts"]["inn"]["exact"], 1)


if __name__ == "__main__":
    unittest.main()
