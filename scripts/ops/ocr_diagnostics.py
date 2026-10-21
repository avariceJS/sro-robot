#!/usr/bin/env python3
"""Print privacy-safe aggregate OCR state from a PostgreSQL read-only snapshot.

Run with the application release Python and source on PYTHONPATH. This command
never selects task identifiers, stored paths, filenames, OCR values, warning
text, or exception messages. It does not contact Redis, OCR workers, or SROArbitr.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

TASK_STATUSES = (
    "new",
    "recognizing",
    "needs_review",
    "approved",
    "writing",
    "awaiting_preview",
    "done",
    "failed",
    "duplicate",
    "rejected",
)

# These are source labels emitted by the parser, not values from documents.
# Keep them fixed and expose no arbitrary JSONB source or warning strings.
SOURCE_CATEGORIES = ("not_found", "role_ambiguous", "role_partial")
CARD_FIELDS = (
    "doc_type", "doc_date", "case_number", "court", "last_name",
    "first_name", "middle_name", "fio_raw", "inn", "snils", "address",
    "address_kind", "entity_kind", "legal_name", "short_name",
    "legal_form", "debtor_name", "ogrn", "ogrnip", "kpp",
)
KNOWN_OCR_ERROR = "OCR worker attempts exhausted; operator review required"


def _status_query() -> str:
    values = ", ".join(f"'{status}'" for status in TASK_STATUSES)
    return f"""
        SELECT CASE WHEN status::text IN ({values}) THEN status::text ELSE 'other' END AS category,
               count(*)::bigint AS total
        FROM task
        GROUP BY category
    """


def _field_query() -> str:
    fields = ", ".join(f"('{name}')" for name in CARD_FIELDS)
    return f"""
        SELECT field.name,
               count(*) FILTER (
                   WHERE NULLIF(task.recognized -> field.name ->> 'value', '') IS NOT NULL
               )::bigint AS present,
               count(*) FILTER (
                   WHERE NULLIF(task.recognized -> field.name ->> 'warning', '') IS NOT NULL
               )::bigint AS warned,
               count(*) FILTER (
                   WHERE task.recognized -> field.name ->> 'source' = 'not_found'
               )::bigint AS not_found,
               count(*) FILTER (
                   WHERE task.recognized -> field.name ->> 'source' = 'role_ambiguous'
               )::bigint AS role_ambiguous,
               count(*) FILTER (
                   WHERE task.recognized -> field.name ->> 'source' = 'role_partial'
               )::bigint AS role_partial
        FROM task
        CROSS JOIN (VALUES {fields}) AS field(name)
        GROUP BY field.name
        ORDER BY field.name
    """


def collect_summary(
    session_factory: Any,
    connection_factory: Any,
    *,
    dialect: str,
    debug: bool,
) -> dict[str, Any]:
    """Run only fixed aggregate SELECTs under a transaction-level read-only guard."""
    if dialect != "postgresql" or debug:
        raise RuntimeError("diagnostic_requires_production_postgresql")
    from sqlalchemy import text

    connection = connection_factory()
    transaction = None
    session = None
    try:
        # The production engine's checkout role-policy hook performs a SELECT
        # before handing out the connection. Clear that completed preflight
        # transaction at the DBAPI level so PostgreSQL can set a repeatable,
        # read-only mode before this diagnostic's first statement.
        connection.connection.rollback()
        transaction = connection.begin()
        connection.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        )
        connection.execute(text("SET LOCAL search_path TO public, pg_temp"))
        # SessionLocal's after_begin hook now sets bounded timeouts inside the
        # already read-only snapshot.
        session = session_factory(bind=connection)
        session.begin()
        status_counts = {status: 0 for status in (*TASK_STATUSES, "other")}
        for row in session.execute(text(_status_query())):
            category = row[0]
            if category in status_counts:
                status_counts[category] = int(row[1])

        fields: dict[str, dict[str, Any]] = {
            name: {
                "present": 0,
                "warning_present": 0,
                "source_categories": {category: 0 for category in SOURCE_CATEGORIES},
            }
            for name in CARD_FIELDS
        }
        for row in session.execute(text(_field_query())):
            name = row[0]
            if name not in CARD_FIELDS:
                continue
            fields[name] = {
                "present": int(row[1]),
                "warning_present": int(row[2]),
                "source_categories": dict(zip(SOURCE_CATEGORIES, map(int, row[3:6]))),
            }

        error_row = session.execute(
            text(
                """SELECT count(*) FILTER (WHERE status::text = 'failed'
                                               AND COALESCE(error, '') <> '')::bigint,
                          count(*) FILTER (WHERE status::text = 'failed'
                                               AND error = :known_error)::bigint
                   FROM task"""
            ),
            {"known_error": KNOWN_OCR_ERROR},
        ).one()
        return {
            "tasks_by_status": status_counts,
            "failed_errors": {
                "with_message": int(error_row[0]),
                "known_ocr_attempts_exhausted": int(error_row[1]),
                "other_or_unclassified": max(0, int(error_row[0]) - int(error_row[1])),
            },
            "recognized_card_fields": fields,
        }
    finally:
        # Rollback is intentional even for the all-SELECT snapshot: it keeps
        # this command from ever committing an accidentally added statement.
        if session is not None:
            session.rollback()
            session.close()
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        connection.close()


def compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Keep the key review fields and roll the remaining card counts up."""
    primary = ("doc_type", "doc_date", "case_number", "court")
    fields = summary["recognized_card_fields"]
    other_names = tuple(name for name in CARD_FIELDS if name not in primary)
    categories = {
        category: sum(fields[name]["source_categories"][category] for name in other_names)
        for category in SOURCE_CATEGORIES
    }
    return {
        "tasks_by_status": summary["tasks_by_status"],
        "failed_errors": summary["failed_errors"],
        "primary_card_fields": {name: fields[name] for name in primary},
        "other_card_fields": {
            "field_count": len(other_names),
            "present_occurrences": sum(fields[name]["present"] for name in other_names),
            "warning_occurrences": sum(fields[name]["warning_present"] for name in other_names),
            "source_categories": categories,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compact",
        action="store_true",
        help="roll secondary card fields up for a short console report",
    )
    arguments = parser.parse_args(argv)
    try:
        from app.db.base import SessionLocal, engine
        from app.settings import settings

        summary = collect_summary(
            SessionLocal,
            engine.connect,
            dialect=engine.dialect.name,
            debug=settings.debug,
        )
    except Exception:
        # Do not print driver exceptions: they can contain connection details.
        print("ocr_diagnostic=failed reason=diagnostic_failed", file=sys.stderr)
        return 1

    if arguments.compact:
        summary = compact_summary(summary)
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
