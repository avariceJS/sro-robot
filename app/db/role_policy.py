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
            FROM pg_roles AS elevated
            WHERE elevated.rolname <> current_user
              AND (elevated.rolsuper OR elevated.rolcreaterole
                   OR elevated.rolcreatedb OR elevated.rolreplication
                   OR elevated.rolbypassrls)
              AND pg_has_role(current_user, elevated.oid, 'MEMBER')
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
