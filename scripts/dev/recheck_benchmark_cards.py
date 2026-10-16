#!/usr/bin/env python3
"""Read-only exact-field audit for cards created by a synthetic benchmark."""
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

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.db.base import SessionLocal
from app.db.models import DebtorCardDraft, Document, Task
from app.settings import settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cohort", required=True)
    args = parser.parse_args()
    env_path = Path(os.environ.get("SRO_ROBOT_ENV_FILE", ""))
    db_url = make_url(settings.database_url)
    if (
        not str(env_path)
        or env_path.is_symlink()
        or not env_path.is_file()
        or env_path.stat().st_size != 0
        or stat.S_IMODE(env_path.stat().st_mode) & 0o077
        or db_url.host not in {"127.0.0.1", "localhost", "::1"}
        or not db_url.database
        or "benchmark" not in db_url.database.casefold()
    ):
        raise SystemExit("Refusing audit unless using an empty private env file and loopback benchmark database")

    manifest_path = args.corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_hash = {item["sha256"]: item for item in manifest["documents"]}
    expected_address = "101000, вымышленный город Макетный, улица Учебная, дом 1"
    outcomes = {
        field: {"exact": 0, "missing": 0, "mismatch": 0}
        for field in ("fio", "inn", "address", "case_number", "doc_date")
    }
    with SessionLocal() as db:
        cards = db.scalars(select(DebtorCardDraft).order_by(DebtorCardDraft.id)).all()
        for card in cards:
            task = db.get(Task, card.source_task_id)
            document = db.get(Document, task.document_id) if task else None
            if document is None or document.content_hash not in by_hash:
                raise AssertionError(f"Card {card.id} source is not in the pinned synthetic corpus")
            expected = by_hash[document.content_hash]
            values = card.values or {}
            actuals = {
                "fio": card.display_name,
                "inn": card.inn,
                "address": (values.get("address") or {}).get("value"),
                "case_number": (values.get("case_number") or {}).get("value"),
                "doc_date": (values.get("doc_date") or {}).get("value"),
            }
            expected_values = {
                "fio": expected["fio"],
                "inn": expected["inn"],
                "address": expected_address,
                "case_number": expected["case_number"],
                "doc_date": expected["doc_date"],
            }
            for field, actual in actuals.items():
                expected_value = expected_values[field]
                outcomes[field][
                    "exact" if actual == expected_value else "missing" if not actual else "mismatch"
                ] += 1
    result = {
        "cohort": args.cohort,
        "database_name": db_url.database,
        "corpus_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "cards_audited": len(cards),
        "field_outcomes": outcomes,
        "result": "pass" if all(not v["missing"] and not v["mismatch"] for v in outcomes.values()) else "findings",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
