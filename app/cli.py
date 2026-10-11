"""Служебные команды платформы.

    .venv/bin/python -m app.cli init-db
    .venv/bin/python -m app.cli create-user ivan@example.ru
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import getpass
import os
import signal
import stat
import sys
import threading

from alembic import command as alembic_command
from alembic.autogenerate.api import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from redis import Redis
from rq import Queue, Worker
from rq.job import Job
from rq.registry import (
    DeferredJobRegistry,
    FailedJobRegistry,
    ScheduledJobRegistry,
    StartedJobRegistry,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy import Integer, func, inspect, select, text
from sqlalchemy.engine import Connection

from sro_robot.config import ROOT

from .db.base import Base, engine, migration_engine, session_scope
from .db import models as _models  # Register the application's tables in Base.metadata.
from .db.models import Document, JobOutbox, Task, TaskLease, TaskStatus
from .db.schema import EXPECTED_SCHEMA_REVISION
from .services.leases import OCR_JOB_TIMEOUT_SECONDS, reconcilable_ocr_task_ids
from .services import auth, outbox
from .settings import settings
from .worker_health import DEFAULT_WORKER_TTL, has_fresh_worker_heartbeat


def has_unversioned_tables(connection: Connection) -> bool:
    """Detect a populated schema that Alembic does not yet own."""
    revision = MigrationContext.configure(connection).get_current_revision()
    tables = set(inspect(connection).get_table_names())
    return revision is None and bool(tables - {"alembic_version"})


def cmd_init_db(args) -> int:
    if args.reset:
        print("Отказ: init-db больше не удаляет таблицы; управляйте схемой через Alembic")
        return 2
    try:
        with migration_engine.connect() as connection:
            if has_unversioned_tables(connection):
                print(
                    "Отказ: найдены существующие таблицы без записанной ревизии Alembic. "
                    "Перед применением миграций нужны проверенная резервная копия и "
                    "точное сравнение схемы; автоматическое проставление baseline запрещено."
                )
                return 2
    except Exception as exc:
        print(f"Отказ: не удалось безопасно проверить состояние схемы: {type(exc).__name__}")
        return 2
    try:
        alembic_command.upgrade(AlembicConfig(str(ROOT / "alembic.ini")), "head")
    except Exception as exc:
        print(f"Отказ: миграции не выполнены ({type(exc).__name__})")
        return 2
    print("Схема базы данных обновлена до текущей ревизии")
    return 0


def cmd_schema_check(args) -> int:
    """Compare the live schema to ORM metadata without changing the database."""
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": True},
            )
            revision = context.get_current_revision()
            differences = compare_metadata(context, Base.metadata)
            tables = sorted(inspect(connection).get_table_names())
            enum_differences = compare_postgresql_enums(connection)
            object_differences = compare_postgresql_objects(connection)
            index_differences = compare_postgresql_index_uniqueness(connection)
    except Exception as exc:
        print(f"Ошибка read-only проверки схемы: {type(exc).__name__}")
        return 2

    print(f"Alembic revision: {revision or 'unversioned'}")
    print(f"Tables: {', '.join(tables) if tables else '(none)'}")
    if differences or enum_differences or object_differences or index_differences:
        total_differences = (
            len(differences)
            + len(enum_differences)
            + len(object_differences)
            + len(index_differences)
        )
        print(f"Schema differs from ORM metadata ({total_differences} difference(s)):")
        for difference in differences:
            print(f"- {difference!r}")
        for difference in enum_differences:
            print(f"- PostgreSQL enum mismatch: {difference}")
        for difference in object_differences:
            print(f"- PostgreSQL object mismatch: {difference}")
        for difference in index_differences:
            print(f"- PostgreSQL index mismatch: {difference}")
        return 1

    print("Schema matches ORM metadata; no migration or stamp was applied")
    return 0


def cmd_db_role_check(args) -> int:
    """Report effective PostgreSQL role capabilities without changing data."""
    try:
        with engine.connect() as connection:
            if connection.dialect.name != "postgresql":
                print("Read-only role capability check requires PostgreSQL")
                return 2
            capabilities = connection.execute(text("""
                SELECT
                    role.rolsuper AS superuser,
                    role.rolcreaterole AS create_role,
                    role.rolcreatedb AS create_database,
                    role.rolreplication AS replication,
                    role.rolbypassrls AS bypass_row_security,
                    (SELECT count(*) FILTER (
                         WHERE pg_has_role(current_user, candidate.oid, 'USAGE'))
                     FROM pg_roles AS candidate
                     WHERE candidate.rolname <> current_user)
                        AS inherited_role_count,
                    (SELECT count(*) FILTER (
                         WHERE pg_has_role(current_user, candidate.oid, 'MEMBER'))
                     FROM pg_roles AS candidate
                     WHERE candidate.rolname <> current_user)
                        AS settable_role_count,
                    (SELECT count(*) FILTER (
                         WHERE pg_has_role(current_user, candidate.oid, 'MEMBER')
                           AND (candidate.rolsuper OR candidate.rolcreaterole
                                OR candidate.rolcreatedb OR candidate.rolreplication
                                OR candidate.rolbypassrls))
                     FROM pg_roles AS candidate
                     WHERE candidate.rolname <> current_user)
                        AS settable_elevated_role_count,
                    has_database_privilege(current_user, current_database(), 'CREATE')
                        AS database_create,
                    has_schema_privilege(current_user, 'public', 'CREATE')
                        AS public_schema_create,
                    has_schema_privilege(current_user, 'public', 'USAGE')
                        AS public_schema_usage,
                    current_setting('transaction_read_only')::boolean AS transaction_read_only,
                    (SELECT count(*) FROM pg_class AS relation
                     JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public' AND relation.relkind IN ('r', 'p'))
                        AS public_table_count,
                    (SELECT count(*) FILTER (WHERE has_table_privilege(
                         current_user, relation.oid, 'SELECT'))
                     FROM pg_class AS relation
                     JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public' AND relation.relkind IN ('r', 'p'))
                        AS public_table_select,
                    (SELECT count(*) FILTER (WHERE has_table_privilege(
                         current_user, relation.oid, 'INSERT'))
                     FROM pg_class AS relation
                     JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public' AND relation.relkind IN ('r', 'p'))
                        AS public_table_insert,
                    (SELECT count(*) FILTER (WHERE has_table_privilege(
                         current_user, relation.oid, 'UPDATE'))
                     FROM pg_class AS relation
                     JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public' AND relation.relkind IN ('r', 'p'))
                        AS public_table_update,
                    (SELECT count(*) FILTER (WHERE has_table_privilege(
                         current_user, relation.oid, 'DELETE'))
                     FROM pg_class AS relation
                     JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public' AND relation.relkind IN ('r', 'p'))
                        AS public_table_delete,
                    (SELECT count(*) FILTER (WHERE relation.relrowsecurity)
                     FROM pg_class AS relation
                     JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public' AND relation.relkind IN ('r', 'p'))
                        AS public_table_rls,
                    (SELECT count(*) FILTER (WHERE relation.relforcerowsecurity)
                     FROM pg_class AS relation
                     JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public' AND relation.relkind IN ('r', 'p'))
                        AS public_table_force_rls,
                    (SELECT count(*) FROM pg_class AS relation
                     JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public' AND relation.relkind = 'S')
                        AS public_sequence_count,
                    (SELECT count(*) FILTER (WHERE has_sequence_privilege(
                         current_user, relation.oid, 'USAGE'))
                     FROM pg_class AS relation
                     JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public' AND relation.relkind = 'S')
                        AS public_sequence_usage,
                    (SELECT count(*) FILTER (WHERE has_sequence_privilege(
                         current_user, relation.oid, 'SELECT'))
                     FROM pg_class AS relation
                     JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public' AND relation.relkind = 'S')
                        AS public_sequence_select,
                    (SELECT count(*) FILTER (WHERE has_sequence_privilege(
                         current_user, relation.oid, 'UPDATE'))
                     FROM pg_class AS relation
                     JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                     WHERE namespace.nspname = 'public' AND relation.relkind = 'S')
                        AS public_sequence_update,
                    (SELECT count(*)
                     FROM pg_proc AS routine
                     JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
                     WHERE namespace.nspname = 'public'
                       AND routine.prosecdef
                       AND has_function_privilege(current_user, routine.oid, 'EXECUTE'))
                        AS public_security_definer_exec_count
                FROM pg_roles AS role
                WHERE role.rolname = current_user
            """)).mappings().one()
    except Exception as exc:
        print(f"Read-only database role check failed: {type(exc).__name__}")
        return 2

    print("Database session role capabilities (role and database names omitted):")
    table_count = int(capabilities["public_table_count"])
    sequence_count = int(capabilities["public_sequence_count"])
    role_count_fields = {
        "inherited_role_count",
        "settable_role_count",
        "settable_elevated_role_count",
        "public_security_definer_exec_count",
    }
    for capability, enabled in capabilities.items():
        if capability in ("public_table_count", "public_sequence_count"):
            print(f"- {capability}: {int(enabled)}")
        elif capability in role_count_fields:
            print(f"- {capability}: {int(enabled)}")
        elif capability.startswith("public_table_"):
            print(f"- {capability}: {int(enabled)}/{table_count}")
        elif capability.startswith("public_sequence_"):
            print(f"- {capability}: {int(enabled)}/{sequence_count}")
        else:
            print(f"- {capability}: {bool(enabled)}")
    print("Informational snapshot only; no least-privilege policy has been approved")
    return 0


def compare_postgresql_enums(connection) -> list[str]:
    """Compare named native enum labels and order without mutating the database."""
    if connection.dialect.name != "postgresql":
        return []

    expected = expected_postgresql_enums()
    actual = {
        enum_info["name"]: list(enum_info["labels"])
        for enum_info in inspect(connection).get_enums(schema="public")
    }
    mismatches = []
    for name in sorted(expected.keys() | actual.keys()):
        expected_labels = expected.get(name)
        actual_labels = actual.get(name)
        if expected_labels != actual_labels:
            mismatches.append(
                f"{name}: expected={expected_labels!r}, actual={actual_labels!r}"
            )
    return mismatches


def expected_postgresql_enums() -> dict[str, list[str]]:
    """Collect every named native enum referenced by application metadata."""
    expected: dict[str, list[str]] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            column_type = column.type
            if isinstance(column_type, SqlEnum) and column_type.native_enum and column_type.name:
                expected[column_type.name] = list(column_type.enums)
    return expected


def compare_postgresql_index_uniqueness(connection) -> list[str]:
    """Detect unique/non-unique index drift that Alembic autogenerate omits."""
    if connection.dialect.name != "postgresql":
        return []
    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names(schema="public"))
    differences = []
    for table in Base.metadata.sorted_tables:
        if table.schema not in (None, "public") or table.name not in actual_tables:
            continue
        actual_indexes = {
            index.get("name"): bool(index.get("unique"))
            for index in inspector.get_indexes(table.name, schema=table.schema or "public")
        }
        for expected in table.indexes:
            actual_unique = actual_indexes.get(expected.name)
            if actual_unique is not None and actual_unique != bool(expected.unique):
                differences.append(
                    f"{table.name}.{expected.name} uniqueness expected="
                    f"{bool(expected.unique)} actual={actual_unique}"
                )
    return differences


def compare_postgresql_objects(connection) -> list[str]:
    """Check public relations, sequences, triggers and row-security objects."""
    if connection.dialect.name != "postgresql":
        return []

    expected_sequences = expected_postgresql_sequences()

    inspector = inspect(connection)
    actual_sequences = set(inspector.get_sequence_names(schema="public"))
    actual_views = set(inspector.get_view_names(schema="public"))
    get_materialized_views = getattr(inspector, "get_materialized_view_names", None)
    actual_materialized_views = (
        set(get_materialized_views(schema="public")) if get_materialized_views else set()
    )
    get_foreign_tables = getattr(inspector, "get_foreign_table_names", None)
    actual_foreign_tables = set(get_foreign_tables(schema="public")) if get_foreign_tables else set()
    unmodeled_objects = connection.execute(
        text(
            "SELECT 'trigger ' || c.relname || '.' || t.tgname AS object_name "
            "FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND NOT t.tgisinternal "
            "UNION ALL "
            "SELECT 'row-level security enabled on table ' || c.relname "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relrowsecurity "
            "UNION ALL "
            "SELECT 'row-level security forced on table ' || c.relname "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relforcerowsecurity "
            "UNION ALL "
            "SELECT 'row-level security policy ' || c.relname || '.' || p.polname "
            "FROM pg_policy p "
            "JOIN pg_class c ON c.oid = p.polrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' "
            "UNION ALL "
            "SELECT 'extension public.' || e.extname "
            "FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE n.nspname = 'public' "
            "UNION ALL "
            "SELECT 'routine public.' || p.proname || '(' "
            "|| pg_get_function_identity_arguments(p.oid) || ')' "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p', 'w', 'a') "
            "ORDER BY object_name"
        )
    ).scalars().all()

    # This is the only application-managed public trigger and routine. Their
    # definitions are checked explicitly below rather than treated as drift.
    managed_audit_objects = {
        "trigger event.event_append_only",
        "trigger event.event_no_truncate",
        "trigger event.event_contract_validate",
        "routine public.sro_reject_event_mutation()",
        "routine public.sro_validate_event_insert()",
        "trigger task.task_status_transition_guard",
        "routine public.sro_enforce_task_status_transition()",
        "trigger task.task_status_history_record_change",
        "trigger task_status_history.task_status_history_append_only",
        "trigger task_status_history.task_status_history_no_truncate",
        "routine public.sro_record_task_status_change()",
        "routine public.sro_reject_task_status_history_mutation()",
    }
    unmodeled_objects = [
        name for name in unmodeled_objects if name not in managed_audit_objects
    ]

    differences = []
    for sequence in sorted(expected_sequences - actual_sequences):
        differences.append(f"missing sequence {sequence}")
    for sequence in sorted(actual_sequences - expected_sequences):
        differences.append(f"unexpected sequence {sequence}")
    differences.extend(f"unexpected view {name}" for name in sorted(actual_views))
    differences.extend(
        f"unexpected materialized view {name}" for name in sorted(actual_materialized_views)
    )
    differences.extend(f"unexpected foreign table {name}" for name in sorted(actual_foreign_tables))
    differences.extend(f"unexpected {name}" for name in unmodeled_objects)
    differences.extend(_audit_event_guard_differences(connection))
    differences.extend(_audit_event_contract_differences(connection))
    differences.extend(_task_status_guard_differences(connection))
    differences.extend(_task_status_history_guard_differences(connection))
    differences.extend(_runtime_trigger_security_differences(connection))
    return differences


def _runtime_trigger_security_differences(connection) -> list[str]:
    """Require managed trigger functions to run with invoker privileges."""
    expected_functions = {
        "sro_reject_event_mutation",
        "sro_validate_event_insert",
        "sro_enforce_task_status_transition",
        "sro_record_task_status_change",
        "sro_reject_task_status_history_mutation",
    }
    rows = connection.execute(
        text(
            "SELECT p.proname, p.prosecdef "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.pronargs = 0 "
            "AND p.prorettype = 'trigger'::regtype AND p.proname IN ("
            "'sro_reject_event_mutation', 'sro_validate_event_insert', "
            "'sro_enforce_task_status_transition', 'sro_record_task_status_change', "
            "'sro_reject_task_status_history_mutation')"
        )
    ).all()
    observed = {name: security_definer for name, security_definer in rows}
    differences = []
    if len(rows) != len(expected_functions) or set(observed) != expected_functions:
        differences.append("runtime trigger function set missing or ambiguous")
    for name, security_definer in observed.items():
        if security_definer:
            differences.append(f"runtime trigger function unexpectedly SECURITY DEFINER: {name}")
    return differences


def _audit_event_guard_differences(connection) -> list[str]:
    """Verify the managed append-only trigger and its fail-closed function."""
    function_rows = connection.execute(
        text(
            "SELECT p.prosrc, language.lanname, p.proconfig "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language language ON language.oid = p.prolang "
            "WHERE n.nspname = 'public' AND p.proname = 'sro_reject_event_mutation' "
            "AND p.pronargs = 0 AND p.prorettype = 'trigger'::regtype"
        )
    ).all()
    expected_source = (
        "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', "
        "MESSAGE = 'event rows are append-only'; END;"
    )
    differences = []
    if len(function_rows) != 1:
        differences.append("append-only event trigger function missing or ambiguous")
    else:
        source, language, config = function_rows[0]
        if " ".join(source.split()) != expected_source or language != "plpgsql" or not config or (
            "search_path=pg_catalog" not in config
        ):
            differences.append("append-only event trigger function definition differs")

    trigger_rows = connection.execute(
        text(
            "SELECT t.tgname, t.tgtype, t.tgenabled, t.tgisinternal, "
            "p.proname, function_ns.nspname "
            "FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "JOIN pg_namespace function_ns ON function_ns.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'event' "
            "AND t.tgname IN ('event_append_only', 'event_no_truncate')"
        )
    ).all()
    expected_triggers = {
        "event_append_only": 27,  # ROW | BEFORE | DELETE | UPDATE
        "event_no_truncate": 34,  # BEFORE | TRUNCATE (statement-level)
    }
    triggers = {row[0]: row[1:] for row in trigger_rows}
    if len(trigger_rows) != 2 or set(triggers) != set(expected_triggers):
        differences.append("append-only event trigger set missing or ambiguous")
    else:
        for name, expected_type in expected_triggers.items():
            trigger_type, enabled, internal, function_name, function_schema = triggers[name]
            if (
                trigger_type != expected_type
                or enabled != "O"
                or internal
                or function_name != "sro_reject_event_mutation"
                or function_schema != "public"
            ):
                differences.append(f"append-only event trigger shape differs: {name}")
    return differences


def _audit_event_contract_differences(connection) -> list[str]:
    """Verify the fixed-message/payload validator and its insert trigger."""
    function_rows = connection.execute(
        text(
            "SELECT p.prosrc, language.lanname, p.proconfig "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language language ON language.oid = p.prolang "
            "WHERE n.nspname = 'public' AND p.proname = 'sro_validate_event_insert' "
            "AND p.pronargs = 0 AND p.prorettype = 'trigger'::regtype"
        )
    ).all()
    expected_source_sha256 = (
        "ae9f2c6625b8e314be7c2af5b4897f80de893308e50a23906aed5503d01132fa"
    )
    differences = []
    if len(function_rows) != 1:
        differences.append("audit event contract trigger function missing or ambiguous")
    else:
        source, language, config = function_rows[0]
        actual_source_sha256 = hashlib.sha256(" ".join(source.split()).encode("utf-8")).hexdigest()
        if (
            actual_source_sha256 != expected_source_sha256
            or language != "plpgsql"
            or not config
            or "search_path=pg_catalog" not in config
        ):
            differences.append("audit event contract trigger function definition differs")

    trigger_rows = connection.execute(
        text(
            "SELECT t.tgtype, t.tgenabled, t.tgisinternal, p.proname, function_ns.nspname, "
            "pg_get_triggerdef(t.oid) "
            "FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "JOIN pg_namespace function_ns ON function_ns.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'event' "
            "AND t.tgname = 'event_contract_validate'"
        )
    ).all()
    if len(trigger_rows) != 1:
        differences.append("audit event contract trigger missing or ambiguous")
    else:
        trigger_type, enabled, internal, function_name, function_schema, definition = trigger_rows[0]
        normalized_definition = " ".join(definition.lower().split())
        if (
            trigger_type != 7  # ROW | BEFORE | INSERT
            or enabled != "O"
            or internal
            or function_name != "sro_validate_event_insert"
            or function_schema != "public"
            or "before insert on public.event" not in normalized_definition
        ):
            differences.append("audit event contract trigger shape differs")
    return differences


def _task_status_guard_differences(connection) -> list[str]:
    """Verify the database lifecycle guard is present and matches the reviewed graph."""
    function_rows = connection.execute(
        text(
            "SELECT p.prosrc, language.lanname, p.proconfig "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language language ON language.oid = p.prolang "
            "WHERE n.nspname = 'public' AND p.proname = 'sro_enforce_task_status_transition' "
            "AND p.pronargs = 0 AND p.prorettype = 'trigger'::regtype"
        )
    ).all()
    expected_fragments = (
        "IF NEW.status IS NOT DISTINCT FROM OLD.status THEN RETURN NEW; END IF;",
        "WHEN 'new' THEN NEW.status::text = ANY (ARRAY['recognizing', 'failed', 'rejected'])",
        "WHEN 'recognizing' THEN NEW.status::text = ANY (ARRAY['needs_review', 'failed', 'duplicate'])",
        "WHEN 'needs_review' THEN NEW.status::text = ANY (ARRAY['approved', 'rejected'])",
        "WHEN 'approved' THEN NEW.status::text = ANY (ARRAY['needs_review', 'rejected', 'writing', 'done', 'failed'])",
        "WHEN 'writing' THEN NEW.status::text = ANY (ARRAY['done', 'awaiting_preview'])",
        "ERRCODE = '23514'",
        "MESSAGE = 'task status transition is not allowed'",
    )
    differences = []
    if len(function_rows) != 1:
        differences.append("task status trigger function missing or ambiguous")
    else:
        source, language, config = function_rows[0]
        normalized_source = " ".join(source.split())
        if (
            language != "plpgsql"
            or not config
            or "search_path=pg_catalog" not in config
            or any(fragment not in normalized_source for fragment in expected_fragments)
        ):
            differences.append("task status trigger function definition differs")

    trigger_rows = connection.execute(
        text(
            "SELECT t.tgname, t.tgtype, t.tgenabled, t.tgisinternal, "
            "p.proname, function_ns.nspname, pg_get_triggerdef(t.oid) "
            "FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "JOIN pg_namespace function_ns ON function_ns.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'task' "
            "AND t.tgname = 'task_status_transition_guard'"
        )
    ).all()
    if len(trigger_rows) != 1:
        differences.append("task status trigger missing or ambiguous")
    else:
        name, trigger_type, enabled, internal, function_name, function_schema, definition = trigger_rows[0]
        normalized_definition = " ".join(definition.lower().split())
        if (
            name != "task_status_transition_guard"
            or trigger_type != 19  # ROW | BEFORE | UPDATE
            or enabled != "O"
            or internal
            or function_name != "sro_enforce_task_status_transition"
            or function_schema != "public"
            or "update of status on public.task" not in normalized_definition
        ):
            differences.append("task status trigger shape differs")
    return differences


def _task_status_history_guard_differences(connection) -> list[str]:
    """Verify automatic status-history capture and append-only protection."""
    function_rows = connection.execute(
        text(
            "SELECT p.proname, p.prosrc, language.lanname, p.proconfig "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language language ON language.oid = p.prolang "
            "WHERE n.nspname = 'public' AND p.pronargs = 0 "
            "AND p.prorettype = 'trigger'::regtype "
            "AND p.proname IN ("
            "'sro_record_task_status_change', "
            "'sro_reject_task_status_history_mutation')"
        )
    ).all()
    expected_functions = {
        "sro_record_task_status_change": (
            "BEGIN INSERT INTO public.task_status_history "
            "(task_id, from_status, to_status) VALUES "
            "(NEW.id, OLD.status::text, NEW.status::text); RETURN NEW; END;"
        ),
        "sro_reject_task_status_history_mutation": (
            "BEGIN RAISE EXCEPTION USING ERRCODE = '55000', "
            "MESSAGE = 'task status history rows are append-only'; END;"
        ),
    }
    differences = []
    functions = {row[0]: row[1:] for row in function_rows}
    if len(function_rows) != 2 or set(functions) != set(expected_functions):
        differences.append("task status history trigger functions missing or ambiguous")
    else:
        for name, expected_source in expected_functions.items():
            source, language, config = functions[name]
            if (
                expected_source != " ".join(source.split())
                or language != "plpgsql"
                or not config
                or "search_path=pg_catalog" not in config
            ):
                differences.append(f"task status history function definition differs: {name}")

    trigger_rows = connection.execute(
        text(
            "SELECT c.relname, t.tgname, t.tgtype, t.tgenabled, t.tgisinternal, "
            "p.proname, function_ns.nspname, pg_get_triggerdef(t.oid) "
            "FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "JOIN pg_namespace function_ns ON function_ns.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND ("
            "(c.relname = 'task' AND t.tgname = 'task_status_history_record_change') OR "
            "(c.relname = 'task_status_history' AND t.tgname IN ("
            "'task_status_history_append_only', 'task_status_history_no_truncate')))"
        )
    ).all()
    expected_triggers = {
        ("task", "task_status_history_record_change"): (
            17,
            "sro_record_task_status_change",
            "after update of status on public.task",
            "old.status is distinct from new.status",
        ),
        ("task_status_history", "task_status_history_append_only"): (
            27,
            "sro_reject_task_status_history_mutation",
            None,
            None,
        ),
        ("task_status_history", "task_status_history_no_truncate"): (
            34,
            "sro_reject_task_status_history_mutation",
            None,
            None,
        ),
    }
    triggers = {(row[0], row[1]): row[2:] for row in trigger_rows}
    if len(trigger_rows) != 3 or set(triggers) != set(expected_triggers):
        differences.append("task status history trigger set missing or ambiguous")
    else:
        for key, (expected_type, expected_function, definition_fragment, when_fragment) in (
            expected_triggers.items()
        ):
            trigger_type, enabled, internal, function_name, function_schema, definition = triggers[key]
            normalized_definition = " ".join(definition.lower().split())
            if (
                trigger_type != expected_type
                or enabled != "O"
                or internal
                or function_name != expected_function
                or function_schema != "public"
                or (
                    definition_fragment is not None
                    and definition_fragment not in normalized_definition
                )
                or (when_fragment is not None and when_fragment not in normalized_definition)
            ):
                differences.append(f"task status history trigger shape differs: {key[1]}")
    return differences


def expected_postgresql_sequences() -> set[str]:
    """Derive default public serial sequence names for integer primary keys."""
    return {
        f"{table.name}_{column.name}_seq"
        for table in Base.metadata.tables.values()
        for column in table.primary_key.columns
        if column.autoincrement in (True, "auto")
        and not column.foreign_keys
        and isinstance(column.type, Integer)
    }


def cmd_ops_status(args) -> int:
    """Report queue and task health using read-only database/Redis operations."""
    client = None
    try:
        with engine.connect() as connection:
            counts = connection.execute(
                select(Task.status, func.count()).group_by(Task.status)
            ).all()
            source_counts = connection.execute(
                select(Document.source, Task.status, func.count())
                .join(Task, Task.document_id == Document.id)
                .group_by(Document.source, Task.status)
            ).all()
            oldest_recognizing = connection.execute(
                select(func.min(Task.updated_at)).where(Task.status == TaskStatus.recognizing)
            ).scalar_one()
            oldest_new = connection.execute(
                select(func.min(Task.created_at)).where(Task.status == TaskStatus.new)
            ).scalar_one()
            unleased_recognizing = None
            if inspect(connection).has_table(TaskLease.__tablename__):
                unleased_recognizing = connection.execute(
                    select(func.count())
                    .select_from(Task)
                    .outerjoin(TaskLease, TaskLease.task_id == Task.id)
                    .where(
                        Task.status == TaskStatus.recognizing,
                        TaskLease.task_id.is_(None),
                    )
                ).scalar_one()
            outbox_summary = None
            if inspect(connection).has_table(JobOutbox.__tablename__):
                outbox_summary = outbox.status_summary(connection)
    except Exception as exc:
        print(f"Проверка задач недоступна: {type(exc).__name__}")
        return 2

    try:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        ocr_queue = Queue("ocr", connection=client)
        sro_queue = Queue("sro", connection=client)
        ocr_count = ocr_queue.count
        sro_count = sro_queue.count
        ocr_head_enqueued = _queue_head_job_enqueued_at(ocr_queue, client, ocr_count)
        sro_head_enqueued = _queue_head_job_enqueued_at(sro_queue, client, sro_count)
        ocr_workers = Worker.all(connection=client, queue=ocr_queue)
        sro_workers = Worker.all(connection=client, queue=sro_queue)
        ocr_registry = _queue_registry_counts("ocr", client)
        sro_registry = _queue_registry_counts("sro", client)
    except Exception as exc:
        print(f"Проверка очередей недоступна: {type(exc).__name__}")
        return 2
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                print(f"Закрытие соединения Redis не удалось: {type(exc).__name__}")

    print("Task counts:")
    for status, count in sorted(counts, key=lambda row: str(getattr(row[0], "value", row[0]))):
        name = getattr(status, "value", status)
        print(f"- {name}: {count}")
    print("Task counts by intake source and status:")
    for source, status, count in sorted(
        source_counts,
        key=lambda row: (
            str(getattr(row[0], "value", row[0])),
            str(getattr(row[1], "value", row[1])),
        ),
    ):
        source_name = getattr(source, "value", source)
        status_name = getattr(status, "value", status)
        print(f"- {source_name}/{status_name}: {count}")
    print(f"Oldest new task created_at: {oldest_new or 'none'}")
    print(f"Oldest recognizing updated_at: {oldest_recognizing or 'none'}")
    if unleased_recognizing is None:
        print("Recognizing tasks without a lease: unavailable (task_lease table absent)")
    else:
        print(
            "Recognizing tasks without a lease (require reconciliation): "
            f"{unleased_recognizing}"
        )
    if outbox_summary is None:
        print("Pending safe job dispatches: unavailable (job_outbox table absent)")
    else:
        print(_format_outbox_summary(outbox_summary))
    print(f"Queue depth: ocr={ocr_count} sro={sro_count}")
    print(
        "Queue-head RQ job enqueued_at: "
        f"ocr={ocr_head_enqueued} sro={sro_head_enqueued}"
    )
    checked_at = datetime.now(timezone.utc)
    print(
        "Queue-head age: "
        f"ocr={_queue_head_job_age_seconds(ocr_head_enqueued, checked_at)} "
        f"sro={_queue_head_job_age_seconds(sro_head_enqueued, checked_at)}"
    )
    print(
        "RQ registry entries (cleanup disabled): "
        f"ocr={_format_registry_counts(ocr_registry)} "
        f"sro={_format_registry_counts(sro_registry)}"
    )
    print(f"Registered RQ workers: ocr={len(ocr_workers)} sro={len(sro_workers)}")
    print(
        "RQ worker heartbeat (oldest age seconds / missing): "
        f"ocr={_worker_heartbeat_summary(ocr_workers, checked_at)} "
        f"sro={_worker_heartbeat_summary(sro_workers, checked_at)}"
    )
    print(f"RQ worker heartbeat freshness limit: {DEFAULT_WORKER_TTL} seconds")
    print("Read-only check; no task was changed or re-enqueued")
    stalled_queues = [
        name
        for name, queued, workers in (
            ("ocr", ocr_count, len(ocr_workers)),
            ("sro", sro_count, len(sro_workers)),
        )
        if queued > 0 and workers == 0
    ]
    if stalled_queues:
        print(f"Queue backlog has no registered worker: {', '.join(stalled_queues)}")
        return 1
    unhealthy_worker_queues = [
        name
        for name, queued, workers in (
            ("ocr", ocr_count, ocr_workers),
            ("sro", sro_count, sro_workers),
        )
        if queued > 0 and workers and not any(
            has_fresh_worker_heartbeat(worker, checked_at) for worker in workers
        )
    ]
    if unhealthy_worker_queues:
        print(
            "Queue backlog has no worker with a fresh heartbeat: "
            f"{', '.join(unhealthy_worker_queues)}"
        )
        return 1
    started_without_worker = [
        name
        for name, registry, workers in (
            ("ocr", ocr_registry, ocr_workers),
            ("sro", sro_registry, sro_workers),
        )
        if registry["started"] > 0
        and not any(has_fresh_worker_heartbeat(worker, checked_at) for worker in workers)
    ]
    if started_without_worker:
        print(
            "Started RQ entries exist without a fresh worker heartbeat for the queue: "
            f"{', '.join(started_without_worker)}"
        )
        return 1
    if unleased_recognizing:
        print("Unleased recognizing tasks require read-only reconciliation before processing")
        return 1
    if outbox_summary is not None and outbox_summary.pending:
        if outbox_summary.eligible or outbox_summary.leased or outbox_summary.delayed:
            print("Dispatchable job intents remain pending; run outbox-dispatch after review")
        if outbox_summary.withheld:
            print(
                "Policy-withheld job intents remain pending; satisfy the capability release gate "
                "before dispatch"
            )
        if not (
            outbox_summary.eligible
            or outbox_summary.leased
            or outbox_summary.delayed
            or outbox_summary.withheld
        ):
            print("Pending job intents require operator investigation")
        return 1
    return 0


class OutboxSchemaUnavailable(RuntimeError):
    """The current database does not have the durable outbox schema."""


def _outbox_dispatch_cycle(*, apply: bool, limit: int) -> tuple[int, outbox.OutboxSummary]:
    with engine.connect() as connection:
        revision = MigrationContext.configure(connection).get_current_revision()
    if revision != EXPECTED_SCHEMA_REVISION:
        raise OutboxSchemaUnavailable("application schema revision is not current")
    with session_scope() as db:
        confirmed = outbox.dispatch_ready(db, limit=limit)[0] if apply else 0
        return confirmed, outbox.status_summary(db)


def run_outbox_dispatch_watch(
    dispatch_cycle,
    *,
    stop_event,
    poll_seconds: float,
) -> int:
    """Continuously dispatch bounded safe batches until asked to stop."""
    if not 1 <= poll_seconds <= 60:
        raise ValueError("poll_seconds must be between 1 and 60")
    retry_seconds = 1.0
    while not stop_event.is_set():
        try:
            confirmed, summary = dispatch_cycle()
        except OutboxSchemaUnavailable:
            print("Outbox unavailable: current job_outbox schema is not installed")
            return 2
        except Exception as exc:
            print(f"Outbox dispatcher cycle failed: {type(exc).__name__}", flush=True)
            if stop_event.wait(retry_seconds):
                break
            retry_seconds = min(retry_seconds * 2, 30.0)
            continue

        retry_seconds = 1.0
        print(
            f"outbox_dispatch_cycle confirmed={confirmed} "
            f"{_format_outbox_summary(summary)}",
            flush=True,
        )
        if stop_event.wait(poll_seconds):
            break
    print("outbox_dispatcher_stopped", flush=True)
    return 0


def cmd_outbox_dispatch(args) -> int:
    """Report or dispatch persisted safe internal job intents."""
    watch = getattr(args, "watch", False)
    if not 1 <= args.limit <= 1000:
        print("Outbox dispatch limit must be between 1 and 1000")
        return 2
    if watch and not args.apply:
        print("Outbox watch mode requires --apply")
        return 2
    poll_seconds = getattr(args, "poll_seconds", 5.0)
    if watch and not 1 <= poll_seconds <= 60:
        print("Outbox poll interval must be between 1 and 60 seconds")
        return 2
    try:
        if watch:
            stop_event = threading.Event()
            previous_handlers = {
                signum: signal.signal(signum, lambda _signum, _frame: stop_event.set())
                for signum in (signal.SIGINT, signal.SIGTERM)
            }
            try:
                return run_outbox_dispatch_watch(
                    lambda: _outbox_dispatch_cycle(apply=True, limit=args.limit),
                    stop_event=stop_event,
                    poll_seconds=poll_seconds,
                )
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)

        confirmed, summary = _outbox_dispatch_cycle(
            apply=args.apply,
            limit=args.limit,
        )
    except Exception as exc:
        print(f"Outbox dispatch failed: {type(exc).__name__}")
        return 2
    if not args.apply:
        print(f"Safe internal job dispatches: {_format_outbox_summary(summary)}; no jobs were enqueued")
        return 0
    print(f"Confirmed safe internal job dispatches: {confirmed}; {_format_outbox_summary(summary)}")
    return 0 if summary.pending == 0 else 1


def _format_outbox_summary(summary: outbox.OutboxSummary) -> str:
    oldest_age = (
        f"{summary.oldest_age_seconds}s"
        if summary.oldest_age_seconds is not None
        else "none"
    )
    last_errors = ",".join(
        f"{error_type}={count}"
        for error_type, count in summary.last_error_counts
    ) or "none"
    return (
        f"pending={summary.pending} eligible={summary.eligible} withheld={summary.withheld} "
        f"leased={summary.leased} delayed={summary.delayed} "
        f"oldest_age={oldest_age} last_errors={last_errors}"
    )


def _queue_registry_counts(queue_name: str, connection: Redis) -> dict[str, int]:
    """Read RQ registries without invoking cleanup or fetching job payloads."""
    registries = {
        "started": StartedJobRegistry,
        "deferred": DeferredJobRegistry,
        "scheduled": ScheduledJobRegistry,
        "failed": FailedJobRegistry,
    }
    return {
        state: registry_type(queue_name, connection=connection).get_job_count(cleanup=False)
        for state, registry_type in registries.items()
    }


def _format_registry_counts(registry: dict[str, int]) -> str:
    return ",".join(f"{state}={registry[state]}" for state in ("started", "deferred", "scheduled", "failed"))


def _queue_head_job_enqueued_at(queue: Queue, client: Redis, count: int) -> str:
    """Read the next queued job timestamp without deserializing its payload."""
    if count == 0:
        return "none"
    job_ids = queue.get_job_ids(offset=0, length=1)
    if not job_ids:
        return "unknown"
    # Job.fetch() unpickles the full Redis payload. Read only this scalar field.
    raw_timestamp = client.hget(Job.key_for(job_ids[0]), "enqueued_at")
    if raw_timestamp is None:
        return "unknown"
    try:
        timestamp_value = raw_timestamp.decode("ascii") if isinstance(raw_timestamp, bytes) else raw_timestamp
        if not isinstance(timestamp_value, str):
            return "unknown"
        if timestamp_value.endswith("Z"):
            timestamp_value = f"{timestamp_value[:-1]}+00:00"
        timestamp = datetime.fromisoformat(timestamp_value)
        if timestamp.tzinfo is None:
            return "unknown"
        return timestamp.astimezone(timezone.utc).isoformat()
    except (UnicodeDecodeError, ValueError, TypeError, OverflowError):
        return "unknown"


def _queue_head_job_age_seconds(enqueued_at: str, checked_at: datetime) -> str:
    """Calculate aggregate oldest queued-job age from the safe timestamp only."""
    if enqueued_at in {"none", "unknown"}:
        return enqueued_at
    try:
        timestamp = datetime.fromisoformat(enqueued_at)
        if timestamp.tzinfo is None or checked_at.tzinfo is None:
            return "unknown"
        age_seconds = (checked_at - timestamp).total_seconds()
        if age_seconds < 0:
            return "future"
        return f"{int(age_seconds)}s"
    except (ValueError, TypeError, OverflowError):
        return "unknown"


def _worker_heartbeat_summary(workers, checked_at: datetime) -> str:
    """Return aggregate heartbeat age without exposing worker names or hosts."""
    if not workers:
        return "none"
    ages = [
        max(0, int((checked_at - worker.last_heartbeat).total_seconds()))
        for worker in workers
        if worker.last_heartbeat is not None
    ]
    missing = len(workers) - len(ages)
    age = f"{max(ages)}s" if ages else "unknown"
    return f"{age}/missing={missing}"


def _task_lease_table_exists() -> bool:
    with engine.connect() as connection:
        return inspect(connection).has_table("task_lease")


def cmd_ocr_reconcile(args) -> int:
    """Report or enqueue eligible local OCR tasks; database claims fence duplicates."""
    try:
        if not _task_lease_table_exists():
            print(
                "Отказ: безопасная сверка OCR недоступна: отсутствует таблица task_lease; "
                "схема не изменялась, задания и очередь не проверялись"
            )
            return 2
        with session_scope() as db:
            task_ids = reconcilable_ocr_task_ids(db)
    except Exception as exc:
        print(f"Не удалось проверить задания OCR: {type(exc).__name__}")
        return 2

    if not args.apply:
        print(f"Eligible local OCR tasks: {len(task_ids)}; no jobs were enqueued")
        return 0
    if not settings.ocr_task_leases_enabled:
        print("Отказ: включите OCR_TASK_LEASES_ENABLED=1 только после применения миграции")
        return 2

    client = None
    enqueued = 0
    try:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        queue = Queue("ocr", connection=client, default_timeout=OCR_JOB_TIMEOUT_SECONDS)
        from .workers.jobs import recognize

        for task_id in task_ids:
            queue.enqueue(recognize, task_id)
            enqueued += 1
    except Exception as exc:
        print(f"Очередь OCR приняла {enqueued} из {len(task_ids)} заданий; ошибка {type(exc).__name__}")
        return 2
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                print(f"Закрытие соединения Redis не удалось: {type(exc).__name__}")

    print(f"Enqueued {enqueued} eligible local OCR task(s); claims remain database-fenced")
    return 0


def cmd_storage_check(args) -> int:
    """Audit local document permissions without opening document contents."""
    if os.name != "posix":
        print("Проверка POSIX-прав хранилища доступна только на POSIX-системах")
        return 2

    root = settings.upload_dir
    try:
        root_stat = root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode):
            print("Хранилище документов недоступно: каталог отсутствует или является ссылкой")
            return 2
        directory_mode = stat.S_IMODE(root_stat.st_mode)
        expected_owner = os.geteuid()
        directory_owner_matches = root_stat.st_uid == expected_owner
        regular_files = 0
        unsafe_files = 0
        files_with_other_owner = 0
        symlinks = 0
        unexpected_entries = 0
        for entry in root.iterdir():
            entry_stat = entry.lstat()
            if stat.S_ISLNK(entry_stat.st_mode):
                symlinks += 1
            elif stat.S_ISREG(entry_stat.st_mode):
                regular_files += 1
                if stat.S_IMODE(entry_stat.st_mode) & 0o077:
                    unsafe_files += 1
                if entry_stat.st_uid != expected_owner:
                    files_with_other_owner += 1
            else:
                unexpected_entries += 1
    except OSError as exc:
        print(f"Проверка прав хранилища не удалась: {type(exc).__name__}")
        return 2

    print(
        "Document storage permissions: "
        f"directory_mode={directory_mode:04o} directory_owner_matches={directory_owner_matches} "
        f"files={regular_files} "
        f"files_with_group_or_other_access={unsafe_files} symlinks={symlinks} "
        f"files_with_other_owner={files_with_other_owner} unexpected_entries={unexpected_entries}"
    )
    return 0 if (
        directory_mode == 0o700
        and directory_owner_matches
        and not (unsafe_files or files_with_other_owner or symlinks or unexpected_entries)
    ) else 1


def cmd_create_user(args) -> int:
    password = getpass.getpass("Пароль для входа в приложение: ")
    if not password:
        print("Пустой пароль не годится")
        return 1
    with session_scope() as db:
        user = auth.create_user(
            db, args.login, password, full_name=args.name, sro_login=args.sro_login
        )
        print(f"Создан пользователь {user.login}")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "create-user" in argv and any(
        argument == "--password" or argument.startswith("--password=")
        for argument in argv
    ):
        print("Передача пароля в аргументах запрещена; используйте скрытый ввод")
        return 2

    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-db", help="применить Alembic migrations до текущей ревизии")
    p_init.add_argument(
        "--reset",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p_init.set_defaults(func=cmd_init_db)

    p_schema = sub.add_parser(
        "schema-check",
        help="сравнить схему БД с моделями без изменений в базе",
    )
    p_schema.set_defaults(func=cmd_schema_check)

    p_db_role = sub.add_parser(
        "db-role-check",
        help="показать effective privileges роли текущего соединения PostgreSQL без изменений",
    )
    p_db_role.set_defaults(func=cmd_db_role_check)

    p_ops = sub.add_parser(
        "ops-status",
        help="показать read-only состояние заданий, очередей и воркеров",
    )
    p_ops.set_defaults(func=cmd_ops_status)

    p_reconcile = sub.add_parser(
        "ocr-reconcile",
        help="показать или поставить в OCR-очередь задания с безопасной арендой",
    )
    p_reconcile.add_argument(
        "--apply",
        action="store_true",
        help="поставить найденные задания в очередь (требует включённых OCR leases)",
    )
    p_reconcile.set_defaults(func=cmd_ocr_reconcile)

    p_outbox = sub.add_parser(
        "outbox-dispatch",
        help="показать или отправить сохранённые задания безопасной внутренней обработки",
    )
    p_outbox.add_argument(
        "--apply",
        action="store_true",
        help="поставить разрешённые jobs в RQ (только OCR и local validation)",
    )
    p_outbox.add_argument("--limit", type=int, default=100)
    p_outbox.add_argument(
        "--watch",
        action="store_true",
        help="непрерывно отправлять ограниченные batches; требует --apply",
    )
    p_outbox.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="пауза между batch (1..60 секунд)",
    )
    p_outbox.set_defaults(func=cmd_outbox_dispatch)

    p_storage = sub.add_parser(
        "storage-check",
        help="проверить POSIX-права локального хранилища без чтения документов",
    )
    p_storage.set_defaults(func=cmd_storage_check)

    p_user = sub.add_parser("create-user", help="завести сотрудника")
    p_user.add_argument("login", help="как в СРО, например «Иванова М.И.»")
    p_user.add_argument("--name", default="")
    p_user.add_argument("--sro-login", default="", help="если отличается от логина у нас")
    p_user.set_defaults(func=cmd_create_user)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
