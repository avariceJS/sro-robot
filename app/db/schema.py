"""Runtime schema compatibility policy shared by readiness and diagnostics."""

import hashlib

from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..settings import settings


EXPECTED_SCHEMA_REVISION = "f61c2a9d04e8"
TASK_STATUS_TRANSITION_FUNCTION_SHA256 = (
    "070bc07d789296b67915d75a7b0c59bceefb7a9f83bcf7b09555a74699d3e371"
)

_RUNTIME_GUARDS = {
    ("event", "event_append_only"): (
        27,
        "sro_reject_event_mutation",
        "before delete or update on public.event",
    ),
    ("event", "event_no_truncate"): (
        34,
        "sro_reject_event_mutation",
        "before truncate on public.event",
    ),
    ("event", "event_contract_validate"): (
        7,
        "sro_validate_event_insert",
        "before insert on public.event",
    ),
    ("task", "task_status_transition_guard"): (
        19,
        "sro_enforce_task_status_transition",
        "before update of status on public.task",
    ),
    ("task", "task_status_history_record_change"): (
        17,
        "sro_record_task_status_change",
        "after update of status on public.task",
    ),
    ("task_status_history", "task_status_history_append_only"): (
        27,
        "sro_reject_task_status_history_mutation",
        "before delete or update on public.task_status_history",
    ),
    ("task_status_history", "task_status_history_no_truncate"): (
        34,
        "sro_reject_task_status_history_mutation",
        "before truncate on public.task_status_history",
    ),
}

_RUNTIME_FUNCTIONS = {
    "sro_reject_event_mutation": (
        "exact",
        "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = "
        "'event rows are append-only'; END;",
    ),
    "sro_validate_event_insert": (
        "sha256",
        "ae9f2c6625b8e314be7c2af5b4897f80de893308e50a23906aed5503d01132fa",
    ),
    "sro_enforce_task_status_transition": (
        "sha256",
        TASK_STATUS_TRANSITION_FUNCTION_SHA256,
    ),
    "sro_record_task_status_change": (
        "exact",
        "BEGIN INSERT INTO public.task_status_history (task_id, from_status, to_status) "
        "VALUES (NEW.id, OLD.status::text, NEW.status::text); RETURN NEW; END;",
    ),
    "sro_reject_task_status_history_mutation": (
        "exact",
        "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = "
        "'task status history rows are append-only'; END;",
    ),
}


class SchemaRevisionUnavailable(RuntimeError):
    """The connected database is not at the application's exact migration head."""


def configure_postgresql_transaction_timeouts(connection: Connection) -> None:
    """Apply configured PostgreSQL lock and statement budgets to this transaction."""
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text(
            "SELECT set_config('lock_timeout', :lock_timeout, true), "
            "set_config('statement_timeout', :statement_timeout, true)"
        ),
        {
            "lock_timeout": f"{settings.database_lock_timeout_ms}ms",
            "statement_timeout": f"{settings.database_statement_timeout_ms}ms",
        },
    )


def configure_read_only_diagnostic_transaction(connection: Connection) -> None:
    """Make PostgreSQL metadata/readiness checks read-only and bound their SQL work."""
    if connection.dialect.name != "postgresql":
        return
    connection.execute(text("SET TRANSACTION READ ONLY"))
    configure_postgresql_transaction_timeouts(connection)


def require_current_schema_revision(connection: Connection) -> None:
    """Fail closed when the database revision is stale, missing, or ambiguous."""
    try:
        configure_read_only_diagnostic_transaction(connection)
        revision = MigrationContext.configure(connection).get_current_revision()
    except Exception:
        raise SchemaRevisionUnavailable from None
    if revision != EXPECTED_SCHEMA_REVISION:
        raise SchemaRevisionUnavailable


def runtime_guard_differences(connection: Connection) -> list[str]:
    """Read back critical triggers and constraints so readiness detects drift."""
    rows = connection.execute(
        text(
            "SELECT c.relname, t.tgname, t.tgtype, t.tgenabled, t.tgisinternal, "
            "p.proname, function_ns.nspname, pg_get_triggerdef(t.oid), "
            "p.prosrc, language.lanname, p.proconfig, p.prosecdef "
            "FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "JOIN pg_namespace function_ns ON function_ns.oid = p.pronamespace "
            "JOIN pg_language language ON language.oid = p.prolang "
            "WHERE n.nspname = 'public' AND ("
            "(c.relname = 'event' AND t.tgname IN ("
            "'event_append_only', 'event_no_truncate', 'event_contract_validate')) OR "
            "(c.relname = 'task' AND t.tgname IN ("
            "'task_status_transition_guard', 'task_status_history_record_change')) OR "
            "(c.relname = 'task_status_history' AND t.tgname IN ("
            "'task_status_history_append_only', 'task_status_history_no_truncate')))"
        )
    ).all()
    observed = {(row[0], row[1]): row[2:] for row in rows}
    differences = []
    if set(observed) != set(_RUNTIME_GUARDS):
        differences.append("critical runtime trigger set missing or ambiguous")
    for key, (expected_type, expected_function, definition_fragment) in _RUNTIME_GUARDS.items():
        record = observed.get(key)
        if record is None:
            continue
        trigger_type, enabled, internal, function_name, function_schema, definition = record[:6]
        normalized_definition = " ".join(definition.lower().split())
        if (
            trigger_type != expected_type
            or enabled != "O"
            or internal
            or function_name != expected_function
            or function_schema != "public"
            or definition_fragment not in normalized_definition
        ):
            differences.append(f"critical runtime trigger shape differs: {key[1]}")
    functions = {row[5]: (row[8], row[9], row[10], row[11]) for row in rows}
    if set(functions) != set(_RUNTIME_FUNCTIONS):
        differences.append("critical runtime trigger functions missing or ambiguous")
    else:
        for name, (check, expected) in _RUNTIME_FUNCTIONS.items():
            source, language, config, security_definer = functions[name]
            normalized_source = " ".join(source.split())
            if check == "exact":
                source_matches = normalized_source == expected
            elif check == "sha256":
                actual_hash = hashlib.sha256(normalized_source.encode("utf-8")).hexdigest()
                source_matches = actual_hash == expected
            else:
                raise RuntimeError("unsupported runtime function check")
            if (
                language != "plpgsql"
                or not config
                or "search_path=pg_catalog" not in config
                or security_definer
            ):
                source_matches = False
            if not source_matches:
                differences.append(f"critical runtime trigger function differs: {name}")
    metadata_constraints = connection.execute(
        text(
            "SELECT c.contype, c.convalidated, pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'public' AND t.relname = 'intake_operation' "
            "AND c.conname = 'ck_intake_operation_source_metadata_sha256'"
        )
    ).all()
    constraint_is_valid = False
    if len(metadata_constraints) == 1:
        constraint_type, validated, definition = metadata_constraints[0]
        normalized_definition = " ".join(
            definition.lower()
            .replace("(", "")
            .replace(")", "")
            .replace("::text", "")
            .split()
        )
        if normalized_definition.startswith("check "):
            normalized_definition = normalized_definition.removeprefix("check ")
        constraint_is_valid = (
            constraint_type == "c"
            and validated
            and normalized_definition
            == "source_metadata_sha256 is null or "
            "source_metadata_sha256 ~ '^[0-9a-f]{64}$'"
        )
    if not constraint_is_valid:
        differences.append(
            "critical runtime constraint missing or invalid: "
            "ck_intake_operation_source_metadata_sha256"
        )
    return differences
