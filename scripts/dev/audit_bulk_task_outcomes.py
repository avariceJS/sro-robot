#!/usr/bin/env python3
"""Read-only expected-field and auto-preparation audit of a synthetic cohort."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.db.base import engine
from app.settings import settings

FIELDS = {
    "fio": ("fio_raw", "fio"),
    "inn": ("inn", "inn"),
    "case_number": ("case_number", "case_number"),
    "doc_date": ("doc_date", "doc_date"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cohort", required=True)
    args = parser.parse_args()
    env_path = Path(os.environ.get("SRO_ROBOT_ENV_FILE", ""))
    db_url = make_url(settings.database_url)
    if (
        not str(env_path) or env_path.is_symlink() or not env_path.is_file()
        or env_path.stat().st_size != 0 or stat.S_IMODE(env_path.stat().st_mode) & 0o077
        or db_url.host not in {"127.0.0.1", "localhost", "::1"}
        or not db_url.database or "benchmark" not in db_url.database.casefold()
    ):
        raise SystemExit("Refusing audit unless using an empty private env file and loopback benchmark database")

    manifest_path = args.corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_hash = {item["sha256"]: item for item in manifest["documents"]}
    outcomes = {key: {"exact": 0, "missing": 0, "mismatch": 0, "missing_filenames": [], "mismatches": []}
                for key in FIELDS}
    reasons: dict[str, int] = {}
    scan_reason_by_kind: dict[str, dict[str, int]] = {}
    rows_seen = 0
    cards = 0
    with engine.connect() as connection:
        with connection.begin():
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            rows = connection.execute(text("""
                SELECT d.filename, d.content_hash, t.recognized,
                       c.id AS card_id
                FROM task AS t
                JOIN document AS d ON d.id = t.document_id
                LEFT JOIN debtor_card_draft AS c ON c.source_task_id = t.id
                ORDER BY t.id
            """)).mappings().all()
    for row in rows:
        rows_seen += 1
        item = by_hash.get(row["content_hash"])
        if item is None or item["filename"] != row["filename"]:
            raise AssertionError(f"Task source is outside the pinned synthetic corpus: {row['filename']}")
        cards += int(row["card_id"] is not None)
        recognized = row["recognized"] or {}
        prep = recognized.get("_internal_preparation_v1") or {}
        reason = prep.get("reason") or ("prepared" if row["card_id"] else "no_preparation_metadata")
        reasons[reason] = reasons.get(reason, 0) + 1
        if item["kind"].startswith("scan_"):
            kinds = scan_reason_by_kind.setdefault(item["kind"], {})
            kinds[reason] = kinds.get(reason, 0) + 1
        for output_name, (recognized_name, expected_name) in FIELDS.items():
            actual = ((recognized.get(recognized_name) or {}).get("value"))
            expected = item[expected_name]
            if actual == expected:
                outcomes[output_name]["exact"] += 1
            elif not actual:
                outcomes[output_name]["missing"] += 1
                outcomes[output_name]["missing_filenames"].append(row["filename"])
            else:
                outcomes[output_name]["mismatch"] += 1
                outcomes[output_name]["mismatches"].append(
                    {"filename": row["filename"], "expected": expected, "actual": actual}
                )

    result = {
        "cohort": args.cohort,
        "database_name": db_url.database,
        "corpus_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "tasks_audited": rows_seen,
        "auto_cards_created": cards,
        "field_outcomes": outcomes,
        "preparation_reason_counts": reasons,
        "scan_preparation_reasons_by_kind": scan_reason_by_kind,
        "result": "pass" if rows_seen and all(
            not field["mismatches"] for field in outcomes.values()
        ) else "findings",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
