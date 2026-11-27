import json
from pathlib import Path
import tempfile
import unittest

from scripts.dev.quality.registry_compare import ROOT, main, reconcile


def field(source, ocr, registry, binding="comparable", evidence="observed"):
    return {"source": source, "ocr": ocr, "registry": registry,
            "binding": binding, "evidence": evidence}


def pair(case="А99-4321/2099", family="appointment", fields=None, registry_case=None, tags=None, diagnostic=None):
    value = {"source_case": case, "registry_case": registry_case or case,
             "family": family, "fields": fields or {"hearing_date": field("31.05.2099", "31.05.2099", "31.05.2099")}}
    if tags is not None:
        value["tags"] = tags
    if diagnostic is not None:
        value["diagnostic"] = diagnostic
    return value


class RegistryCompareTests(unittest.TestCase):
    def test_case_number_normalization_and_categories_are_aggregate_only(self):
        data = {"schema": 1, "pairs": [
            pair(case="a99 - 4321 / 2099", fields={
                "all_equal": field("A", "A", "A"),
                "ocr_source": field("B", "B", "C"),
                "registry_source": field("D", "E", "D"),
                "weak": field("Учебная\nКомпания", "учебная компания", "Другой вариант"),
            }, tags=["synthetic"]),
        ]}
        result = reconcile(data)
        self.assertEqual(result["status"], "observed_weak_comparison_not_gold")
        self.assertEqual(result["qualifying_case_groups"], 1)
        self.assertEqual(result["categories"]["all_equal"], 1)
        self.assertEqual(result["categories"]["ocr_matches_source_only"], 1)
        self.assertEqual(result["categories"]["registry_matches_source_only"], 1)
        self.assertEqual(result["categories"]["weak_match_only"], 1)
        serialized = json.dumps(result, ensure_ascii=False)
        for private_value in ("А99", "4321", "Учебная", "synthetic"):
            self.assertNotIn(private_value, serialized)

    def test_mismatched_or_unknown_case_is_rejected_from_scoring(self):
        data = {"schema": 1, "pairs": [
            pair(registry_case="А99-1234/2099"),
            pair(case="not-a-case", registry_case="not-a-case"),
        ]}
        result = reconcile(data)
        self.assertEqual(result["qualifying_case_groups"], 0)
        self.assertEqual(result["categories"], {})
        self.assertEqual(result["rejected_mismatched_case_pairs"], 1)
        self.assertEqual(result["rejected_unknown_case_pairs"], 1)

    def test_case_prefixes_and_dashes_are_canonicalized_without_serial_expansion(self):
        data = {"schema": 1, "pairs": [
            pair(case="a1–4321/2099", registry_case="А1-4321/2099"),
            pair(case="СИП — 135/2099", registry_case="СИП-135/2099"),
            pair(case="А1-4321/99", registry_case="А1-4321/2099"),
        ]}
        result = reconcile(data)
        self.assertEqual(result["qualifying_case_groups"], 2)
        self.assertEqual(result["rejected_unknown_case_pairs"], 1)

    def test_duplicate_case_rows_count_once_and_conflicting_field_is_unqualified(self):
        first = pair(fields={"inn": field("111", "111", "111"),
                             "name": field("Alpha", "Alpha", "Alpha")})
        repeated = pair(fields={"inn": field("111", "111", "111"),
                                "name": field("Alpha", "Alpha", "Alpha")})
        conflict = pair(fields={"inn": field("111", "112", "111")})
        result = reconcile({"schema": 1, "pairs": [first, repeated, conflict]})
        self.assertEqual(result["qualifying_case_groups"], 1)
        self.assertEqual(result["duplicate_rows_removed"], 1)
        self.assertEqual(result["duplicate_conflicts"], 1)
        self.assertEqual(result["categories"]["all_equal"], 1)
        self.assertEqual(result["categories"]["unqualified"], 1)
        self.assertEqual(result["qualifiers"]["duplicate_conflict"], 1)

    def test_missing_and_lookup_values_are_not_assumed_absent_or_scored(self):
        result = reconcile({"schema": 1, "pairs": [pair(fields={
            "no_ocr": field("source value", None, "registry value"),
            "no_registry": field("source value", "ocr value", None),
            "both_missing": field("source value", None, None),
            "lookup": field(None, None, "registry-only value", binding="lookup"),
            "not_in_act": field(None, None, None, binding="not_in_act"),
            "missing_source": field(None, "ocr value", "registry value", binding="missing_source"),
            "approved_date": field("source date", "source date", "approved date", evidence="approved_date"),
            "registry_only": field(None, None, "registry value", binding="lookup", evidence="registry_only"),
            "text_layer_only": field("text-layer value", "text-layer value", None, evidence="weak_pdf_text"),
        })]})
        self.assertEqual(result["categories"]["ocr_missing"], 1)
        self.assertEqual(result["categories"]["registry_missing"], 1)
        self.assertEqual(result["categories"]["both_missing"], 1)
        self.assertEqual(result["categories"]["unqualified"], 6)
        self.assertEqual(result["qualifiers"]["weak_pdf_text"], 1)
        self.assertEqual(result["qualifiers"]["lookup"], 1)
        self.assertEqual(result["qualifiers"]["not_in_act"], 1)
        self.assertEqual(result["qualifiers"]["missing_source"], 1)
        self.assertEqual(result["qualifiers"]["approved_date"], 1)
        self.assertEqual(result["qualifiers"]["registry_only"], 1)

    def test_unverified_pdf_text_is_unqualified_and_diagnostics_stay_private(self):
        result = reconcile({"schema": 1, "pairs": [pair(
            fields={"private_person_name": field("Private Name", "Private OCR", "Private Record",
                                                  evidence="weak_pdf_text")},
            diagnostic="PRIVATE-DIAGNOSTIC-TEXT",
        )]})
        self.assertEqual(result["categories"]["unqualified"], 1)
        self.assertEqual(result["qualifiers"]["weak_pdf_text"], 1)
        self.assertEqual(result["pairs_with_diagnostics"], 1)
        self.assertIn("other_field", result["by_field"])
        serialized = json.dumps(result, ensure_ascii=False)
        for private_value in ("Private Name", "Private OCR", "Private Record", "PRIVATE-DIAGNOSTIC-TEXT"):
            self.assertNotIn(private_value, serialized)

    def test_invalid_private_input_error_does_not_echo_path_or_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            (root / "pairs.json").write_text("PERSONAL-INPUT-MUST-STAY-PRIVATE", encoding="utf-8")
            from contextlib import redirect_stdout
            from io import StringIO
            output = StringIO()
            with redirect_stdout(output):
                status = main(["--private-root", str(root), "--input-name", "pairs.json"])
            self.assertEqual(status, 1)
            self.assertNotIn("PERSONAL", output.getvalue())
            self.assertNotIn(str(root), output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["status"], "rejected")

    def test_valid_cli_writes_only_aggregate_metrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            data = {"schema": 1, "pairs": [pair(fields={
                "fio": field("Private Name", "Private Name", "Private Name")
            }, tags=["private-label"], diagnostic="private diagnostic") ]}
            (root / "pairs.json").write_text(json.dumps(data), encoding="utf-8")
            from contextlib import redirect_stdout
            from io import StringIO
            output = StringIO()
            with redirect_stdout(output):
                status = main(["--private-root", str(root), "--input-name", "pairs.json"])
            self.assertEqual(status, 0)
            self.assertTrue((root / "registry-compare-metrics.json").is_file())
            for private_value in ("Private Name", "private-label", "private diagnostic", "А99-4321"):
                self.assertNotIn(private_value, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["categories"]["all_equal"], 1)

    def test_checkout_and_symlinked_private_paths_are_rejected(self):
        from contextlib import redirect_stdout
        from io import StringIO

        checkout_root = ROOT / "tests" / "must-not-create-registry-compare-data"
        output = StringIO()
        with redirect_stdout(output):
            status = main(["--private-root", str(checkout_root), "--input-name", "pairs.json"])
        self.assertEqual(status, 1)
        self.assertFalse(checkout_root.exists())
        self.assertNotIn(str(checkout_root), output.getvalue())

        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external:
            root = Path(temporary)
            target = Path(external)
            root.chmod(0o700)
            target.chmod(0o700)
            protected = target / "pairs.json"
            protected.write_text("PRIVATE-OUTSIDE-INPUT", encoding="utf-8")
            (root / "pairs.json").symlink_to(protected)
            output = StringIO()
            with redirect_stdout(output):
                status = main(["--private-root", str(root), "--input-name", "pairs.json"])
            self.assertEqual(status, 1)
            self.assertNotIn("PRIVATE-OUTSIDE-INPUT", output.getvalue())

    def test_insecure_private_root_permissions_are_rejected(self):
        from contextlib import redirect_stdout
        from io import StringIO

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            output = StringIO()
            with redirect_stdout(output):
                status = main(["--private-root", str(root), "--input-name", "pairs.json"])
            self.assertEqual(status, 1)
            self.assertNotIn(str(root), output.getvalue())


if __name__ == "__main__":
    unittest.main()
