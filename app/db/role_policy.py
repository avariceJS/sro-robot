"""Fail-closed privilege ceiling for production application connections."""

from __future__ import annotations

_RUNTIME_ROLE_POLICY = """
    SELECT (
        role.rolsuper
        OR role.rolcreaterole
        OR role.rolcreatedb
        OR role.rolreplication
        OR role.rolbypassrls
        OR has_database_privilege(current_user, current_database(), 'CREATE')
        OR has_schema_privilege(current_user, 'public', 'CREATE')
        OR EXISTS (
            SELECT 1
            FROM pg_roles AS member_role
            WHERE member_role.rolname <> current_user
              AND pg_has_role(current_user, member_role.oid, 'MEMBER')
        )
        OR EXISTS (
            SELECT 1
            FROM pg_class AS owned_relation
            JOIN pg_namespace AS namespace ON namespace.oid = owned_relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND owned_relation.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
              AND owned_relation.relowner = role.oid
        )
        OR EXISTS (
            SELECT 1
            FROM pg_proc AS routine
            JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
            WHERE namespace.nspname = 'public'
              AND routine.proowner = role.oid
        )
        OR EXISTS (
            SELECT 1
            FROM pg_type AS data_type
            JOIN pg_namespace AS namespace ON namespace.oid = data_type.typnamespace
            WHERE namespace.nspname = 'public'
              AND data_type.typowner = role.oid
        )
        OR EXISTS (
            SELECT 1
            FROM pg_class AS table_relation
            JOIN pg_namespace AS namespace ON namespace.oid = table_relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND table_relation.relkind IN ('r', 'p')
              AND (
                  has_table_privilege(current_user, table_relation.oid, 'TRUNCATE')
                  OR has_table_privilege(current_user, table_relation.oid, 'REFERENCES')
                  OR has_table_privilege(current_user, table_relation.oid, 'TRIGGER')
              )
        )
        OR EXISTS (
            SELECT 1
            FROM pg_class AS sequence_relation
            JOIN pg_namespace AS namespace ON namespace.oid = sequence_relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND sequence_relation.relkind = 'S'
              AND has_sequence_privilege(current_user, sequence_relation.oid, 'UPDATE')
        )
        OR EXISTS (
            SELECT 1
            FROM pg_class AS revision_relation
            JOIN pg_namespace AS namespace ON namespace.oid = revision_relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND revision_relation.relname = 'alembic_version'
              AND revision_relation.relkind IN ('r', 'p')
              AND (
                  has_table_privilege(current_user, revision_relation.oid, 'INSERT')
                  OR has_table_privilege(current_user, revision_relation.oid, 'UPDATE')
                  OR has_table_privilege(current_user, revision_relation.oid, 'DELETE')
                  OR has_table_privilege(current_user, revision_relation.oid, 'TRUNCATE')
              )
        )
    ) AS violates_runtime_privilege_ceiling
    FROM pg_roles AS role
    WHERE role.rolname = current_user
    """


def enforce_runtime_privilege_ceiling(cursor) -> None:
    """Reject app connections whose PostgreSQL identity can administer the DB."""
    try:
        cursor.execute(_RUNTIME_ROLE_POLICY)
        row = cursor.fetchone()
    except Exception:
        raise RuntimeError(
            "Production runtime database role privilege check failed"
        ) from None

    if row is None or row[0] is not False:
        raise RuntimeError(
            "Production runtime database role exceeds its privilege ceiling"
        )
