"""Runtime schema compatibility policy shared by readiness and diagnostics."""

import hashlib

from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..settings import settings


EXPECTED_SCHEMA_REVISION = "a5f9c1d84b27"
TASK_STATUS_TRANSITION_FUNCTION_SHA256 = (
    "070bc07d789296b67915d75a7b0c59bceefb7a9f83bcf7b09555a74699d3e371"
)

_RUNTIME_GUARDS = {
    ("debtor_card_draft", "debtor_card_record_revision"): (
        21, "sro_record_debtor_card_revision",
        'after insert or update of entity_kind, display_name, inn, "values", preparation_state, version, updated_by_id on public.debtor_card_draft',
    ),
    ("debtor_card_draft_revision", "debtor_card_revision_append_only"): (
        27, "sro_reject_assignment_draft_revision_mutation", "before delete or update on public.debtor_card_draft_revision",
    ),
    ("debtor_card_draft_revision", "debtor_card_revision_no_truncate"): (
        34, "sro_reject_assignment_draft_revision_mutation", "before truncate on public.debtor_card_draft_revision",
    ),
    ("debtor_card_draft_revision", "debtor_card_revision_require_trigger"): (
        7, "sro_require_assignment_draft_revision_trigger", "before insert on public.debtor_card_draft_revision",
    ),
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
    ("task_status_history", "task_status_history_require_transition"): (
        7,
        "sro_require_triggered_status_history_insert",
        "before insert on public.task_status_history",
    ),
    ("user", "user_revoke_login_sessions_on_deactivation"): (
        17,
        "sro_revoke_sessions_on_user_deactivation",
        'after update of is_active on public."user"',
    ),
    ("login_session", "login_session_write_guard"): (
        23,
        "sro_guard_login_session_write",
        "before insert or update on public.login_session",
    ),
    ("assignment_draft", "assignment_draft_record_revision"): (
        21,
        "sro_record_assignment_draft_revision",
        'after insert or update of debtor_card_id, source_task_id, case_number, status, "values", version, updated_by_id on public.assignment_draft',
    ),
    ("assignment_draft_revision", "assignment_draft_revision_append_only"): (
        27,
        "sro_reject_assignment_draft_revision_mutation",
        "before delete or update on public.assignment_draft_revision",
    ),
    ("assignment_draft_revision", "assignment_draft_revision_no_truncate"): (
        34,
        "sro_reject_assignment_draft_revision_mutation",
        "before truncate on public.assignment_draft_revision",
    ),
    ("case_package_task_event", "case_package_task_event_append_only"): (
        27,
        "sro_reject_case_package_task_event_mutation",
        "before delete or update on public.case_package_task_event",
    ),
    ("case_package_task_event", "case_package_task_event_no_truncate"): (
        34,
        "sro_reject_case_package_task_event_mutation",
        "before truncate on public.case_package_task_event",
    ),
    ("case_package", "case_package_identity_immutable"): (
        27,
        "sro_reject_case_package_mutation",
        "before delete or update on public.case_package",
    ),
    ("case_package", "case_package_no_truncate"): (
        34,
        "sro_reject_case_package_mutation",
        "before truncate on public.case_package",
    ),
    ("intake_item", "intake_item_append_only"): (
        27, "sro_reject_intake_item_mutation", "before delete or update on public.intake_item",
    ),
    ("intake_item", "intake_item_no_truncate"): (
        34, "sro_reject_intake_item_mutation", "before truncate on public.intake_item",
    ),
    ("ocr_retry_run", "ocr_retry_run_validate_insert"): (
        7, "sro_validate_ocr_retry_insert", "before insert on public.ocr_retry_run",
    ),
    ("ocr_retry_run", "ocr_retry_run_append_only"): (
        27, "sro_reject_ocr_retry_mutation", "on public.ocr_retry_run",
    ),
    ("ocr_retry_run", "ocr_retry_run_no_truncate"): (
        34, "sro_reject_ocr_retry_mutation", "before truncate on public.ocr_retry_run",
    ),
    ("assignment_draft_revision", "assignment_draft_revision_require_trigger"): (
        7,
        "sro_require_assignment_draft_revision_trigger",
        "before insert on public.assignment_draft_revision",
    ),
}

_RUNTIME_FUNCTIONS = {
    "sro_record_debtor_card_revision": (
        "exact", "BEGIN INSERT INTO public.debtor_card_draft_revision "
        "(debtor_card_id, revision, snapshot, changed_by_id) "
        "VALUES (NEW.id, NEW.version, jsonb_build_object( "
        "'entity_kind', NEW.entity_kind, 'display_name', NEW.display_name, "
        "'inn', NEW.inn, 'values', NEW.values, "
        "'preparation_state', NEW.preparation_state ), NEW.updated_by_id); RETURN NEW; END;",
    ),
    "sro_reject_event_mutation": (
        "exact",
        "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = "
        "'event rows are append-only'; END;",
    ),
    "sro_validate_event_insert": (
        "sha256",
        "f01da81d770b8f6ed92d7614582ad9b76ff88cb6b29bcafcd28c1fd6fa481a5b",
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
    "sro_require_triggered_status_history_insert": (
        "exact",
        "BEGIN IF pg_catalog.pg_trigger_depth() < 2 THEN RAISE EXCEPTION USING "
        "ERRCODE = '55000', MESSAGE = 'task status history rows require a status "
        "transition'; END IF; RETURN NEW; END;",
    ),
    "sro_revoke_sessions_on_user_deactivation": (
        "exact",
        "BEGIN IF OLD.is_active IS DISTINCT FROM NEW.is_active THEN "
        "UPDATE public.login_session SET revoked_at = pg_catalog.clock_timestamp() "
        "WHERE user_id = NEW.id AND revoked_at IS NULL AND expires_at > "
        "pg_catalog.clock_timestamp(); END IF; RETURN NEW; END;",
    ),
    "sro_guard_login_session_write": (
        "exact",
        "DECLARE account_is_active boolean; BEGIN IF TG_OP = 'UPDATE' THEN IF "
        "NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.token_sha256 IS DISTINCT FROM "
        "OLD.token_sha256 OR NEW.created_at IS DISTINCT FROM OLD.created_at OR "
        "NEW.expires_at IS DISTINCT FROM OLD.expires_at OR (OLD.revoked_at IS NOT NULL "
        "AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN RAISE EXCEPTION USING "
        "ERRCODE = '23514', MESSAGE = 'login session identity and revocation are "
        "immutable'; END IF; RETURN NEW; END IF; SELECT is_active INTO account_is_active "
        "FROM public.\"user\" WHERE id = NEW.user_id FOR UPDATE; IF NOT FOUND OR "
        "account_is_active IS NOT TRUE THEN RAISE EXCEPTION USING ERRCODE = '23514', "
        "MESSAGE = 'login session requires an active account'; END IF; RETURN NEW; END;",
    ),
    "sro_record_assignment_draft_revision": (
        "exact",
        "BEGIN INSERT INTO public.assignment_draft_revision "
        "(assignment_draft_id, revision, status, snapshot, changed_by_id) "
        "VALUES (NEW.id, NEW.version, NEW.status, jsonb_build_object( "
        "'debtor_card_id', NEW.debtor_card_id, 'source_task_id', NEW.source_task_id, "
        "'case_number', NEW.case_number, "
        "'values', NEW.values ), NEW.updated_by_id); RETURN NEW; END;",
    ),
    "sro_reject_assignment_draft_revision_mutation": (
        "exact",
        "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = "
        "'assignment draft revisions are append-only'; END;",
    ),
    "sro_require_assignment_draft_revision_trigger": (
        "exact",
        "BEGIN IF pg_catalog.pg_trigger_depth() < 2 THEN RAISE EXCEPTION USING "
        "ERRCODE = '55000', MESSAGE = 'assignment revisions require a draft write'; "
        "END IF; RETURN NEW; END;",
    ),
    "sro_reject_case_package_task_event_mutation": (
        "exact",
        "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = "
        "'case package task events are append-only'; END;",
    ),
    "sro_reject_case_package_mutation": (
        "exact",
        "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = "
        "'case package identity is immutable'; END;",
    ),
    "sro_validate_ocr_retry_insert": (
        "sha256",
        "584be4c50a23d9ec2217e5620f7d535ab2e2268e1fa8139a2db8edc354d9dd38",
    ),
    "sro_reject_intake_item_mutation": (
        "exact",
        "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = "
        "'intake items are append-only'; END;",
    ),
    "sro_reject_ocr_retry_mutation": (
        "exact",
        "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = "
        "'OCR retry lineage is append-only'; END;",
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


def _normalize_case_package_constraint(definition: str) -> str:
    """Normalize PostgreSQL formatting while preserving SQL string literal case."""
    output: list[str] = []
    single_quoted = False
    double_quoted = False
    index = 0
    casts = ("::character varying", "::text[]", "::text")
    while index < len(definition):
        character = definition[index]
        if single_quoted:
            output.append(character)
            if character == "'":
                if index + 1 < len(definition) and definition[index + 1] == "'":
                    output.append("'")
                    index += 2
                    continue
                single_quoted = False
            index += 1
            continue
        if double_quoted:
            if character == '"':
                if index + 1 < len(definition) and definition[index + 1] == '"':
                    output.append('"')
                    index += 2
                    continue
                double_quoted = False
            else:
                output.append(character)
            index += 1
            continue
        if character == "'":
            single_quoted = True
            output.append(character)
            index += 1
            continue
        if character == '"':
            double_quoted = True
            index += 1
            continue
        cast = next(
            (item for item in casts if definition[index:index + len(item)].lower() == item),
            None,
        )
        if cast is not None:
            index += len(cast)
            continue
        if character.isspace() or character in "()":
            index += 1
            continue
        output.append(character.lower())
        index += 1
    return "".join(output)


def runtime_guard_differences(connection: Connection) -> list[str]:
    """Read back required tables, triggers and constraints so readiness detects drift."""
    from .base import Base
    from . import models as _models  # Register application tables for readiness checks.

    missing_tables = []
    if connection.dialect.name == "postgresql":
        expected_tables = {
            table.name for table in Base.metadata.tables.values()
            if table.schema in (None, "public")
        }
        actual_tables = set(connection.execute(
            text(
                "SELECT tablename FROM pg_catalog.pg_tables "
                "WHERE schemaname = 'public'"
            )
        ).scalars())
        missing_tables = sorted(expected_tables - actual_tables)
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
            "'task_status_history_append_only', 'task_status_history_no_truncate', "
            "'task_status_history_require_transition')) OR "
            "(c.relname = 'user' AND t.tgname IN ("
            "'user_revoke_login_sessions_on_deactivation')) OR "
            "(c.relname = 'login_session' AND t.tgname IN ("
            "'login_session_write_guard')) OR "
            "(c.relname = 'assignment_draft' AND t.tgname IN ("
            "'assignment_draft_record_revision')) OR "
            "(c.relname = 'assignment_draft_revision' AND t.tgname IN ("
            "'assignment_draft_revision_append_only', "
            "'assignment_draft_revision_no_truncate', "
            "'assignment_draft_revision_require_trigger')) OR "
            "(c.relname = 'case_package_task_event' AND t.tgname IN ("
            "'case_package_task_event_append_only', 'case_package_task_event_no_truncate')) OR "
            "(c.relname = 'case_package' AND t.tgname IN ("
            "'case_package_identity_immutable', 'case_package_no_truncate')) OR "
            "(c.relname = 'ocr_retry_run' AND t.tgname IN ("
            "'ocr_retry_run_validate_insert', 'ocr_retry_run_append_only', "
            "'ocr_retry_run_no_truncate')) OR "
            "(c.relname = 'intake_item' AND t.tgname IN ("
            "'intake_item_append_only', 'intake_item_no_truncate')) OR "
            "(c.relname = 'debtor_card_draft' AND t.tgname = 'debtor_card_record_revision') OR "
            "(c.relname = 'debtor_card_draft_revision' AND t.tgname IN ("
            "'debtor_card_revision_append_only', 'debtor_card_revision_no_truncate', "
            "'debtor_card_revision_require_trigger')))"
        )
    ).all()
    observed = {(row[0], row[1]): row[2:] for row in rows}
    differences = [f"required runtime table missing: {name}" for name in missing_tables]
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
    assignment_guards = connection.execute(
        text(
            "SELECT c.conname, c.contype, c.convalidated, pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'public' AND t.relname = 'assignment_draft' "
            "AND c.conname IN ('uq_assignment_draft_source_task', "
            "'fk_assignment_draft_source_task')"
        )
    ).all()
    assignment_guards = {row[0]: row[1:] for row in assignment_guards}
    if set(assignment_guards) != {
        "uq_assignment_draft_source_task", "fk_assignment_draft_source_task"
    }:
        differences.append("assignment source-task identity guards missing or ambiguous")
    else:
        unique = assignment_guards["uq_assignment_draft_source_task"]
        foreign = assignment_guards["fk_assignment_draft_source_task"]
        if unique[0] != "u" or not unique[1] or "unique (source_task_id)" not in unique[2].lower():
            differences.append("assignment source-task uniqueness guard missing or invalid")
        if foreign[0] != "f" or not foreign[1] or "foreign key (source_task_id) references task(id)" not in foreign[2].lower():
            differences.append("assignment source-task foreign key missing or invalid")
    kad_guards = connection.execute(text(
        "SELECT t.relname, c.conname, c.contype, c.convalidated, "
        "pg_get_constraintdef(c.oid) "
        "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname='public' AND t.relname IN "
        "('kad_source_run', 'kad_import_request_receipt') AND c.conname IN ("
        "'uq_kad_source_run_generation', 'uq_kad_source_run_operation', "
        "'uq_kad_source_run_task', 'fk_kad_source_run_parent_identity', "
        "'ck_kad_source_run_identity_sha256', 'ck_kad_source_run_generation_positive', "
        "'ck_kad_source_run_parent_generation', 'kad_import_request_receipt_pkey', "
        "'ck_kad_import_receipt_request_hash', 'ck_kad_import_receipt_manifest_hash', "
        "'ck_kad_import_receipt_source_metadata_hash')"
    )).all()
    kad_guards = {(row[0], row[1]): row[2:] for row in kad_guards}
    expected_kad_guards = {
        ("kad_source_run", "uq_kad_source_run_generation"): "u",
        ("kad_source_run", "uq_kad_source_run_operation"): "u",
        ("kad_source_run", "uq_kad_source_run_task"): "u",
        ("kad_source_run", "fk_kad_source_run_parent_identity"): "f",
        ("kad_source_run", "ck_kad_source_run_identity_sha256"): "c",
        ("kad_source_run", "ck_kad_source_run_generation_positive"): "c",
        ("kad_source_run", "ck_kad_source_run_parent_generation"): "c",
        ("kad_import_request_receipt", "kad_import_request_receipt_pkey"): "p",
        ("kad_import_request_receipt", "ck_kad_import_receipt_request_hash"): "c",
        ("kad_import_request_receipt", "ck_kad_import_receipt_manifest_hash"): "c",
        ("kad_import_request_receipt", "ck_kad_import_receipt_source_metadata_hash"): "c",
    }
    if set(kad_guards) != set(expected_kad_guards):
        differences.append("KAD source-operation identity constraints missing or ambiguous")
    else:
        for key, expected_type in expected_kad_guards.items():
            actual_type, validated, definition = kad_guards[key]
            if actual_type != expected_type or not validated:
                differences.append(f"KAD source-operation identity constraint invalid: {key[1]}")
                continue
            normalized = " ".join(definition.lower().split())
            if key[1] == "uq_kad_source_run_generation" and "unique (source_identity_sha256, generation)" not in normalized:
                differences.append("KAD source run generation uniqueness differs")
            elif key[1] == "fk_kad_source_run_parent_identity" and (
                "foreign key (parent_run_id, source_identity_sha256) references kad_source_run(id, source_identity_sha256)"
                not in normalized
            ):
                differences.append("KAD source run parent identity binding differs")
    ocr_retry_guards = connection.execute(text(
        "SELECT c.conname, c.contype, c.convalidated, pg_get_constraintdef(c.oid) "
        "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname='public' AND t.relname='ocr_retry_run'"
    )).all()
    ocr_retry_guards = {row[0]: row[1:] for row in ocr_retry_guards}
    expected_ocr_retry_guards = {
        "ocr_retry_run_pkey": ("p", "primary key (id)"),
        "uq_ocr_retry_run_parent": ("u", "unique (parent_task_id)"),
        "uq_ocr_retry_run_failure": ("u", "unique (failure_history_id)"),
        "uq_ocr_retry_run_child": ("u", "unique (child_task_id)"),
        "ck_ocr_retry_distinct_tasks": ("c", "check (parent_task_id <> child_task_id)"),
        "fk_ocr_retry_parent_document": (
            "f", "foreign key (parent_task_id, document_id) references task(id, document_id)"
        ),
        "fk_ocr_retry_failure_parent": (
            "f", "foreign key (failure_history_id, parent_task_id) references task_status_history(id, task_id)"
        ),
        "fk_ocr_retry_child_document": (
            "f", "foreign key (child_task_id, document_id) references task(id, document_id)"
        ),
        "fk_ocr_retry_source_receipt": (
            "f", "foreign key (source_intake_item_id, source_operation_id, document_id) references intake_item(id, operation_id, document_id)"
        ),
        "fk_ocr_retry_actor": (
            "f", 'foreign key (requested_by_id) references "user"(id)'
        ),
    }
    if set(ocr_retry_guards) != set(expected_ocr_retry_guards):
        differences.append("OCR retry lineage constraints missing or ambiguous")
    else:
        for name, (expected_type, expected_definition) in expected_ocr_retry_guards.items():
            actual_type, validated, definition = ocr_retry_guards[name]
            normalized = _normalize_case_package_constraint(definition)
            if (
                actual_type != expected_type
                or not validated
                or normalized != _normalize_case_package_constraint(expected_definition)
            ):
                differences.append(f"OCR retry lineage constraint differs: {name}")
    package_guards = connection.execute(text(
        "SELECT t.relname, c.conname, c.contype, c.convalidated, pg_get_constraintdef(c.oid) "
        "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname='public' AND t.relname IN "
        "('case_package', 'case_package_task_event')"
    )).all()
    package_guards = {(row[0], row[1]): row[2:] for row in package_guards}
    expected_package_guards = {
        ("case_package", "case_package_pkey"): "p",
        ("case_package", "uq_case_package_request_key_sha256"): "u",
        ("case_package", "case_package_created_by_id_fkey"): "f",
        ("case_package", "ck_case_package_title_length"): "c",
        ("case_package", "ck_case_package_case_number_length"): "c",
        ("case_package", "ck_case_package_request_key_sha256"): "c",
        ("case_package", "ck_case_package_payload_sha256"): "c",
        ("case_package_task_event", "case_package_task_event_pkey"): "p",
        ("case_package_task_event", "case_package_task_event_case_package_id_fkey"): "f",
        ("case_package_task_event", "case_package_task_event_task_id_fkey"): "f",
        ("case_package_task_event", "case_package_task_event_actor_id_fkey"): "f",
        ("case_package_task_event", "ck_case_package_task_event_action"): "c",
    }
    if set(package_guards) != set(expected_package_guards):
        differences.append("case package identity or membership constraints missing or ambiguous")
    else:
        for key, expected_type in expected_package_guards.items():
            actual_type, validated, definition = package_guards[key]
            if actual_type != expected_type or not validated:
                differences.append(f"case package constraint invalid: {key[1]}")
                continue
            normalized = _normalize_case_package_constraint(definition)
            expected_definition = {
                "case_package_pkey": "primary key id",
                "uq_case_package_request_key_sha256": "unique request_key_sha256",
                "case_package_created_by_id_fkey": "foreign key created_by_id references userid",
                "ck_case_package_title_length": "check lengthtitle >= 1 and lengthtitle <= 255",
                "ck_case_package_case_number_length": "check case_number = '' or lengthcase_number <= 64",
                "ck_case_package_request_key_sha256": "check request_key_sha256 ~ '^[0-9a-f]{64}$'",
                "ck_case_package_payload_sha256": "check payload_sha256 ~ '^[0-9a-f]{64}$'",
                "case_package_task_event_pkey": "primary key id",
                "case_package_task_event_case_package_id_fkey": "foreign key case_package_id references case_packageid",
                "case_package_task_event_task_id_fkey": "foreign key task_id references taskid",
                "case_package_task_event_actor_id_fkey": "foreign key actor_id references userid",
                "ck_case_package_task_event_action": "check action = any array['added', 'removed']",
            }[key[1]]
            if normalized != _normalize_case_package_constraint(expected_definition):
                differences.append(f"case package constraint definition differs: {key[1]}")
    card_lifecycle = connection.execute(text(
        "SELECT c.contype, c.convalidated, pg_get_constraintdef(c.oid) "
        "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname='public' AND t.relname='debtor_card_draft' "
        "AND c.conname='ck_debtor_card_draft_preparation_state'"
    )).first()
    source_task_nullable = connection.execute(text(
        "SELECT NOT a.attnotnull FROM pg_attribute a "
        "JOIN pg_class t ON t.oid=a.attrelid JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname='public' AND t.relname='debtor_card_draft' "
        "AND a.attname='source_task_id' AND NOT a.attisdropped"
    )).scalar()
    if (card_lifecycle is None or card_lifecycle[0] != "c" or not card_lifecycle[1]
            or "'draft'" not in card_lifecycle[2] or "'prepared'" not in card_lifecycle[2]
            or "'reviewed'" not in card_lifecycle[2]):
        differences.append("debtor-card draft lifecycle constraint missing or invalid")
    entity_constraints = connection.execute(text(
        "SELECT c.conname, c.contype, c.convalidated, pg_get_constraintdef(c.oid) "
        "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname='public' AND t.relname='debtor_card_draft' "
        "AND c.conname IN ('ck_debtor_card_draft_entity_kind', 'ck_debtor_card_draft_inn_length')"
    )).all()
    entity_constraints = {row[0]: row[1:] for row in entity_constraints}
    kind_guard = entity_constraints.get("ck_debtor_card_draft_entity_kind")
    inn_guard = entity_constraints.get("ck_debtor_card_draft_inn_length")
    if (kind_guard is None or kind_guard[0] != "c" or not kind_guard[1]
            or not all(kind in kind_guard[2] for kind in ("person", "legal_entity", "sole_proprietor"))):
        differences.append("debtor-card entity-kind constraint missing or invalid")
    if (inn_guard is None or inn_guard[0] != "c" or not inn_guard[1]
            or "'legal_entity'::text" not in inn_guard[2]
            or "length((inn)::text) = 10" not in inn_guard[2]
            or "'sole_proprietor'::text" not in inn_guard[2]
            or "length((inn)::text) = 12" not in inn_guard[2]):
        differences.append("debtor-card INN-length constraint missing or invalid")
    if source_task_nullable is not True:
        differences.append("debtor-card source task must be nullable for manual drafts")
    return differences
