"""Offline, private evaluation for the extraction pilot.

The private metrics file may contain source-level detail and must stay in local
private storage. The optional summary contains aggregates only and never emits
document or person values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

COMMON_FIELDS = (
    "name", "inn", "snils", "ogrn", "address", "address_kind",
    "doc_date", "case_number", "court",
)
DOCUMENT_FIELDS = {"doc_date", "case_number", "court"}
IDENTIFIER_FIELDS = {"inn", "snils", "ogrn"}
ROUTE_NAMES = ("A", "B", "C", "D")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return _text(value.get("value") or value.get("raw"))
    return str(value).strip()


def _norm(value: Any) -> str:
    value = unicodedata.normalize("NFKC", _text(value)).replace("ё", "е").replace("Ё", "Е")
    return " ".join(value.split()).casefold()


def _compact(value: Any) -> str:
    return "".join(ch for ch in _norm(value) if ch.isalnum())


def _date_norm(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return _compact(raw)


def _name_forms(value: Any) -> set[str]:
    raw = _text(value)
    # Identity uses only the explicit printed/normalized gold strings and
    # aliases. Morphological lemmas can collapse gendered surnames and are not
    # a safe identity key.
    return {_compact(raw)} if raw else set()


def _gold_expected(label: dict[str, Any]) -> dict[str, Any]:
    document = label.get("document") or {}
    primary = label.get("primary") or {}
    return {
        "name": [primary.get("name_printed"), primary.get("name_normalized"), *_as_values(primary.get("name_aliases")), *_as_values(primary.get("aliases"))],
        "inn": primary.get("inn_normalized") or primary.get("inn_printed"),
        "snils": primary.get("snils_normalized") or primary.get("snils_printed"),
        "ogrn": primary.get("ogrn_normalized") or primary.get("ogrn_printed"),
        "address": primary.get("address_raw") or primary.get("address_normalized"),
        "address_kind": primary.get("address_kind"),
        "doc_date": document.get("document_date") or document.get("doc_date"),
        "case_number": document.get("case_number"),
        "court": document.get("court_printed") or document.get("court"),
    }


def _is_present(value: Any) -> bool:
    return bool(_text(value))


def _as_values(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _matches(field: str, predicted: Any, expected: Any) -> bool:
    if not _is_present(predicted) or not _is_present(expected):
        return False
    if field == "name":
        pforms = _name_forms(predicted)
        eforms: set[str] = set()
        for item in expected if isinstance(expected, list) else [expected]:
            eforms.update(_name_forms(item))
        return bool(pforms & eforms)
    if field == "doc_date":
        return _date_norm(predicted) == _date_norm(expected)
    # Compact matching intentionally ignores punctuation only. For addresses,
    # every alphanumeric component (including postcode and apartment) remains.
    if field in IDENTIFIER_FIELDS | {"case_number", "court", "address"}:
        return _compact(predicted) == _compact(expected)
    if field == "address_kind":
        return _norm(predicted) == _norm(expected)
    return _norm(predicted) == _norm(expected)


def _expected_present(expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_is_present(x) for x in expected)
    return _is_present(expected)


def _field_status(field: str, predicted: Any, expected: Any) -> str:
    if field == "address_kind":
        if not _expected_present(expected):
            return "not_expected" if _norm(predicted) in ("", "unknown") else "unexpected_nonnull"
        if _norm(expected) == "unknown" and _norm(predicted) in ("", "unknown"):
            return "unknown_abstention"
        if _norm(predicted) in ("", "unknown"):
            return "missing"
    if not _expected_present(expected):
        return "unexpected_nonnull" if _is_present(predicted) else "not_expected"
    if not _is_present(predicted):
        return "missing"
    return "correct" if _matches(field, predicted, expected) else "wrong"


def _expected_primary_names(label: dict[str, Any]) -> list[str]:
    primary = label.get("primary") or {}
    vals = [primary.get("name_printed"), primary.get("name_normalized")]
    vals.extend(_as_values(primary.get("name_aliases")))
    vals.extend(_as_values(primary.get("aliases")))
    return [str(v) for v in vals if _is_present(v)]


def _extract_atom(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("raw")) or _text(value.get("value"))
    return _text(value)


def _atom_value(value: Any) -> str:
    return _text(value.get("value")) if isinstance(value, dict) else _text(value)


def _atom_raw(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("raw")) or _atom_value(value)
    return _text(value)


def _join_person(fields: dict[str, Any]) -> str:
    entity_name = _extract_atom(fields.get("legal_name")) or _extract_atom(fields.get("debtor_name"))
    if entity_name:
        return entity_name
    components = [_extract_atom(fields.get(k)) for k in ("last_name", "first_name", "middle_name")]
    joined = " ".join(x for x in components if x)
    return joined or _extract_atom(fields.get("fio_raw")) or _extract_atom(fields.get("name")) or _extract_atom(fields.get("short_name"))


def _ab_main(row: dict[str, Any]) -> dict[str, Any]:
    fields = row.get("fields") or {}
    return {
        "name": _join_person(fields),
        "inn": _extract_atom(fields.get("inn")),
        "snils": _extract_atom(fields.get("snils")),
        "ogrn": _extract_atom(fields.get("ogrn")),
        "address": _extract_atom(fields.get("address")),
        "address_kind": _extract_atom(fields.get("address_kind")),
        "doc_date": _extract_atom(fields.get("doc_date")),
        "case_number": _extract_atom(fields.get("case_number")),
        "court": _extract_atom(fields.get("court")),
    }


def _review_projection(row: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for item in row.get("review") or []:
        attr = item.get("attr")
        if attr in (*COMMON_FIELDS, "last_name", "first_name", "middle_name", "legal_name", "debtor_name", "short_name"):
            result[attr] = _text(item.get("value"))
    if not result.get("name"):
        result["name"] = result.get("legal_name") or result.get("debtor_name") or result.get("short_name", "")
    if not result.get("name"):
        names = [result.get(k, "") for k in ("last_name", "first_name", "middle_name")]
        result["name"] = " ".join(x for x in names if x)
    return result


def _party_name(party: dict[str, Any]) -> str:
    return _extract_atom(party.get("name")) or _text(party.get("name_printed")) or _text(party.get("name_normalized"))


def _party_matches_name(party: dict[str, Any], expected_names: list[str]) -> bool:
    atom = party.get("name")
    candidates = [atom.get("raw"), atom.get("value")] if isinstance(atom, dict) else [_party_name(party)]
    candidates.extend([party.get("name_printed"), party.get("name_normalized")])
    return any(_same_gold_name(candidate, expected_names) for candidate in candidates if _is_present(candidate))


def _same_gold_name(candidate: Any, expected_names: list[str]) -> bool:
    if not _is_present(candidate):
        return False
    return any(_matches("name", candidate, name) for name in expected_names)


def _candidate_key(field: str, value: Any) -> str:
    if field == "doc_date":
        return _date_norm(value)
    if field == "address_kind":
        return _norm(value)
    return _compact(value)


def _candidate_rank(candidate: dict[str, Any]) -> tuple[int, int, str, str]:
    evidence = candidate.get("evidence") or {}
    owner = candidate.get("owner_evidence") or {}
    return (
        int(candidate.get("normalization_support") == "supported"),
        int(evidence.get("status") == "verified_quote" and owner.get("status") == "verified_quote"),
        _norm(candidate.get("value")),
        _norm(candidate.get("raw")),
    )


def _candidate_record(candidate: dict[str, Any]) -> dict[str, Any]:
    evidence = candidate.get("evidence") or {}
    owner = candidate.get("owner_evidence") or {}
    return {
        "raw": _atom_raw(candidate), "value": _atom_value(candidate),
        "normalization_support": candidate.get("normalization_support", "not_checked"),
        "value_evidence_status": evidence.get("status", "missing"),
        "owner_evidence_status": owner.get("status", "missing"),
    }


def _cd_extract(row: dict[str, Any], label: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    payloads = []
    if isinstance(row.get("result"), dict):
        payloads = [row["result"]]
    else:
        for call in row.get("calls") or []:
            if isinstance(call.get("result"), dict):
                payloads.append(call["result"])
    expected_names = _expected_primary_names(label)
    all_parties = [p for payload in payloads for p in payload.get("participants", []) if isinstance(p, dict)]
    matched = [p for p in all_parties if _party_matches_name(p, expected_names)]
    alignment = "matched" if len(matched) == 1 else ("matched_multiple_occurrences" if len(matched) > 1 else "unaligned")
    field_conflicts: list[str] = []
    candidate_alternatives_by_field: dict[str, list[dict[str, Any]]] = {}
    selected: dict[str, Any] = {}
    if matched:
        # Same-identity page occurrences and repeated keys in one participant
        # are treated uniformly. Keep all distinct candidates; select only a
        # single normalized value, never the final list item by accident.
        fields_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        roles = []
        seen_roles = set()
        for party in matched:
            for field in party.get("fields", []) or []:
                if isinstance(field, dict) and field.get("key") and (_atom_value(field) or _atom_raw(field)):
                    fields_by_key[str(field["key"])].append(field)
            for role in party.get("roles", []) or []:
                marker = (_norm(role.get("role")), _norm(role.get("status")))
                if marker not in seen_roles:
                    seen_roles.add(marker)
                    roles.append(role)
        merged_fields = []
        for key, entries in sorted(fields_by_key.items()):
            candidates_by_value: dict[str, dict[str, Any]] = {}
            for entry in entries:
                canonical = _candidate_key(key, _atom_value(entry) or _atom_raw(entry))
                previous = candidates_by_value.get(canonical)
                if previous is None or _candidate_rank(entry) > _candidate_rank(previous):
                    candidates_by_value[canonical] = entry
            distinct_candidates = [candidates_by_value[k] for k in sorted(candidates_by_value)]
            if len(distinct_candidates) == 1:
                merged_fields.append(distinct_candidates[0])
            elif len(distinct_candidates) > 1:
                field_conflicts.append(key)
                candidate_alternatives_by_field[key] = [_candidate_record(x) for x in distinct_candidates]
        name_candidates = [p.get("name") for p in matched if p.get("name") is not None]
        selected_name = sorted(name_candidates, key=lambda x: (_norm(_atom_value(x)), _norm(_atom_raw(x))))[0] if name_candidates else None
        selected = {"name": selected_name, "fields": merged_fields, "roles": roles}
    party_fields = {}
    observations: dict[str, dict[str, Any]] = {}
    for f in selected.get("fields", []) or []:
        if isinstance(f, dict) and f.get("key"):
            key = f["key"]
            party_fields[key] = _atom_value(f) or _atom_raw(f)
            evidence = f.get("evidence") or {}
            owner_evidence = f.get("owner_evidence") or {}
            observations[key] = {"raw": _atom_raw(f), "value": _atom_value(f), "normalization_support": f.get("normalization_support", "not_checked"), "evidence_status": evidence.get("status", "missing"), "owner_evidence_status": owner_evidence.get("status", "missing")}
    doc = {}
    doc_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for payload in payloads:
        for key, atom in (payload.get("document") or {}).items():
            if key in DOCUMENT_FIELDS:
                val = _atom_value(atom) or _atom_raw(atom)
                if _is_present(val):
                    doc_candidates[key].append(atom if isinstance(atom, dict) else {"raw": val, "value": val})
    for key, atoms in doc_candidates.items():
        distinct = {_compact(_atom_value(atom) or _atom_raw(atom)) for atom in atoms}
        if len(distinct) == 1:
            atom = atoms[0]
            doc[key] = _atom_value(atom) or _atom_raw(atom)
            evidence = atom.get("evidence") or {}
            observations[key] = {"raw": _atom_raw(atom), "value": _atom_value(atom), "normalization_support": atom.get("normalization_support", "not_checked"), "evidence_status": evidence.get("status", "missing")}
        elif len(distinct) > 1:
            field_conflicts.append(key)
    if selected.get("name") is not None:
        atom = selected.get("name")
        evidence = atom.get("evidence") or {} if isinstance(atom, dict) else {}
        observations["name"] = {"raw": _atom_raw(atom), "value": _atom_value(atom), "normalization_support": atom.get("normalization_support", "not_checked") if isinstance(atom, dict) else "not_checked", "evidence_status": evidence.get("status", "missing")}
    main = {
        "name": (_atom_value(selected.get("name")) or _atom_raw(selected.get("name"))) if selected else "",
        "inn": party_fields.get("inn", ""),
        "snils": party_fields.get("snils", ""),
        "ogrn": party_fields.get("ogrn", ""),
        "address": party_fields.get("address", ""),
        "address_kind": party_fields.get("address_kind", ""),
        "doc_date": doc.get("doc_date", ""),
        "case_number": doc.get("case_number", ""),
        "court": doc.get("court", ""),
    }
    wrong_owner = []
    uncertain_owner = []
    gold = label.get("primary") or {}
    independently_annotated_names = []
    for gold_party in label.get("participants") or []:
        names = [gold_party.get("name_printed"), gold_party.get("name_normalized"), *_as_values(gold_party.get("aliases"))]
        if any(_is_present(name) for name in names):
            independently_annotated_names.append([name for name in names if _is_present(name)])
    for idx, party in enumerate(all_parties):
        if party in matched or _party_matches_name(party, expected_names):
            continue
        for f in party.get("fields", []) or []:
            key = f.get("key")
            if key in IDENTIFIER_FIELDS:
                expected = gold.get(f"{key}_normalized") or gold.get(f"{key}_printed")
                value = _text(f.get("value")) or _text(f.get("raw"))
                if _is_present(expected) and _matches(key, value, expected):
                    item = {"participant_index": idx, "field": key}
                    if any(_party_matches_name(party, gold_names) for gold_names in independently_annotated_names):
                        wrong_owner.append(item)
                    else:
                        uncertain_owner.append(item)
    return main, all_parties, {"identity_alignment": alignment, "wrong_owner_primary_identifiers": wrong_owner, "uncertain_owner_primary_identifiers": uncertain_owner, "field_conflicts": sorted(set(field_conflicts)), "candidate_alternatives_by_field": candidate_alternatives_by_field, "selected_fields": party_fields, "primary_alignment_basis": "gold-name alignment used only for evaluator scoring; not an operational projection"}, observations


def _suggestion_candidates(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in ((row.get("fields") or {}).get("review_suggestions") or []) if isinstance(s, dict)]


def _suggestion_values(suggestion: dict[str, Any]) -> dict[str, str]:
    raw = suggestion.get("fields") or {}
    values = {k: _extract_atom(v) for k, v in raw.items()}
    values["name"] = values.get("fio") or " ".join(values.get(k, "") for k in ("last_name", "first_name", "middle_name")).strip()
    return values


def _address_part(value: Any, part: str) -> str:
    raw = _norm(value)
    if part == "postal_code":
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", raw)
    elif part == "apartment_type":
        m = re.search(r"(квартира|кв|апартамент)\.?\s*№?\s*[\w/-]+", raw)
    else:
        m = re.search(r"(?:кв(?:артира)?|апартамент)\.?\s*№?\s*([\w/-]+)", raw)
    if not m:
        return ""
    return m.group(1) if part != "apartment_type" else m.group(1)


def _score_fields(predicted: dict[str, Any], expected: dict[str, Any], excluded: set[str] | None = None) -> dict[str, Any]:
    excluded = excluded or set()
    detail = {}
    counts = Counter()
    for field in COMMON_FIELDS:
        if field in excluded:
            detail[field] = {"status": "excluded_reference"}
            counts["excluded_reference"] += 1
            continue
        status = _field_status(field, predicted.get(field), expected.get(field))
        counts[status] += 1
        detail[field] = {"status": status}
        if field == "address" and _expected_present(expected.get(field)):
            for part in ("postal_code", "apartment", "apartment_type"):
                exp = _address_part(expected[field], part)
                got = _address_part(predicted.get(field), part)
                detail[f"address_{part}"] = {
                    "status": "not_expected" if not exp else ("missing" if not got else ("correct" if _compact(got) == _compact(exp) else "wrong"))
                }
    expected_n = sum(counts[k] for k in ("correct", "wrong", "missing", "unknown_abstention"))
    return {
        "fields": detail,
        "counts": {"correct": counts["correct"], "wrong": counts["wrong"], "missing": counts["missing"], "unknown_abstention": counts["unknown_abstention"], "unexpected_nonnull": counts["unexpected_nonnull"], "not_expected": counts["not_expected"], "excluded_reference": counts["excluded_reference"], "expected_nonnull": expected_n},
        "estimated_correction_proxy_wrong_plus_missing": counts["wrong"] + counts["missing"],
    }


def _gold_role_metrics(label: dict[str, Any], parties: list[dict[str, Any]]) -> dict[str, Any]:
    annotations = []
    for gold_party in label.get("participants") or []:
        roles = [r.get("role") for r in (gold_party.get("roles") or []) if r.get("role")]
        if roles:
            annotations.append((gold_party, roles))
    expected = correct = missed = 0
    for gold_party, roles in annotations:
        names = [gold_party.get("name_printed"), gold_party.get("name_normalized"), *_as_values(gold_party.get("aliases"))]
        aligned = [p for p in parties if _party_matches_name(p, [n for n in names if _is_present(n)])]
        got = {str(r.get("role", "")).casefold() for p in aligned for r in p.get("roles", []) if isinstance(r, dict)}
        expected += len(roles)
        correct += sum(1 for role in roles if str(role).casefold() in got)
        missed += sum(1 for role in roles if str(role).casefold() not in got)
    return {"annotated_participant_count": len(annotations), "expected_role_annotations": expected, "correct": correct, "missing": missed, "false_positive_metrics": "not_scored; gold participant coverage is partial" if (label.get("completeness") or {}).get("secondary_participants") == "partial" else "not_scored"}


def _evidence_statuses(row: dict[str, Any]) -> dict[str, int]:
    counts = Counter()
    payloads = [row.get("result")] if isinstance(row.get("result"), dict) else [c.get("result") for c in (row.get("calls") or [])]
    def visit(x: Any) -> None:
        if isinstance(x, dict):
            for evidence_key in ("evidence", "owner_evidence"):
                evidence = x.get(evidence_key)
                if isinstance(evidence, dict) and evidence.get("status"):
                    counts[f"{evidence_key}:{evidence['status']}"] += 1
            for v in x.values():
                visit(v)
        elif isinstance(x, list):
            for v in x:
                visit(v)
    for p in payloads:
        if p:
            visit(p)
    return dict(sorted(counts.items()))


def _timing(route: str, row: dict[str, Any] | None, b_row: dict[str, Any] | None, source: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest_prep_s = float((source or {}).get("preparation_seconds") or 0) if route in ("B", "C", "D") else 0.0
    if not row:
        return {"reader_s": 0.0, "manifest_preparation_s": manifest_prep_s, "llm_inference_s": 0.0, "source_binding_s": 0.0, "total_s": manifest_prep_s, "status": "result_missing"}
    if route in ("A", "B"):
        timing = row.get("timing") or {}
        prep = manifest_prep_s if route == "B" else 0.0
        reader = float(timing.get("total_s") or 0)
        return {"reader_s": reader, "manifest_preparation_s": prep, "llm_inference_s": 0.0, "source_binding_s": 0.0, "total_s": reader + prep, "status": "recorded"}
    calls = row.get("calls") or []
    infer = sum(float((c.get("timing") or {}).get("model_s") or 0) for c in calls)
    def leaf_binding(call: dict[str, Any]) -> float:
        value = float((call.get("timing") or {}).get("source_binding_s") or 0)
        value += float(((call.get("initial_attempt") or {}).get("timing") or {}).get("source_binding_s") or 0)
        return value + sum(leaf_binding(child) for child in call.get("retry_calls", []) if isinstance(child, dict))
    binding = sum(leaf_binding(c) for c in calls)
    call_total = sum(float((c.get("timing") or {}).get("total_s") or ((c.get("timing") or {}).get("model_s") or 0) + leaf_binding(c)) for c in calls)
    reader = row.get("reader_timing") or (b_row or {}).get("timing") or {}
    reader_s = float(reader.get("total_s") or 0)
    other_call_overhead = max(0.0, call_total - infer - binding)
    return {"reader_s": reader_s, "manifest_preparation_s": manifest_prep_s, "reader_basis": "B reader timing; includes OCR used for D source corroboration although D receives no B text" if route == "D" else "B reader timing for model input", "llm_inference_s": infer, "source_binding_s": binding, "other_call_overhead_s": other_call_overhead, "total_s": reader_s + manifest_prep_s + call_total, "status": "recorded"}


def _find_result(root: Path, route: str, source_id: str) -> tuple[dict[str, Any] | None, str]:
    candidates = [root / route / f"{source_id}.json", root / source_id / f"{route}.json", root / f"{source_id}-{route}.json"]
    for path in candidates:
        if path.is_file():
            try:
                row = json.loads(path.read_text())
                if row.get("route", route) == route:
                    if row.get("error"):
                        return row, "error"
                    calls = row.get("calls") or []
                    if row.get("incomplete") or any(c.get("incomplete") or c.get("error") for c in calls):
                        return row, "incomplete"
                    return row, "complete"
            except (OSError, json.JSONDecodeError):
                return {"error": "unreadable_result"}, "error"
    for path in (root / route / f"{source_id}.failure.json", root / f"{source_id}-{route}.failure.json"):
        if path.is_file():
            return {"error": "route_failure_recorded"}, "error"
    return None, "missing"


def _summarize_group(scores: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in scores:
        if item["split"] == "eval":
            groups[item["group_id"]].append(item)
    per_field: dict[str, list[float]] = defaultdict(list)
    overall = []
    group_rows = []
    for group, items in sorted(groups.items()):
        field_rates = {}
        source_rates = []
        for field in COMMON_FIELDS:
            denom = numer = 0
            for item in items:
                status = item["main"]["fields"][field]["status"]
                if status in ("correct", "wrong", "missing", "unknown_abstention"):
                    denom += 1
                    numer += status in ("correct", "unknown_abstention")
            if denom:
                field_rates[field] = numer / denom
                per_field[field].append(numer / denom)
        for item in items:
            counts = item["main"]["counts"]
            denom = counts["expected_nonnull"]
            if denom:
                source_rates.append((counts["correct"] + counts["unknown_abstention"]) / denom)
        if source_rates:
            group_rate = sum(source_rates) / len(source_rates)
            overall.append(group_rate)
        else:
            group_rate = None
        group_rows.append({"group_id": group, "source_count": len(items), "mean_source_accuracy": group_rate, "field_accuracy": field_rates})
    return {"interpretation": "Fixed assessed-state denominator; appropriate unknown-type abstention is a correct state, not a populated fact. Report populated correct counts separately.", "group_count": len(groups), "one_vote_per_group_mean_source_accuracy": sum(overall) / len(overall) if overall else None, "field_accuracy_macro_over_groups": {k: sum(v) / len(v) for k, v in per_field.items()}, "groups": group_rows}


def evaluate(gold: dict[str, Any], manifest: dict[str, Any], results_root: Path, reference_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    labels = {x["source_id"]: x for x in gold.get("labels", [])}
    manifest_by_id = {x["source_id"]: x for x in manifest.get("sources", [])}
    output = {"schema_version": "1", "gold_validation": "not_human_validated; label status=" + str(gold.get("status", "unknown")), "reference": reference_meta or {}, "name_reference_consistency": "printed and normalized names are both accepted exact variants; discrepancies are counted below", "primary_identity_alignment_scope": "Gold-name alignment selects candidates only for evaluator scoring; it is not operational card selection or proof of participant ownership.", "evidence_limitations": "Source presence and normalization support do not prove semantic ownership. C line quote statuses and owner quotes remain syntactic evidence; D page-level OCR corroboration is reported separately and is not exact word alignment.", "sources": [], "routes": {}}
    route_summary: dict[str, list[dict[str, Any]]] = {r: [] for r in ROUTE_NAMES}
    for source_id, label in sorted(labels.items()):
        source = manifest_by_id.get(source_id, {})
        expected = _gold_expected(label)
        excluded_fields = {x["field"] for x in (reference_meta or {}).get("excluded_fields", []) if x.get("source_id") == source_id}
        source_scores = []
        rows = {}
        states = {}
        for route in ROUTE_NAMES:
            row, state = _find_result(results_root, route, source_id)
            rows[route], states[route] = row, state
            if route in ("A", "B"):
                main = _ab_main(row) if row else {}
                parties = []
                review = _review_projection(row) if row else {}
            else:
                main, parties, alignment, observations = _cd_extract(row, label) if row else ({}, [], {"identity_alignment": "unaligned", "wrong_owner_primary_identifiers": [], "uncertain_owner_primary_identifiers": [], "field_conflicts": [], "candidate_alternatives_by_field": {}, "selected_fields": {}}, {})
                review = dict(main) if row else {}
            main_score = _score_fields(main, expected, excluded_fields)
            review_score = _score_fields(review, expected, excluded_fields) if review else _score_fields({}, expected, excluded_fields)
            roles = _gold_role_metrics(label, parties) if route in ("C", "D") else {"status": "not_applicable_to_baseline_projection"}
            score = {
                "source_id": source_id, "group_id": label.get("group_id", source.get("group_id", source_id)),
                "split": label.get("split", source.get("split", "unknown")), "route": route,
                "result_status": state, "main": main_score,
                "displayed_review": review_score,
                "identity_alignment": alignment.get("identity_alignment", "unaligned") if route in ("C", "D") else "baseline_card_projection",
                "wrong_owner_primary_identifiers": alignment.get("wrong_owner_primary_identifiers", []) if route in ("C", "D") else [],
                "uncertain_owner_primary_identifiers": alignment.get("uncertain_owner_primary_identifiers", []) if route in ("C", "D") else [],
                "field_conflicts": alignment.get("field_conflicts", []) if route in ("C", "D") else [],
                "candidate_alternatives_by_field": alignment.get("candidate_alternatives_by_field", {}) if route in ("C", "D") else {},
                "roles": roles,
                "source_status_counts": _evidence_statuses(rows[route]) if route in ("C", "D") and rows[route] else {},
                "timing": _timing(route, row, rows.get("B"), source),
            }
            if route in ("C", "D"):
                raw_prediction = {k: observations.get(k, {}).get("raw", "") for k in COMMON_FIELDS}
                gated_fields = IDENTIFIER_FIELDS | {"address", "address_kind"}
                safe_prediction = {k: (observations.get(k, {}).get("value", "") if k not in gated_fields or observations.get(k, {}).get("normalization_support") == "supported" else "") for k in COMMON_FIELDS}
                raw_excluded = excluded_fields | ({"name"} if source_id in (reference_meta or {}).get("raw_name_exclusion_sources", []) else set())
                score["raw_model_output"] = _score_fields(raw_prediction, expected, raw_excluded)
                score["normalized_model_value"] = _score_fields(main, expected, excluded_fields)
                score["identifier_address_type_normalization_gated_values"] = _score_fields(safe_prediction, expected, excluded_fields)
                score["normalization_gate_interpretation"] = "identifier/address/type values are gated on supported normalization; name/document metadata is retained, and this gate is not a semantic ownership or complete safety determination"
                score["normalization_support_counts"] = dict(Counter(str(observations.get(k, {}).get("normalization_support", "not_checked")) for k in COMMON_FIELDS if k in observations))
                score["source_provenance_by_field"] = {k: observations.get(k, {}).get("evidence_status", "missing") for k in COMMON_FIELDS if k in observations}
                field_evidence = {}
                for field in COMMON_FIELDS:
                    obs = observations.get(field, {})
                    if not obs.get("owner_evidence_status"):
                        continue
                    value_status = obs.get("evidence_status", "missing")
                    owner_status = obs.get("owner_evidence_status", "missing")
                    if value_status == owner_status == "verified_quote":
                        qualification = "value_and_owner_quotes_verified_syntactically"
                    elif value_status == owner_status == "page_quote_ocr_corroborated":
                        qualification = "page_level_value_and_owner_corroborated_not_word_aligned"
                    elif value_status == "verified_quote":
                        qualification = "value_quote_verified_owner_quote_not_verified"
                    else:
                        qualification = "not_qualified_by_value_and_owner_quote_pair"
                    field_evidence[field] = {"value_evidence_status": value_status, "owner_evidence_status": owner_status, "qualification": qualification, "semantic_ownership_proven": False}
                score["selected_participant_field_evidence"] = field_evidence
                score["source_interpretation"] = "quote/source checks establish syntactic presence only; they do not validate semantic ownership"
                proposals = {}
                for field, alternatives in alignment.get("candidate_alternatives_by_field", {}).items():
                    if field not in COMMON_FIELDS:
                        continue
                    correct = sum(1 for candidate in alternatives if _matches(field, candidate.get("value") or candidate.get("raw"), expected.get(field)))
                    supported_correct = sum(1 for candidate in alternatives if candidate.get("normalization_support") == "supported" and _matches(field, candidate.get("value"), expected.get(field)))
                    proposals[field] = {"candidate_count": len(alternatives), "correct_candidate_count": correct, "normalization_supported_correct_candidate_count": supported_correct, "useful_correct_missing_main": bool(main_score["fields"][field]["status"] == "missing" and correct > 0 and field not in excluded_fields)}
                score["same_identity_alternative_proposals"] = proposals
            source_scores.append(score)
            route_summary[route].append(score)
        # Suggestions are evaluated for A/B, with identity required for useful
        # credit. Correct values from any suggestion are reported separately.
        arow = rows.get("A") or {}
        a_main = _ab_main(arow)
        suggestions = _suggestion_candidates(arow)
        a_suggestion = {"same_identity_correct_missing_main": {}, "all_suggestions_correct": {}, "suggestion_count": len(suggestions)}
        for field in COMMON_FIELDS:
            expected_value = expected[field]
            missing_main = not _is_present(a_main.get(field))
            any_correct = field not in excluded_fields and any(_matches(field, _suggestion_values(s).get(field), expected_value) for s in suggestions)
            same_correct = field not in excluded_fields and any(_same_gold_name(_suggestion_values(s).get("name"), _expected_primary_names(label)) and _matches(field, _suggestion_values(s).get(field), expected_value) for s in suggestions)
            a_suggestion["all_suggestions_correct"][field] = bool(any_correct)
            a_suggestion["same_identity_correct_missing_main"][field] = bool(missing_main and same_correct)
        # C/D correctness is scored as extraction accuracy. Incremental discovery
        # is separate and only counts values absent from A's main, review, and
        # same-identity suggestion pool.
        baseline_pool = [a_main, _review_projection(arow)] + [_suggestion_values(s) for s in suggestions if _same_gold_name(_suggestion_values(s).get("name"), _expected_primary_names(label))]
        incremental = {}
        for route in ("C", "D"):
            cscore = next(s for s in source_scores if s["route"] == route)
            actual = _cd_extract(rows[route], label)[0] if rows.get(route) else {}
            incremental[route] = {
                field: bool(field not in excluded_fields and cscore["main"]["fields"][field]["status"] == "correct" and not any(_matches(field, pool.get(field), expected[field]) for pool in baseline_pool))
                for field in COMMON_FIELDS
            }
        primary = label.get("primary") or {}
        name_ref_mismatch = bool(_is_present(primary.get("name_printed")) and _is_present(primary.get("name_normalized")) and _compact(primary.get("name_printed")) != _compact(primary.get("name_normalized")))
        output["sources"].append({"source_id": source_id, "group_id": label.get("group_id", source.get("group_id", source_id)), "split": label.get("split", source.get("split", "unknown")), "primary_selection_basis": "gold-name alignment is evaluator-only and is not an operational projection", "name_reference_printed_normalized_mismatch": name_ref_mismatch, "raw_name_reference_excluded": source_id in (reference_meta or {}).get("raw_name_exclusion_sources", []) or "name" in excluded_fields, "excluded_reference_fields": sorted(excluded_fields), "expected_nonnull_fields": [k for k, v in expected.items() if _expected_present(v) and k not in excluded_fields], "routes": source_scores, "baseline_suggestions": a_suggestion, "incremental_correct_discovery_vs_A": incremental})
    for route, scores in route_summary.items():
        output["routes"][route] = {"source_count": len(scores), "per_source": scores, "grouped_holdout": _summarize_group(scores)}
    return output


def privacy_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    summary = {"gold_validation": metrics["gold_validation"], "primary_identity_alignment_scope": metrics.get("primary_identity_alignment_scope"), "evidence_limitations": metrics.get("evidence_limitations"), "route_summary": {}}
    for route, data in metrics["routes"].items():
        per_source = data["per_source"]
        fields = {}
        for field in COMMON_FIELDS:
            counts = Counter(s["main"]["fields"][field]["status"] for s in per_source)
            fields[field] = dict(sorted(counts.items()))
        summary["route_summary"][route] = {
            "source_count": data["source_count"], "field_status_counts": fields,
            "grouped_holdout": {k: v for k, v in data["grouped_holdout"].items() if k != "groups"},
            "total_timing_s": {"reader": sum(s["timing"]["reader_s"] for s in per_source), "manifest_preparation": sum(s["timing"].get("manifest_preparation_s", 0) for s in per_source), "llm_inference": sum(s["timing"]["llm_inference_s"] for s in per_source), "source_binding": sum(s["timing"]["source_binding_s"] for s in per_source), "charged_total": sum(s["timing"]["total_s"] for s in per_source)},
            "reader_time_basis": "B OCR/reader time is charged for D source corroboration although D receives no B text" if route == "D" else ("B OCR/reader time charged for C input" if route == "C" else "route-local parse/preparation timing"),
            "identity_alignment_counts": dict(Counter(s["identity_alignment"] for s in per_source)),
            "wrong_owner_identifier_count": sum(len(s["wrong_owner_primary_identifiers"]) for s in per_source),
            "uncertain_owner_identifier_count": sum(len(s["uncertain_owner_primary_identifiers"]) for s in per_source),
            "normalization_support_counts": dict(sum((Counter(s.get("normalization_support_counts", {})) for s in per_source), Counter())),
            "source_status_counts": dict(sum((Counter(s.get("source_status_counts", {})) for s in per_source), Counter())),
            "selected_participant_field_evidence_qualification_counts": dict(Counter(e.get("qualification") for s in per_source for e in s.get("selected_participant_field_evidence", {}).values())),
            "same_identity_alternative_candidates_preserved": sum(x.get("candidate_count", 0) for s in per_source for x in s.get("same_identity_alternative_proposals", {}).values()),
            "same_identity_alternative_correct_candidates": sum(x.get("correct_candidate_count", 0) for s in per_source for x in s.get("same_identity_alternative_proposals", {}).values()),
            "useful_correct_missing_main_same_identity_alternative_fields": sum(bool(x.get("useful_correct_missing_main")) for s in per_source for x in s.get("same_identity_alternative_proposals", {}).values()),
            "result_status_counts": dict(Counter(s["result_status"] for s in per_source)),
            "estimated_correction_proxy_wrong_plus_missing": sum(s["main"]["estimated_correction_proxy_wrong_plus_missing"] for s in per_source),
            "correction_proxy_definition": "wrong plus missing expected fields; not employee time",
            "name_reference_mismatch_source_count": sum(1 for s in metrics["sources"] if s["split"] == "eval" and s["name_reference_printed_normalized_mismatch"]),
            "raw_name_reference_caution_count": len(metrics.get("reference", {}).get("raw_reading_cautions", [])),
            "raw_name_reference_caution_note": "Some raw-name exactness references were marked unreliable; reviewed normalized variants remain the name reference.",
            "full_address_comparison": "whitespace/case/punctuation compacted; abbreviations are not expanded; all alphanumeric components including postcode and apartment must match",
        }
    return summary


def _write_private(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.chmod(path, 0o600)


def _apply_errata(gold: dict[str, Any], errata_path: Path | None, gold_bytes: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply narrow primary-label overrides in memory and record provenance."""
    corrected = json.loads(json.dumps(gold))
    meta: dict[str, Any] = {"gold_sha256": hashlib.sha256(gold_bytes).hexdigest(), "errata_applied": [], "excluded_fields": [], "raw_reading_cautions": [], "raw_name_exclusion_sources": [], "errata_sha256": None}
    if errata_path is None or not errata_path.is_file():
        return corrected, meta
    raw = errata_path.read_bytes()
    errata = json.loads(raw)
    meta["errata_sha256"] = hashlib.sha256(raw).hexdigest()
    labels = {x.get("source_id"): x for x in corrected.get("labels", [])}
    for override in errata.get("overrides", []):
        source_id = override.get("source_id")
        field = str(override.get("field", ""))
        key = field.removeprefix("primary.")
        if source_id not in labels or not key or key not in (labels[source_id].get("primary") or {}):
            raise ValueError(f"Invalid private gold erratum target: {source_id!r}/{field!r}")
        labels[source_id]["primary"][key] = override.get("value")
        value_hash = hashlib.sha256(_text(override.get("value")).encode("utf-8")).hexdigest()
        meta["errata_applied"].append({"source_id": source_id, "field": field, "reason": str(override.get("reason", "")), "value_sha256": value_hash})
    for exclusion in errata.get("exclude_fields", []):
        source_id = exclusion.get("source_id")
        field = str(exclusion.get("field", ""))
        if source_id not in labels or field not in COMMON_FIELDS:
            raise ValueError(f"Invalid private gold exclusion: {source_id!r}/{field!r}")
        meta["excluded_fields"].append({"source_id": source_id, "field": field, "reason": str(exclusion.get("reason", ""))})
    meta["raw_reading_cautions"] = [str(x) for x in errata.get("raw_reading_cautions", [])]
    meta["raw_name_exclusion_sources"] = sorted({source_id for caution in meta["raw_reading_cautions"] for source_id in re.findall(r"IMG_\d+", caution)})
    return corrected, meta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path, help="Results root containing A/, B/, C/, D/ subdirectories")
    parser.add_argument("--output", required=True, type=Path, help="Private detailed metrics JSON; file mode is set to 0600")
    parser.add_argument("--summary", required=True, type=Path, help="Privacy-safe aggregate summary JSON")
    parser.add_argument("--errata", type=Path, help="Optional private errata JSON; defaults to errata-private.json beside --gold")
    args = parser.parse_args()
    gold_bytes = args.gold.read_bytes()
    errata_path = args.errata or (args.gold.parent / "errata-private.json")
    gold, reference_meta = _apply_errata(json.loads(gold_bytes), errata_path if errata_path.is_file() else None, gold_bytes)
    metrics = evaluate(gold, json.loads(args.manifest.read_text()), args.results, reference_meta)
    _write_private(args.output, metrics)
    _write_private(args.summary, privacy_summary(metrics))


if __name__ == "__main__":
    main()
