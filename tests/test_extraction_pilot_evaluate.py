"""Synthetic identity and incremental-discovery checks for the offline pilot evaluator."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.dev.extraction_pilot.evaluate import _apply_errata, _cd_extract, _field_status, _matches, _timing, _score_fields, evaluate


def _label():
    return {
        "source_id": "synthetic-1",
        "group_id": "synthetic-case",
        "split": "eval",
        "document": {"document_date": None, "case_number": None, "court_printed": None},
        "primary": {
            "participant_id": "p1", "name_printed": "Иванов Иван Иванович",
            "name_normalized": "Иванов Иван Иванович", "inn_printed": "123456789012",
            "inn_normalized": "123456789012", "snils_printed": None,
            "snils_normalized": None, "ogrn_printed": None, "ogrn_normalized": None,
            "address_raw": None, "address_kind": "unknown", "address_label": None,
        },
        "participants": [],
        "completeness": {"primary": "complete", "secondary_participants": "partial", "notes": "synthetic"},
        "uncertainties": [],
    }


class ExtractionPilotEvaluatorTests(unittest.TestCase):
    def test_identifier_on_unrelated_participant_is_wrong_owner_not_primary_credit(self):
        label = _label()
        label["participants"] = [{"participant_id": "p2", "name_printed": "Петров Петр Петрович", "name_normalized": "Петров Петр Петрович", "roles": [{"role": "judge", "certainty": "high", "evidence": []}]}]
        row = {"route": "C", "calls": [{"result": {
            "document": {}, "participants": [
                {"name": {"raw": "Иванов Иван Иванович", "value": "Иванов Иван Иванович"}, "roles": [], "fields": []},
                {"name": {"raw": "Петров Петр Петрович", "value": "Петров Петр Петрович"}, "roles": [], "fields": [
                    {"key": "inn", "raw": "123456789012", "value": "123456789012"},
                ]},
            ], "incomplete": False,
        }}]}
        main, _parties, alignment, _observations = _cd_extract(row, label)
        self.assertEqual(main["inn"], "")
        self.assertEqual(alignment["identity_alignment"], "matched")
        self.assertEqual(alignment["wrong_owner_primary_identifiers"], [{"participant_index": 1, "field": "inn"}])

    def test_unannotated_nonmatching_owner_remains_uncertain(self):
        label = _label()
        row = {"route": "C", "calls": [{"result": {"document": {}, "participants": [
            {"name": {"raw": "Иванов Иван Иванович", "value": "Иванов Иван Иванович"}, "roles": [], "fields": []},
            {"name": {"raw": "Петров Петр Петрович", "value": "Петров Петр Петрович"}, "roles": [], "fields": [{"key": "inn", "raw": "123456789012", "value": "123456789012"}]},
        ], "incomplete": False}}]}
        _main, _parties, alignment, _obs = _cd_extract(row, label)
        self.assertEqual(alignment["wrong_owner_primary_identifiers"], [])
        self.assertEqual(alignment["uncertain_owner_primary_identifiers"], [{"participant_index": 1, "field": "inn"}])

    def test_gendered_name_lemmas_are_not_collapsed(self):
        self.assertFalse(_matches("name", "Иванова Мария Ивановна", "Иванов Мария Ивановна"))

    def test_proposal_already_in_same_identity_baseline_pool_is_not_new_discovery(self):
        label = _label()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "A").mkdir()
            (root / "C").mkdir()
            baseline = {
                "route": "A", "fields": {
                    "last_name": {"value": "Иванов"}, "first_name": {"value": "Иван"},
                    "middle_name": {"value": "Иванович"}, "inn": {"value": None},
                    "review_suggestions": [{"fields": {
                        "fio": {"value": "Иванов Иван Иванович"},
                        "inn": {"value": "123456789012"},
                    }}],
                }, "review": [], "timing": {"total_s": 0.1},
            }
            candidate = {
                "route": "C", "calls": [{"result": {
                    "document": {}, "participants": [{
                        "name": {"raw": "Иванов Иван Иванович", "value": "Иванов Иван Иванович"},
                        "roles": [], "fields": [{"key": "inn", "raw": "123456789012", "value": "123456789012"}],
                    }], "incomplete": False,
                }, "timing": {"model_s": 0.2}}], "reader_timing": {"total_s": 0.1},
            }
            (root / "A" / "synthetic-1.json").write_text(json.dumps(baseline))
            (root / "C" / "synthetic-1.json").write_text(json.dumps(candidate))
            result = evaluate({"status": "synthetic", "labels": [label]}, {"sources": [{"source_id": "synthetic-1", "group_id": "synthetic-case", "split": "eval"}]}, root)
            source = result["sources"][0]
            self.assertTrue(source["baseline_suggestions"]["same_identity_correct_missing_main"]["inn"])
            self.assertFalse(source["incremental_correct_discovery_vs_A"]["C"]["inn"])

    def test_raw_address_does_not_prove_unsupported_normalized_value(self):
        label = _label()
        label["primary"]["address_raw"] = "630000, г. Тест, ул. Примерная, д. 1"
        label["primary"]["address_kind"] = "actual"
        model = {"route": "C", "calls": [{"result": {
            "document": {}, "participants": [{
                "name": {"raw": "Иванов Иван Иванович", "value": "Иванов Иван Иванович"},
                "roles": [], "fields": [{"key": "address", "raw": "630000, г. Тест, ул. Примерная, д. 1", "value": "630000, г. Тест, ул. Примерная, д. 1, кв. 9", "normalization_support": "unsupported_address_change"}],
            }], "incomplete": False,
        }}], "reader_timing": {"total_s": 1.0}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "C").mkdir()
            (root / "C" / "synthetic-1.json").write_text(json.dumps(model))
            result = evaluate({"status": "synthetic", "labels": [label]}, {"sources": [{"source_id": "synthetic-1", "group_id": "synthetic-case", "split": "eval"}]}, root)
        route = result["routes"]["C"]["per_source"][0]
        self.assertEqual(route["raw_model_output"]["fields"]["address"]["status"], "correct")
        self.assertEqual(route["normalized_model_value"]["fields"]["address"]["status"], "wrong")
        self.assertEqual(route["identifier_address_type_normalization_gated_values"]["fields"]["address"]["status"], "missing")
        self.assertEqual(route["normalization_support_counts"]["unsupported_address_change"], 1)

    def test_errata_is_applied_in_memory_and_hashes_are_recorded(self):
        gold = {"labels": [{"source_id": "synthetic-1", "primary": {"inn_normalized": "old"}}]}
        raw = json.dumps(gold).encode()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "errata-private.json"
            path.write_text(json.dumps({"overrides": [{"source_id": "synthetic-1", "field": "inn_normalized", "value": "new", "reason": "synthetic check"}], "exclude_fields": [{"source_id": "synthetic-1", "field": "name", "reason": "synthetic reference uncertainty"}]}))
            fixed, meta = _apply_errata(gold, path, raw)
        self.assertEqual(fixed["labels"][0]["primary"]["inn_normalized"], "new")
        self.assertEqual(gold["labels"][0]["primary"]["inn_normalized"], "old")
        self.assertEqual(len(meta["gold_sha256"]), 64)
        self.assertEqual(len(meta["errata_sha256"]), 64)
        self.assertEqual(meta["errata_applied"][0]["field"], "inn_normalized")
        self.assertEqual(meta["excluded_fields"][0]["field"], "name")
        with tempfile.TemporaryDirectory() as tmp:
            metrics = evaluate(fixed | {"status": "synthetic"}, {"sources": [{"source_id": "synthetic-1", "group_id": "g", "split": "eval"}]}, Path(tmp), meta)
        score = metrics["routes"]["A"]["per_source"][0]
        self.assertEqual(score["main"]["fields"]["name"]["status"], "excluded_reference")
        self.assertNotIn("name", metrics["sources"][0]["expected_nonnull_fields"])

    def test_address_kind_unknown_is_abstention_and_not_a_generic_actual_credit(self):
        self.assertEqual(_field_status("address_kind", "", "actual"), "missing")
        self.assertEqual(_field_status("address_kind", "unknown", "unknown"), "unknown_abstention")
        self.assertEqual(_field_status("address_kind", "actual", "unknown"), "wrong")

    def test_unknown_type_denominator_does_not_depend_on_prediction(self):
        expected={'address_kind':'unknown'}
        good=_score_fields({'address_kind':''},expected);bad=_score_fields({'address_kind':'actual'},expected)
        self.assertEqual(good['counts']['expected_nonnull'],1);self.assertEqual(bad['counts']['expected_nonnull'],1)
        self.assertEqual(good['counts']['correct'],0);self.assertEqual(good['counts']['unknown_abstention'],1);self.assertEqual(bad['counts']['wrong'],1)

    def test_subdivision_uses_parent_charged_model_time_once_and_manifest_prep(self):
        row = {"calls": [{"timing": {"model_s": 5.0}, "initial_attempt": {"timing": {"source_binding_s": 0.5}}, "retry_calls": [{"timing": {"model_s": 3.0, "source_binding_s": 1.0}}]}], "reader_timing": {"total_s": 2.0}}
        result = _timing("D", row, None, {"preparation_seconds": 6.37})
        self.assertEqual(result["llm_inference_s"], 5.0)
        self.assertEqual(result["source_binding_s"], 1.5)
        self.assertEqual(result["manifest_preparation_s"], 6.37)
        self.assertAlmostEqual(result["total_s"], 14.87)
        self.assertIn("no B text", result["reader_basis"])

    def test_repeated_field_conflict_is_order_independent_and_retained_as_useful_proposal(self):
        label = _label()
        fields = [
            {"key": "inn", "raw": "123456789012", "value": "123456789012", "normalization_support": "supported"},
            {"key": "inn", "raw": "999999999999", "value": "999999999999", "normalization_support": "supported"},
        ]
        def score(candidate_fields):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "C").mkdir()
                row = {"route": "C", "calls": [{"result": {"document": {}, "participants": [{
                    "name": {"raw": "Иванов Иван Иванович", "value": "Иванов Иван Иванович"}, "roles": [], "fields": candidate_fields,
                }], "incomplete": False}}]}
                (root / "C" / "synthetic-1.json").write_text(json.dumps(row))
                result = evaluate({"status": "synthetic", "labels": [label]}, {"sources": [{"source_id": "synthetic-1", "group_id": "g", "split": "eval"}]}, root)
                return result
        forward = score(fields)
        reverse = score(list(reversed(fields)))
        for result in (forward, reverse):
            route = result["routes"]["C"]["per_source"][0]
            self.assertEqual(route["main"]["fields"]["inn"]["status"], "missing")
            self.assertEqual(route["field_conflicts"], ["inn"])
            proposal = route["same_identity_alternative_proposals"]["inn"]
            self.assertEqual(proposal["candidate_count"], 2)
            self.assertEqual(proposal["correct_candidate_count"], 1)
            self.assertTrue(proposal["useful_correct_missing_main"])
        self.assertEqual(forward["routes"]["C"]["per_source"][0]["candidate_alternatives_by_field"], reverse["routes"]["C"]["per_source"][0]["candidate_alternatives_by_field"])

    def test_value_and_owner_quote_pair_is_syntactic_not_semantic_ownership(self):
        label = _label()
        for route, status, qualification in (
            ("C", "verified_quote", "value_and_owner_quotes_verified_syntactically"),
            ("D", "page_quote_ocr_corroborated", "page_level_value_and_owner_corroborated_not_word_aligned"),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / route).mkdir()
                row = {"route": route, "calls": [{"result": {"document": {}, "participants": [{
                    "name": {"raw": "Иванов Иван Иванович", "value": "Иванов Иван Иванович"}, "roles": [], "fields": [{
                        "key": "inn", "raw": "123456789012", "value": "123456789012", "normalization_support": "supported",
                        "evidence": {"status": status}, "owner_evidence": {"status": status},
                    }],
                }], "incomplete": False}}]}
                (root / route / "synthetic-1.json").write_text(json.dumps(row))
                result = evaluate({"status": "synthetic", "labels": [label]}, {"sources": [{"source_id": "synthetic-1", "group_id": "g", "split": "eval"}]}, root)
            score = result["routes"][route]["per_source"][0]
            evidence = score["selected_participant_field_evidence"]["inn"]
            self.assertEqual(evidence["qualification"], qualification)
            self.assertFalse(evidence["semantic_ownership_proven"])
        self.assertIn("not operational card selection", forward_policy := result["primary_identity_alignment_scope"])


if __name__ == "__main__":
    unittest.main()
