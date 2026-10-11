"""Runtime schema compatibility policy shared by readiness and diagnostics."""

from alembic.runtime.migration import MigrationContext
from sqlalchemy.engine import Connection


EXPECTED_SCHEMA_REVISION = "3b6a9f2d41c8"


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
