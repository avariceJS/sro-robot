#!/usr/bin/env python3
"""Offline, aggregate-only comparison of observed OCR/source/registry fields.

This is a weak reconciliation aid, not gold truth, an accuracy estimate, or a
registry integration. `evidence=observed` means a human independently read the
value from the original act, or verified the text layer against its visible
original. An unverified embedded text layer is `weak_pdf_text` and is excluded
from scored comparisons. Input JSON and metrics remain in a private local folder.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_PAIRS = 10_000
MAX_FIELDS_PER_PAIR = 64
MAX_VALUE_LENGTH = 8_000
FAMILIES = {"appointment", "person", "firm"}
BINDINGS = {"comparable", "lookup", "not_in_act", "missing_source"}
EVIDENCE = {"observed", "weak_pdf_text", "approved_date", "registry_only"}
CASE_NUMBER = re.compile(r"(?:А[0-9]{1,3}|СИП)-[0-9]{1,8}/[0-9]{4}\Z")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
TAG = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,100}\.json\Z")
PUBLIC_FIELDS = {
    "address", "address_kind", "applicant", "birth_date", "case_number", "court",
    "debtor_birth_date", "debtor_inn", "debtor_name", "debtor_registration_address",
    "debtor_snils", "doc_date", "doc_type", "entity_kind", "fio_raw", "first_name",
    "hearing_date", "hearing_time", "inn", "judge_name", "kpp", "last_name",
    "legal_form", "legal_name", "middle_name", "ogrn", "ogrnip", "practitioner",
    "registration_address", "short_name", "snils",
}
CATEGORIES = (
    "all_equal", "ocr_matches_source_only", "registry_matches_source_only",
    "ocr_registry_match_only", "weak_match_only", "ocr_missing",
    "registry_missing", "both_missing", "disagreement", "unqualified",
)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("arguments_invalid") from None


def _case(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    from sro_robot.core.validators import normalize_case_number

    canonical = normalize_case_number(value)
    return canonical if CASE_NUMBER.fullmatch(canonical) else None


def _normal(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    return " ".join(value.split()).casefold().replace("ё", "е")


def _value(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_VALUE_LENGTH:
        raise ValueError("input_invalid") from None
    return value if value.strip() else None


def _tags(value: object) -> bool:
    if value is None:
        return False
    if (not isinstance(value, list) or len(value) > 16
            or any(not isinstance(tag, str) or not TAG.fullmatch(tag) for tag in value)):
        raise ValueError("input_invalid") from None
    return bool(value)


def _validate_pair(pair: object) -> dict:
    if not isinstance(pair, dict):
        raise ValueError("input_invalid") from None
    source_case = _case(pair.get("source_case"))
    registry_case = _case(pair.get("registry_case"))
    family = pair.get("family")
    tags_present = _tags(pair.get("tags")) if "tags" in pair else False
    fields = pair.get("fields")
    if family not in FAMILIES or not isinstance(fields, dict) or len(fields) > MAX_FIELDS_PER_PAIR:
        raise ValueError("input_invalid") from None
    diagnostic = pair.get("diagnostic")
    if diagnostic is not None and (not isinstance(diagnostic, str) or len(diagnostic) > MAX_VALUE_LENGTH):
        raise ValueError("input_invalid") from None
    normalized_fields = {}
    for name, item in fields.items():
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or not isinstance(item, dict):
            raise ValueError("input_invalid") from None
        if set(item) - {"source", "ocr", "registry", "binding", "evidence"}:
            raise ValueError("input_invalid") from None
        if not {"source", "ocr", "registry", "binding", "evidence"} <= item.keys():
            raise ValueError("input_invalid") from None
        binding, evidence = item["binding"], item["evidence"]
        if binding not in BINDINGS or evidence not in EVIDENCE:
            raise ValueError("input_invalid") from None
        normalized_fields[name] = (
            _value(item["source"]), _value(item["ocr"]), _value(item["registry"]), binding, evidence,
        )
    return {
        "source_case": source_case, "registry_case": registry_case,
        "family": family, "fields": normalized_fields, "tags_present": tags_present,
        "diagnostic_present": diagnostic is not None,
    }


def _case_signature(pair: dict) -> tuple:
    return pair["family"], pair["source_case"]


def _field_category(record: tuple[str | None, str | None, str | None, str, str]) -> tuple[str, str | None]:
    source, ocr, registry, binding, evidence = record
    if evidence != "observed":
        return "unqualified", evidence
    if binding != "comparable" or source is None:
        return "unqualified", binding if binding != "comparable" else "missing_source"
    if ocr is None and registry is None:
        return "both_missing", None
    if ocr is None:
        return "ocr_missing", None
    if registry is None:
        return "registry_missing", None
    if source == ocr == registry:
        return "all_equal", None
    if source == ocr:
        return "ocr_matches_source_only", None
    if source == registry:
        return "registry_matches_source_only", None
    if ocr == registry:
        return "ocr_registry_match_only", None
    source_n, ocr_n, registry_n = _normal(source), _normal(ocr), _normal(registry)
    if source_n == ocr_n or source_n == registry_n or ocr_n == registry_n:
        return "weak_match_only", None
    return "disagreement", None


def reconcile(data: object) -> dict:
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("pairs"), list):
        raise ValueError("input_invalid") from None
    raw_pairs = data["pairs"]
    if not 1 <= len(raw_pairs) <= MAX_PAIRS:
        raise ValueError("input_invalid") from None

    totals = Counter()
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for raw in raw_pairs:
        pair = _validate_pair(raw)
        totals["pairs_received"] += 1
        if pair["tags_present"]:
            totals["pairs_with_tags"] += 1
        if pair["diagnostic_present"]:
            totals["pairs_with_diagnostics"] += 1
        if pair["source_case"] is None or pair["registry_case"] is None:
            totals["rejected_unknown_case_pairs"] += 1
            continue
        if pair["source_case"] != pair["registry_case"]:
            totals["rejected_mismatched_case_pairs"] += 1
            continue
        totals["case_matched_pairs"] += 1
        grouped[_case_signature(pair)].append(pair)

    by_family: dict[str, dict] = {}
    by_field: dict[str, dict] = {}
    category_counts: dict[str, Counter] = defaultdict(Counter)
    qualifier_counts: dict[str, Counter] = defaultdict(Counter)
    families_seen = Counter()
    duplicate_rows = conflicts = 0
    for (family, _case_number), rows in grouped.items():
        families_seen[family] += 1
        totals["qualifying_case_groups"] += 1
        unique_rows = {tuple(sorted(row["fields"].items())): row for row in rows}
        duplicate_rows += len(rows) - len(unique_rows)
        per_field: dict[str, set] = defaultdict(set)
        for row in rows:
            for name, record in row["fields"].items():
                per_field[name].add(record)
        for name, records in per_field.items():
            totals["field_observations"] += 1
            if len(records) > 1:
                category, qualifier = "unqualified", "duplicate_conflict"
                conflicts += 1
            else:
                category, qualifier = _field_category(next(iter(records)))
            category_counts[family][category] += 1
            category_counts["all"][category] += 1
            qualifier_counts[family][qualifier or "none"] += 1
            qualifier_counts["all"][qualifier or "none"] += 1
            # Only known schema labels escape; custom labels collapse to a safe bucket.
            safe_field = name if name in PUBLIC_FIELDS else "other_field"
            by_field.setdefault(safe_field, {key: 0 for key in CATEGORIES})[category] += 1
    for family, count in families_seen.items():
        by_family[family] = {"case_groups": count,
                             "fields": dict(category_counts[family]),
                             "qualifiers": dict(qualifier_counts[family])}
    result = {
        "status": "observed_weak_comparison_not_gold",
        "scope": "source_ocr_existing_registry_fields",
        "pairs_received": totals["pairs_received"],
        "case_matched_pairs": totals["case_matched_pairs"],
        "pairs_with_tags": totals["pairs_with_tags"],
        "pairs_with_diagnostics": totals["pairs_with_diagnostics"],
        "field_observations": totals["field_observations"],
        "rejected_unknown_case_pairs": totals["rejected_unknown_case_pairs"],
        "rejected_mismatched_case_pairs": totals["rejected_mismatched_case_pairs"],
        "qualifying_case_groups": totals["qualifying_case_groups"],
        "duplicate_rows_removed": duplicate_rows,
        "duplicate_conflicts": conflicts,
        "categories": dict(category_counts["all"]),
        "qualifiers": dict(qualifier_counts["all"]),
        "by_family": by_family,
        "by_field": by_field,
        "interpretation": (
            "Only independently observed original-act values are scored; weak PDF text, approved dates, "
            "registry-only fields, and non-comparable bindings are unqualified. Unlisted or missing fields "
            "are unobserved, never assumed absent."
        ),
    }
    return result


def main(argv=None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument("--private-root", required=True, type=Path)
    parser.add_argument("--input-name", required=True)
    try:
        args = parser.parse_args(argv)
        if not SAFE_NAME.fullmatch(args.input_name):
            raise ValueError("arguments_invalid") from None
        from scripts.dev.quality.court_dataset import private_root, verify_storage, write_json

        if Path(args.private_root).expanduser().is_symlink():
            raise ValueError("arguments_invalid") from None
        root = private_root(args.private_root)
        verify_storage(root)
        input_path = root / args.input_name
        if input_path.is_symlink() or not input_path.is_file() or input_path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("input_invalid") from None
        data = json.loads(input_path.read_text(encoding="utf-8"))
        result = reconcile(data)
        write_json(root / "registry-compare-metrics.json", result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"status": "rejected", "reason": "arguments_or_private_input_invalid"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
