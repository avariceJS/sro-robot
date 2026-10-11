"""Runtime schema compatibility policy shared by readiness and diagnostics."""

from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection


EXPECTED_SCHEMA_REVISION = "c4a781b6d902"

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


class SchemaRevisionUnavailable(RuntimeError):
    """The connected database is not at the application's exact migration head."""


def require_current_schema_revision(connection: Connection) -> None:
    """Fail closed when the database revision is stale, missing, or ambiguous."""
    try:
        revision = MigrationContext.configure(connection).get_current_revision()
    except Exception:
        raise SchemaRevisionUnavailable from None
    if revision != EXPECTED_SCHEMA_REVISION:
        raise SchemaRevisionUnavailable


def runtime_guard_differences(connection: Connection) -> list[str]:
    """Read back enabled critical triggers so readiness detects managed-schema drift."""
    rows = connection.execute(
        text(
            "SELECT c.relname, t.tgname, t.tgtype, t.tgenabled, t.tgisinternal, "
            "p.proname, function_ns.nspname, pg_get_triggerdef(t.oid) "
            "FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "JOIN pg_namespace function_ns ON function_ns.oid = p.pronamespace "
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
        trigger_type, enabled, internal, function_name, function_schema, definition = record
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
    return differences
