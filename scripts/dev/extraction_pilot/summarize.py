"""Rebuild the privacy-safe extraction pilot aggregate from private local inputs.

This command performs no inference. Its JSON output contains aggregate counts,
timings, and status labels only; source text and extracted values stay local.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Allow the documented direct invocation from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.dev.extraction_pilot.evaluate import (
    _ab_main,
    _cd_extract,
    _expected_present,
    _gold_expected,
    _matches,
    _norm,
    _compact,
    _summarize_group,
    privacy_summary,
)

ROUTES = ("A", "B", "C", "D")
PARTICIPANT_FIELDS = ("name", "inn", "snils", "ogrn", "address", "address_kind")
METADATA_FIELDS = ("doc_date", "case_number", "court")
FIELDS = PARTICIPANT_FIELDS + METADATA_FIELDS
CONTROL_SOURCE_ID = "task10_pdf"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _counter_dict(values: Any) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _known_fields(source_id: str, labels: dict[str, dict[str, Any]], metrics: dict[str, Any]) -> list[str]:
    expected = _gold_expected(labels[source_id])
    excluded = {
        item["field"] for item in metrics["reference"].get("excluded_fields", [])
        if item.get("source_id") == source_id
    }
    return [
        field for field in FIELDS
        if field not in excluded
        and _expected_present(expected.get(field))
        and not (field == "address_kind" and _norm(expected.get(field)) == "unknown")
    ]


def _known_outcomes(
    route: str,
    source_ids: list[str],
    fields: tuple[str, ...],
    scores: dict[str, dict[str, dict[str, Any]]],
    labels: dict[str, dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, int]:
    counts = Counter()
    for source_id in source_ids:
        if source_id not in scores[route]:
            continue
        for field in _known_fields(source_id, labels, metrics):
            if field in fields:
                counts[scores[route][source_id]["main"]["fields"][field]["status"]] += 1
    return {key: counts[key] for key in ("correct", "wrong", "missing")}


def _source_candidates(
    route: str, source_id: str, labels: dict[str, dict[str, Any]], results: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    row = read_json(results / route / f"{source_id}.json")
    return _cd_extract(row, labels[source_id])


def _correct_fact_quality(
    route: str,
    source_ids: list[str],
    scores: dict[str, dict[str, dict[str, Any]]],
    labels: dict[str, dict[str, Any]],
    metrics: dict[str, Any],
    results: Path,
) -> dict[str, int]:
    counts = Counter()
    for source_id in source_ids:
        if source_id not in scores[route]:
            continue
        row_path = results / route / f"{source_id}.json"
        if not row_path.is_file():
            continue
        main, parties, _alignment, observations = _source_candidates(route, source_id, labels, results)
        for field in PARTICIPANT_FIELDS:
            if field not in _known_fields(source_id, labels, metrics):
                continue
            if scores[route][source_id]["main"]["fields"][field]["status"] != "correct":
                continue
            observation = observations.get(field, {})
            selected_candidates = []
            for party in parties:
                if not isinstance(party, dict):
                    continue
                if field == "name":
                    atom = party.get("name") or {}
                    owner = party.get("ownership_evidence") or {}
                    if atom.get("raw") == observation.get("raw") or atom.get("value") == observation.get("value"):
                        selected_candidates.append((atom, owner))
                else:
                    for atom in party.get("fields", []) or []:
                        if atom.get("key") != field:
                            continue
                        if atom.get("raw") == observation.get("raw") and (atom.get("value") or atom.get("raw")) == observation.get("value"):
                            selected_candidates.append((atom, atom.get("owner_evidence") or {}))
            atom, owner = selected_candidates[0] if selected_candidates else ({}, {})
            evidence = atom.get("evidence") or {}
            norm_status = observation.get("normalization_support", "not_checked")
            value_status = observation.get("evidence_status", evidence.get("status", "missing"))
            owner_status = observation.get("owner_evidence_status", owner.get("status", "missing"))
            granularity = evidence.get("granularity", "unknown")
            value_ok = value_status == ("verified_quote" if route == "C" else "page_quote_ocr_corroborated")
            owner_ok = owner_status == ("verified_quote" if route == "C" else "page_quote_ocr_corroborated")
            if norm_status == "supported":
                counts["correct_personal_fields_with_supported_normalization"] += 1
            if value_ok:
                counts["correct_personal_fields_with_value_source_accepted"] += 1
            if owner_ok:
                counts["correct_personal_fields_with_owner_quote_syntactically_accepted"] += 1
            if value_ok and owner_ok:
                counts["correct_personal_fields_with_value_and_owner_evidence_pair"] += 1
            if norm_status == "supported" and value_ok and owner_ok:
                counts["correct_personal_fields_with_supported_norm_and_value_owner_pair"] += 1
            if route == "C" and value_ok:
                counts[f"correct_personal_value_quote_granularity_{granularity}"] += 1
            if route == "D" and value_status == "page_only_unverified_quote":
                counts["correct_personal_fields_page_only_unverified"] += 1
    return dict(counts)


def _route_split(
    route: str,
    source_ids: list[str],
    scores: dict[str, dict[str, dict[str, Any]]],
    source_metrics: dict[str, dict[str, Any]],
    labels: dict[str, dict[str, Any]],
    metrics: dict[str, Any],
    results: Path,
    *,
    grouped: bool = False,
) -> dict[str, Any]:
    source_ids = [source_id for source_id in source_ids if source_id in scores[route]]
    rows = [scores[route][source_id] for source_id in source_ids]
    statuses = {
        field: _counter_dict(row["main"]["fields"][field]["status"] for row in rows)
        for field in FIELDS
    }
    participant_outcomes = _known_outcomes(route, source_ids, PARTICIPANT_FIELDS, scores, labels, metrics)
    metadata_outcomes = _known_outcomes(route, source_ids, METADATA_FIELDS, scores, labels, metrics)
    participant_proxy = participant_outcomes["wrong"] + participant_outcomes["missing"]
    metadata_proxy = metadata_outcomes["wrong"] + metadata_outcomes["missing"]
    result = {
        "sources": len(rows),
        "result_status_counts": _counter_dict(row["result_status"] for row in rows),
        "main_field_status_all_sources": statuses,
        "known_participant_fact_outcomes": participant_outcomes,
        "known_metadata_fact_outcomes": metadata_outcomes,
        "correction_proxy_wrong_plus_missing": {
            "participant": participant_proxy,
            "metadata": metadata_proxy,
            "total": participant_proxy + metadata_proxy,
        },
        "address_postcode_apartment_status": {
            key: _counter_dict(row["main"]["fields"].get(key, {}).get("status", "not_recorded") for row in rows)
            for key in ("address_postal_code", "address_apartment", "address_apartment_type")
        },
        "normalization_support_counts": dict(sorted(sum((Counter(row.get("normalization_support_counts", {})) for row in rows), Counter()).items())),
        "value_and_owner_evidence_status_counts": dict(sorted(sum((Counter(row.get("source_status_counts", {})) for row in rows), Counter()).items())),
        "selected_field_syntactic_evidence_qualification_counts": dict(sorted(Counter(
            evidence.get("qualification")
            for row in rows
            for evidence in row.get("selected_participant_field_evidence", {}).values()
        ).items())),
        "timing_seconds": {},
    }
    if route in ("A", "B"):
        result["displayed_review_field_status"] = {
            field: _counter_dict(row["displayed_review"]["fields"][field]["status"] for row in rows)
            for field in FIELDS
        }
    else:
        result["candidate_projection_scope"] = "Normalized candidate participant fields selected by exact gold-name alignment for evaluation only; not source-gated or operational."
        promotions = Counter()
        discoveries = Counter()
        alternatives = Counter()
        useful = Counter()
        roles = Counter()
        conflicts = 0
        for source_id, row in zip(source_ids, rows):
            for field in _known_fields(source_id, labels, metrics):
                if row["main"]["fields"][field]["status"] != "correct":
                    continue
                target = discoveries if source_metrics[source_id]["incremental_correct_discovery_vs_A"][route].get(field) else promotions
                target[field] += 1
            for field, proposal in row.get("same_identity_alternative_proposals", {}).items():
                alternatives[field] += proposal.get("candidate_count", 0)
                useful[field] += bool(proposal.get("useful_correct_missing_main"))
            conflicts += len(row.get("field_conflicts", []))
            roles["expected"] += row.get("roles", {}).get("expected_role_annotations", 0)
            roles["correct"] += row.get("roles", {}).get("correct", 0)
            roles["missing"] += row.get("roles", {}).get("missing", 0)
        result["correct_facts_already_in_A_pool"] = dict(sorted(promotions.items()))
        result["incremental_correct_facts_beyond_A_pool"] = dict(sorted(discoveries.items()))
        result["same_identity_alternative_candidates_preserved"] = dict(sorted(alternatives.items()))
        result["useful_correct_missing_main_alternative_sources"] = dict(sorted(useful.items()))
        result["participant_field_conflicts"] = conflicts
        result["annotated_role_metrics"] = {**roles, "false_positives": "not scored; secondary participant gold is partial"}
        result["wrong_owner_primary_identifier_count"] = sum(len(row.get("wrong_owner_primary_identifiers", [])) for row in rows)
        result["uncertain_owner_primary_identifier_count"] = sum(len(row.get("uncertain_owner_primary_identifiers", [])) for row in rows)
        result["correct_participant_fact_evidence_quality"] = _correct_fact_quality(route, source_ids, scores, labels, metrics, results)
    if grouped:
        result["one_vote_per_group_holdout"] = _summarize_group(rows)
    return result


def _case_diagnostic(
    route: str, source_ids: list[str], scores: dict[str, dict[str, dict[str, Any]]],
    labels: dict[str, dict[str, Any]], results: Path,
) -> dict[str, int]:
    latin_to_cyrillic = str.maketrans({"a": "а", "b": "в", "e": "е", "k": "к", "m": "м", "h": "н", "o": "о", "p": "р", "c": "с", "t": "т", "y": "у", "x": "х"})
    wrong = homoglyph = 0
    for source_id in source_ids:
        if scores[route][source_id]["main"]["fields"]["case_number"]["status"] != "wrong":
            continue
        wrong += 1
        row = read_json(results / route / f"{source_id}.json")
        prediction = _ab_main(row).get("case_number", "") if route in ("A", "B") else _cd_extract(row, labels[source_id])[0].get("case_number", "")
        expected = _gold_expected(labels[source_id]).get("case_number", "")
        visual = lambda value: _compact(_norm(value).translate(latin_to_cyrillic))
        if prediction and expected and visual(prediction) == visual(expected) and _norm(prediction) != _norm(expected):
            homoglyph += 1
    return {"wrong_count": wrong, "homoglyph_only_count": homoglyph}


def _metadata_attribution_diagnostic(
    route: str, source_ids: list[str], scores: dict[str, dict[str, dict[str, Any]]],
    labels: dict[str, dict[str, Any]], results: Path,
) -> dict[str, Any]:
    output = {}
    for field in METADATA_FIELDS:
        first_page = Counter()
        full_source = Counter()
        suppressed = rescued = leaf_correct = leaf_values = 0
        for source_id in source_ids:
            row = read_json(results / route / f"{source_id}.json")
            calls = row.get("calls", [])
            def page_num(call: dict[str, Any]) -> int:
                match = re.search(r":p(\d+)", str(call.get("page_id", "")))
                return int(match.group(1)) if match else 999
            ordered = sorted(calls, key=page_num)
            doc = (ordered[0].get("result", {}).get("document", {}) if ordered else {})
            atom = doc.get(field, {})
            predicted = atom.get("value") or atom.get("raw") or ""
            expected = _gold_expected(labels[source_id]).get(field)
            if not _expected_present(expected):
                first_status = "not_expected"
            elif not predicted:
                first_status = "missing"
            else:
                first_status = "correct" if _matches(field, predicted, expected) else "wrong"
            first_page[first_status] += 1
            full_status = scores[route][source_id]["main"]["fields"][field]["status"]
            full_source[full_status] += 1
            suppressed += first_status == "correct" and full_status != "correct"
            rescued += first_status != "correct" and full_status == "correct"
            terminal_values: list[str] = []
            def collect_leaf(call: dict[str, Any]) -> None:
                children = call.get("retry_calls", [])
                if children:
                    for child in children:
                        collect_leaf(child)
                    return
                candidate = (call.get("result", {}).get("document", {}) or {}).get(field, {})
                value = candidate.get("value") or candidate.get("raw") or ""
                if value:
                    terminal_values.append(value)
            for call in calls:
                collect_leaf(call)
            unique = {_compact(value) for value in terminal_values}
            leaf_values += len(unique)
            leaf_correct += any(_matches(field, value, expected) for value in terminal_values)
        output[field] = {
            "first_logical_page_status": dict(first_page),
            "full_source_projection_status": dict(full_source),
            "first_page_correct_but_full_projection_not_correct": suppressed,
            "full_projection_correct_when_first_page_not_correct": rescued,
            "sources_with_any_terminal_leaf_gold_correct_value": leaf_correct,
            "distinct_terminal_leaf_values": leaf_values,
        }
    return output


def _stage_totals(route: str, source_ids: list[str], manifest_sources: dict[str, dict[str, Any]], results: Path) -> dict[str, float]:
    totals = Counter()
    for source_id in source_ids:
        path = results / route / f"{source_id}.json"
        if not path.is_file():
            continue
        row = read_json(path)
        if route in ("B", "C", "D"):
            totals["manifest_preparation_s"] += float(manifest_sources[source_id].get("preparation_seconds") or 0)
        if route == "A":
            timing = row.get("timing", {})
            for key in ("parse_file_s", "projection_s", "total_s"):
                totals[key] += float(timing.get(key) or 0)
            continue
        if route == "B":
            timing = row.get("timing", {})
            for key in ("preparation_s", "ocr_s", "ocr_retry_s", "extraction_sources_projection_s", "total_s"):
                totals[key] += float(timing.get(key) or 0)
            continue
        reader = row.get("reader_timing", {})
        for key in ("preparation_s", "ocr_s", "ocr_retry_s", "extraction_sources_projection_s"):
            totals[f"B_reader_{key}"] += float(reader.get(key) or 0)
        calls = row.get("calls", [])
        def binding_seconds(call: dict[str, Any]) -> float:
            own = float((call.get("timing") or {}).get("source_binding_s") or 0)
            own += float(((call.get("initial_attempt") or {}).get("timing") or {}).get("source_binding_s") or 0)
            return own + sum(binding_seconds(child) for child in call.get("retry_calls", []) if isinstance(child, dict))
        model = sum(float((call.get("timing") or {}).get("model_s") or 0) for call in calls)
        binding = sum(binding_seconds(call) for call in calls)
        total = sum(float((call.get("timing") or {}).get("total_s") or 0) for call in calls)
        totals["model_call_total_s"] += total
        totals["llm_model_s"] += model
        totals["source_binding_s"] += binding
        totals["other_call_overhead_s"] += max(0.0, total - model - binding)
    if route in ("C", "D"):
        # Charge B reader time, including OCR used to corroborate D page quotes.
        b = _stage_totals("B", source_ids, manifest_sources, results)
        totals["B_reader_total_s"] = b.get("total_s", 0.0)
        totals["charged_total_s"] = totals["manifest_preparation_s"] + totals["B_reader_total_s"] + totals["model_call_total_s"]
    elif route == "B":
        totals["charged_total_s"] = totals["manifest_preparation_s"] + totals.get("total_s", 0.0)
    else:
        totals["charged_total_s"] = totals.get("total_s", 0.0)
    return {key: round(value, 6) for key, value in totals.items()}


def build_summary(metrics: dict[str, Any], manifest: dict[str, Any], gold: dict[str, Any], results: Path, rebind_seconds: float, reconcile_seconds: float) -> dict[str, Any]:
    labels = {row["source_id"]: row for row in gold.get("labels", [])}
    source_metrics = {row["source_id"]: row for row in metrics["sources"]}
    manifest_sources = {row["source_id"]: row for row in manifest.get("sources", [])}
    route_scores = {route: {row["source_id"]: row for row in metrics["routes"][route]["per_source"]} for route in ROUTES}
    all_ids = list(labels)
    holdout_ids = [source_id for source_id in all_ids if source_metrics[source_id]["split"] == "eval"]
    independent_ids = [source_id for source_id in all_ids if source_id != CONTROL_SOURCE_ID]

    # The same-act PDF is a control, not a separate C/D observation.
    filtered = json.loads(json.dumps(metrics))
    for route in ("C", "D"):
        rows = [row for row in metrics["routes"][route]["per_source"] if row["source_id"] != CONTROL_SOURCE_ID]
        filtered["routes"][route]["per_source"] = rows
        filtered["routes"][route]["source_count"] = len(rows)
        filtered["routes"][route]["grouped_holdout"] = _summarize_group(rows)
    summary = privacy_summary(filtered)
    summary.update({
        "report_status": "final results after fragment-scoped rebind; gold labels remain not human validated",
        "cohort": "A/B: all 22 sources. C/D: 21 independent non-PDF sources; same-act seven-page PDF reported separately.",
        "gold_sha256": metrics.get("reference", {}).get("gold_sha256"),
        "errata_sha256": metrics.get("reference", {}).get("errata_sha256"),
        "all22_source_count": len(all_ids),
        "holdout_source_count": len(holdout_ids),
        "independent_CD_source_count": len(independent_ids),
        "identity_and_evidence_limits": metrics.get("evidence_limitations"),
        "primary_selection_scope": metrics.get("primary_identity_alignment_scope"),
        "source_quote_limit": "C verified_quote and owner quotes indicate syntactic quote presence, not semantic ownership. D page quote OCR corroboration is page-level, not word-aligned; unverified page quotes remain separate.",
        "offline_rebind_reconcile_seconds": {"fragment_scoped_rebind_all44": rebind_seconds, "reconcile_all44": reconcile_seconds},
        "cpu_control": "inference/control complete; timing and outcomes remain separately reported by owner",
        "holdout_expected_scope": {},
        "all22_expected_scope": {},
        "routes_by_split": {},
        "A_baseline_proposal_pool": {},
        "case_number_homoglyph_diagnostic_not_scoring": {},
        "first_page_vs_full_source_metadata_diagnostic_not_scoring": {},
        "first_page_metadata_interpretation": "Page-to-source aggregation diagnostic only. It does not establish which historical/current heading is authoritative or prove model reading failure.",
        "processing_stage_totals_summed_seconds": {},
        "same_act_seven_page_pdf_control": {},
        "offline_postprocess_seconds": {"fragment_rebind_validation_all44": rebind_seconds, "reconcile_all44": reconcile_seconds},
    })

    for scope, ids in (("holdout_expected_scope", holdout_ids), ("all22_expected_scope", all_ids)):
        summary[scope] = {
            "known_facts": sum(len(_known_fields(source_id, labels, metrics)) for source_id in ids),
            "unknown_address_kind_scope": sum(_norm(_gold_expected(labels[source_id]).get("address_kind")) == "unknown" for source_id in ids),
            "excluded_reference_fields": sum(item.get("source_id") in ids for item in metrics.get("reference", {}).get("excluded_fields", [])),
        }

    for route in ROUTES:
        cohort_ids = all_ids if route in ("A", "B") else independent_ids
        for split, source_ids in (("holdout", holdout_ids), ("all_independent", cohort_ids)):
            summary["routes_by_split"].setdefault(route, {})[split] = _route_split(
                route, source_ids, route_scores, source_metrics, labels, metrics, results,
                grouped=(split == "holdout"),
            )
        summary["processing_stage_totals_summed_seconds"][route] = {
            "holdout": _stage_totals(route, holdout_ids, manifest_sources, results),
            "all_independent": _stage_totals(route, cohort_ids, manifest_sources, results),
        }

    for scope, source_ids in (("holdout", holdout_ids), ("all22", all_ids)):
        same = Counter()
        any_identity = Counter()
        primary_or_displayed = 0
        primary_displayed_or_same_identity = 0
        for source_id in source_ids:
            suggestions = source_metrics[source_id]["baseline_suggestions"]
            for field in FIELDS:
                same[field] += bool(suggestions["same_identity_correct_missing_main"].get(field))
                any_identity[field] += bool(suggestions["all_suggestions_correct"].get(field))
            if source_id not in route_scores["A"]:
                continue
            for field in _known_fields(source_id, labels, metrics):
                a = route_scores["A"][source_id]
                main_correct = a["main"]["fields"][field]["status"] == "correct"
                displayed_correct = a["displayed_review"]["fields"][field]["status"] == "correct"
                same_identity = bool(suggestions["same_identity_correct_missing_main"].get(field))
                primary_or_displayed += main_correct or displayed_correct
                primary_displayed_or_same_identity += main_correct or displayed_correct or same_identity
        summary["A_baseline_proposal_pool"][scope] = {
            "same_identity_correct_suggestion_sources_missing_main": dict(same),
            "any_identity_correct_suggestion_sources_missing_main": dict(any_identity),
            "unique_known_field_slots_A_main_or_displayed_correct": primary_or_displayed,
            "unique_known_field_slots_A_main_or_displayed_or_same_identity_suggestion_correct": primary_displayed_or_same_identity,
        }

    # Attribute correct C/D participant facts to genuinely new discoveries,
    # correct A primary output, or facts already available elsewhere in A's pool.
    for route in ("C", "D"):
        for scope, source_ids in (("holdout", holdout_ids), ("all_independent", independent_ids)):
            partition = Counter()
            for source_id in source_ids:
                if source_id not in route_scores[route] or source_id not in route_scores["A"]:
                    continue
                for field in _known_fields(source_id, labels, metrics):
                    if field not in PARTICIPANT_FIELDS or route_scores[route][source_id]["main"]["fields"][field]["status"] != "correct":
                        continue
                    if source_metrics[source_id]["incremental_correct_discovery_vs_A"][route].get(field):
                        partition["new_beyond_A_pool"] += 1
                    elif route_scores["A"][source_id]["main"]["fields"][field]["status"] == "correct":
                        partition["already_A_primary"] += 1
                    else:
                        partition["available_elsewhere_in_A_pool"] += 1
            summary["routes_by_split"][route][scope]["participant_fact_partition_vs_A"] = dict(partition)

    latin_to_cyrillic = str.maketrans({"a": "а", "b": "в", "e": "е", "k": "к", "m": "м", "h": "н", "o": "о", "p": "р", "c": "с", "t": "т", "y": "у", "x": "х"})
    def visual_key(value: Any) -> str:
        return _compact(_norm(value).translate(latin_to_cyrillic))
    for scope, source_ids in (("holdout", holdout_ids), ("all_independent", independent_ids)):
        summary["case_number_homoglyph_diagnostic_not_scoring"][scope] = {}
        for route in ROUTES:
            wrong = homoglyph = 0
            for source_id in source_ids:
                if route_scores[route][source_id]["main"]["fields"]["case_number"]["status"] != "wrong":
                    continue
                wrong += 1
                row = read_json(results / route / f"{source_id}.json")
                predicted = _ab_main(row).get("case_number", "") if route in ("A", "B") else _cd_extract(row, labels[source_id])[0].get("case_number", "")
                expected = _gold_expected(labels[source_id]).get("case_number", "")
                if predicted and expected and visual_key(predicted) == visual_key(expected) and _norm(predicted) != _norm(expected):
                    homoglyph += 1
            summary["case_number_homoglyph_diagnostic_not_scoring"][scope][route] = {"wrong_count": wrong, "homoglyph_only_count": homoglyph}

    for route in ("C", "D"):
        summary["first_page_vs_full_source_metadata_diagnostic_not_scoring"][route] = _metadata_attribution_diagnostic(route, holdout_ids, route_scores, labels, results)

    for route in ("C", "D"):
        row = read_json(results / route / f"{CONTROL_SOURCE_ID}.json")
        calls = row.get("calls", [])
        summary["same_act_seven_page_pdf_control"][route] = {
            "pages": manifest_sources[CONTROL_SOURCE_ID].get("page_count"),
            "calls": len(calls),
            "complete_calls": sum(not(call.get("incomplete") or call.get("error")) for call in calls),
            "incomplete_calls": sum(bool(call.get("incomplete") or call.get("error")) for call in calls),
            "call_total_seconds": round(sum(float((call.get("timing") or {}).get("total_s") or 0) for call in calls), 6),
            "model_seconds": round(sum(float((call.get("timing") or {}).get("model_s") or 0) for call in calls), 6),
            "independent_scoring": "excluded; control only",
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", required=True, type=Path, help="Private output from evaluate.py")
    parser.add_argument("--manifest", required=True, type=Path, help="Private corpus manifest")
    parser.add_argument("--gold", required=True, type=Path, help="Private gold label file; values are used only in memory")
    parser.add_argument("--results", required=True, type=Path, help="Local A/B/C/D result directories")
    parser.add_argument("--output", required=True, type=Path, help="Aggregate JSON output; file mode set to 0600")
    parser.add_argument("--rebind-seconds", type=float, default=0.270246)
    parser.add_argument("--reconcile-seconds", type=float, default=0.039113)
    args = parser.parse_args()
    summary = build_summary(read_json(args.metrics), read_json(args.manifest), read_json(args.gold), args.results, args.rebind_seconds, args.reconcile_seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    os.chmod(args.output, 0o600)


if __name__ == "__main__":
    main()
